"""API Gateway: único punto de entrada del sistema.

Se encarga de lo transversal para que los servicios no lo repitan:
    enrutamiento por prefijo hacia la instancia sana que indica Consul
    validación de la sesión e inyección de X-Usuario-Id hacia los servicios internos
    protección CSRF, límite de intentos de inicio de sesión y cabeceras de seguridad
    composición de respuestas (historial + métricas) para que el frontend no cambie
    Circuit Breaker por servicio de destino
"""
import asyncio
import logging
import os
import time
from collections import defaultdict, deque

import httpx
from comun.circuito import CircuitoAbierto, ClienteServicio, ErrorServicio
from comun.descubrimiento import Resolutor, ServicioNoDisponible
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

log = logging.getLogger("gateway")
COOKIE = "sigatoka_sesion"
MAX_BYTES = int(float(os.getenv("SIGATOKA_MAX_MB", "25")) * 1024 * 1024) + 1024 * 1024
SALTO = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "content-length",
         "content-encoding", "host"}
SERVICIOS = ("usuarios", "diagnostico", "historial", "metricas", "web")

resolutor = Resolutor(ttl=5)
clientes = {s: ClienteServicio(s, resolutor, timeout=30, umbral_fallos=3, espera_s=15) for s in SERVICIOS}
app = FastAPI(title="API Gateway · Detector de Sigatoka negra", version="3.0", docs_url="/gateway/docs",
              openapi_url="/gateway/openapi.json")


# ------------------------------------------------------------------ seguridad transversal
@app.middleware("http")
async def seguridad(request: Request, call_next):
    if (request.method in ("POST", "PATCH", "DELETE") and request.url.path.startswith("/api/")
            and request.headers.get("x-requested-with") != "fetch"):
        return JSONResponse(status_code=403, content={"detail": "Petición no permitida."})
    if int(request.headers.get("content-length") or 0) > MAX_BYTES:
        return JSONResponse(status_code=413, content={"detail": "La petición es demasiado grande."})
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Permissions-Policy"] = "camera=(self), microphone=(), geolocation=()"
    if not request.url.path.startswith("/gateway/docs") and request.url.path != "/gateway/openapi.json":
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; "
            "style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
    return resp


_fallos_login: dict[str, deque] = defaultdict(deque)


def limitar_intentos(ip: str):
    """Máximo 8 intentos fallidos en 10 minutos por IP."""
    q, ahora = _fallos_login[ip], time.time()
    while q and ahora - q[0] > 600:
        q.popleft()
    if len(q) >= 8:
        raise HTTPException(429, "Demasiados intentos. Espere unos minutos e inténtelo de nuevo.")


# ------------------------------------------------------------------ sesión
_sesiones: dict[str, tuple[float, dict]] = {}       # caché corta para no consultar usuarios en cada petición


async def usuario_actual(request: Request) -> dict:
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, "Inicie sesión para continuar.")
    t, u = _sesiones.get(token, (0.0, None))
    if u and time.monotonic() - t < 15:
        return u
    try:
        r = await clientes["usuarios"].peticion("POST", "/interno/sesiones/validar", json={"token": token})
    except (CircuitoAbierto, ServicioNoDisponible, ErrorServicio, httpx.TransportError):
        raise HTTPException(503, "El servicio de usuarios no está disponible.") from None
    if r.status_code != 200:
        _sesiones.pop(token, None)
        raise HTTPException(401, "Inicie sesión para continuar.")
    _sesiones[token] = (time.monotonic(), r.json())
    return r.json()


# ------------------------------------------------------------------ proxy
def cabeceras_salida(request: Request, extra: dict | None = None) -> dict:
    h = {k: v for k, v in request.headers.items() if k.lower() not in SALTO and k.lower() != "x-usuario-id"}
    h["x-forwarded-proto"] = request.headers.get("x-forwarded-proto", request.url.scheme)
    h["x-forwarded-for"] = request.client.host if request.client else ""
    return {**h, **(extra or {})}


async def reenviar(servicio: str, request: Request, extra: dict | None = None,
                   ruta: str | None = None) -> httpx.Response:
    try:
        return await clientes[servicio].peticion(
            request.method, ruta or request.url.path, params=list(request.query_params.multi_items()),
            content=await request.body(), headers=cabeceras_salida(request, extra))
    except (CircuitoAbierto, ServicioNoDisponible, httpx.TransportError):
        raise HTTPException(503, f"El servicio de {servicio} no está disponible en este momento.") from None
    except ErrorServicio as e:
        return e.respuesta


def a_respuesta(r: httpx.Response) -> Response:
    resp = Response(r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type"))
    for k, v in r.headers.multi_items():
        if k.lower() not in SALTO and k.lower() != "content-type":
            resp.headers.append(k, v)
    return resp


async def resumen(uid: int, min_version: int = 0):
    """Composición de API: el resumen vive en métricas. Si métricas falla, el historial se entrega igual."""
    try:
        r = await clientes["metricas"].peticion("GET", "/api/metricas", params={"min_version": min_version},
                                                headers={"x-usuario-id": str(uid)})
        return r.json() if r.status_code == 200 else None
    except (CircuitoAbierto, ServicioNoDisponible, ErrorServicio, httpx.TransportError):
        return None


# ------------------------------------------------------------------ rutas: usuarios
@app.post("/api/login")
async def login(request: Request):
    ip = request.client.host if request.client else "?"
    limitar_intentos(ip)
    r = await reenviar("usuarios", request)
    if r.status_code == 401:
        _fallos_login[ip].append(time.time())
    elif r.status_code == 200:
        _fallos_login.pop(ip, None)
    return a_respuesta(r)


@app.post("/api/logout")
@app.delete("/api/cuenta")
async def cerrar(request: Request):
    _sesiones.pop(request.cookies.get(COOKIE, ""), None)
    return a_respuesta(await reenviar("usuarios", request))


@app.api_route("/api/registro", methods=["POST"])
@app.api_route("/api/yo", methods=["GET"])
@app.api_route("/api/cuenta/eliminacion/{saga_id}", methods=["GET"])
async def a_usuarios(request: Request):
    return a_respuesta(await reenviar("usuarios", request))


# ------------------------------------------------------------------ rutas: diagnóstico
@app.api_route("/api/requisitos", methods=["GET"])
@app.api_route("/predecir", methods=["POST"])
async def a_diagnostico(request: Request):
    return a_respuesta(await reenviar("diagnostico", request))


# ------------------------------------------------------------------ rutas: historial y métricas
@app.get("/api/predicciones")
async def historial(request: Request):
    u = await usuario_actual(request)
    r, res = await asyncio.gather(reenviar("historial", request, {"x-usuario-id": str(u["id"])}), resumen(u["id"]))
    if r.status_code != 200:
        return a_respuesta(r)
    return {**r.json(), "resumen": res}


@app.api_route("/api/predicciones/{pid}", methods=["PATCH", "DELETE"])
async def modificar(pid: int, request: Request):
    u = await usuario_actual(request)
    r = await reenviar("historial", request, {"x-usuario-id": str(u["id"])})
    if r.status_code != 200:
        return a_respuesta(r)
    d = r.json()
    return {**d, "resumen": await resumen(u["id"], d.get("seq") or 0)}


@app.api_route("/api/predicciones", methods=["POST"])
@app.api_route("/api/predicciones.csv", methods=["GET"])
@app.api_route("/api/predicciones/{pid}/miniatura", methods=["GET"])
async def a_historial(request: Request):
    u = await usuario_actual(request)
    return a_respuesta(await reenviar("historial", request, {"x-usuario-id": str(u["id"])}))


@app.get("/api/metricas")
async def a_metricas(request: Request):
    u = await usuario_actual(request)
    return a_respuesta(await reenviar("metricas", request, {"x-usuario-id": str(u["id"])}))


# ------------------------------------------------------------------ salud y frontend
@app.get("/salud")
async def salud():
    estados = {}
    for s in SERVICIOS:
        n = len(await resolutor.instancias(s))
        estados[s] = {"instancias_sanas": n, "circuito": clientes[s].breaker.estado}
    ok = all(v["instancias_sanas"] for v in estados.values())
    return JSONResponse(status_code=200 if ok else 503, content={"ok": ok, "servicios": estados})


@app.get("/", include_in_schema=False)
@app.get("/static/{ruta:path}", include_in_schema=False)
async def a_web(request: Request):
    return a_respuesta(await reenviar("web", request))
