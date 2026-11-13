"""Servicio de usuarios: cuentas, contraseñas y sesiones. Base de datos propia en PostgreSQL.

También inicia la Saga de eliminación de cuenta y aplica su compensación si un paso falla:
    1. usuarios      marca la cuenta como 'eliminando' y cierra sus sesiones   (compensable: reactivar)
    2. historial     borra las predicciones del usuario                        (punto sin retorno)
    3. metricas      borra los contadores del usuario                           (reintentable)
    4. usuarios      borra la cuenta definitivamente
"""
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import asyncpg
from comun.descubrimiento import Registro
from comun.mensajeria import Bus
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

log = logging.getLogger("usuarios")
DSN = os.getenv("DATABASE_URL", "postgresql://sigatoka:sigatoka123@postgres-usuarios/usuarios")
COOKIE = "sigatoka_sesion"
DURACION_SESION = timedelta(days=7)

ESQUEMA = """
CREATE TABLE IF NOT EXISTS usuarios (
    id      SERIAL PRIMARY KEY,
    nombre  TEXT NOT NULL,
    usuario TEXT NOT NULL,
    hash    TEXT NOT NULL,
    estado  TEXT NOT NULL DEFAULT 'activo' CHECK (estado IN ('activo', 'eliminando')),
    creado  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS usuarios_usuario_unico ON usuarios (lower(usuario));
CREATE TABLE IF NOT EXISTS sesiones (
    token_hash TEXT PRIMARY KEY,
    usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    creado     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expira     TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS sesiones_usuario ON sesiones (usuario_id);
CREATE TABLE IF NOT EXISTS sagas (
    id          UUID PRIMARY KEY,
    tipo        TEXT NOT NULL,
    usuario_id  INTEGER NOT NULL,
    estado      TEXT NOT NULL CHECK (estado IN ('en_curso', 'completada', 'compensada')),
    pasos       JSONB NOT NULL DEFAULT '[]',
    actualizado TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

estado: dict = {}


# ------------------------------------------------------------------ contraseñas y tokens
_N, _R, _P = 2 ** 14, 8, 1


def hash_contrasena(contrasena: str) -> str:
    sal = os.urandom(16)
    h = hashlib.scrypt(contrasena.encode(), salt=sal, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${sal.hex()}${h.hex()}"


def verificar_contrasena(contrasena: str, guardado: str) -> bool:
    try:
        _, n, r, p, sal, h = guardado.split("$")
        calc = hashlib.scrypt(contrasena.encode(), salt=bytes.fromhex(sal), n=int(n), r=int(r), p=int(p),
                              dklen=len(bytes.fromhex(h)))
        return hmac.compare_digest(calc.hex(), h)
    except (ValueError, TypeError):
        return False


_HASH_FALSO = hash_contrasena("contrasena-falsa-para-igualar-tiempos")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def crear_sesion(con, usuario_id: int) -> str:
    token = secrets.token_urlsafe(32)
    await con.execute("INSERT INTO sesiones (token_hash, usuario_id, expira) VALUES ($1, $2, $3)",
                      hash_token(token), usuario_id, datetime.now(timezone.utc) + DURACION_SESION)
    return token


def poner_cookie(resp: Response, request: Request, token: str):
    seguro = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    resp.set_cookie(COOKIE, token, max_age=int(DURACION_SESION.total_seconds()), httponly=True,
                    samesite="lax", secure=seguro, path="/")


# ------------------------------------------------------------------ Saga de eliminación de cuenta
async def registrar_paso(con, saga_id, paso: str, nuevo_estado: str | None = None):
    await con.execute(
        "UPDATE sagas SET pasos = pasos || $2::jsonb, estado = COALESCE($3, estado), actualizado = now() "
        "WHERE id = $1", saga_id, json.dumps([{"paso": paso, "fecha": datetime.now(timezone.utc).isoformat()}]),
        nuevo_estado)


async def al_recibir_evento(evento: dict):
    d, tipo = evento["datos"], evento["tipo"]
    saga_id = uuid.UUID(d["saga_id"])
    async with estado["pool"].acquire() as con:
        async with con.transaction():
            saga = await con.fetchrow("SELECT estado FROM sagas WHERE id = $1 FOR UPDATE", saga_id)
            if not saga or saga["estado"] != "en_curso":
                return                                           # evento repetido: idempotente
            if tipo == "historial.usuario_purgado":
                await registrar_paso(con, saga_id, f"historial borró {d.get('eliminadas', 0)} predicciones")
            elif tipo == "metricas.usuario_purgado":
                await con.execute("DELETE FROM usuarios WHERE id = $1", d["usuario_id"])
                await registrar_paso(con, saga_id, "metricas borró los contadores; cuenta eliminada", "completada")
                log.info("Saga %s completada", saga_id)
            elif tipo == "historial.purga_fallida":
                # Compensación del paso 1: la cuenta vuelve a estar activa
                await con.execute("UPDATE usuarios SET estado = 'activo' WHERE id = $1", d["usuario_id"])
                await registrar_paso(con, saga_id, f"historial falló ({d.get('motivo')}); cuenta reactivada",
                                     "compensada")
                log.warning("Saga %s compensada", saga_id)


# ------------------------------------------------------------------ aplicación
@asynccontextmanager
async def ciclo_de_vida(app: FastAPI):
    import asyncio
    for intento in range(30):
        try:
            estado["pool"] = await asyncpg.create_pool(DSN, min_size=1, max_size=10)
            break
        except (OSError, asyncpg.PostgresError):
            log.info("PostgreSQL no disponible todavía (intento %d)", intento + 1)
            await asyncio.sleep(2)
    async with estado["pool"].acquire() as con:
        await con.execute(ESQUEMA)
        await con.execute("DELETE FROM sesiones WHERE expira < now()")
    estado["bus"] = Bus("usuarios")
    await estado["bus"].conectar()
    await estado["bus"].suscribir("usuarios.saga", ["historial.usuario_purgado", "historial.purga_fallida",
                                                    "metricas.usuario_purgado"], al_recibir_evento)
    estado["registro"] = Registro("usuarios")
    await estado["registro"].registrar()
    yield
    await estado["registro"].retirar()
    await estado["bus"].cerrar()
    await estado["pool"].close()


app = FastAPI(title="Servicio de usuarios", version="3.0", lifespan=ciclo_de_vida)


class Alta(BaseModel):
    nombre: str = Field(min_length=1, max_length=60)
    usuario: str = Field(min_length=3, max_length=32)
    contrasena: str = Field(min_length=8, max_length=128)


class Acceso(BaseModel):
    usuario: str = Field(min_length=1, max_length=32)
    contrasena: str = Field(min_length=1, max_length=128)


class Token(BaseModel):
    token: str = Field(min_length=1, max_length=200)


def publico(u) -> dict:
    return {"id": u["id"], "nombre": u["nombre"], "usuario": u["usuario"]}


@app.post("/api/registro", status_code=201)
async def registro(datos: Alta, request: Request, response: Response):
    usuario, nombre = datos.usuario.strip(), datos.nombre.strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,32}", usuario):
        raise HTTPException(422, "El usuario solo puede tener letras, números, punto, guion y guion bajo.")
    async with estado["pool"].acquire() as con:
        try:
            uid = await con.fetchval("INSERT INTO usuarios (nombre, usuario, hash) VALUES ($1, $2, $3) RETURNING id",
                                     nombre, usuario, hash_contrasena(datos.contrasena))
        except asyncpg.UniqueViolationError:
            raise HTTPException(409, "Ese nombre de usuario ya está registrado.") from None
        poner_cookie(response, request, await crear_sesion(con, uid))
    return {"id": uid, "nombre": nombre, "usuario": usuario}


@app.post("/api/login")
async def login(datos: Acceso, request: Request, response: Response):
    async with estado["pool"].acquire() as con:
        u = await con.fetchrow("SELECT * FROM usuarios WHERE lower(usuario) = lower($1) AND estado = 'activo'",
                               datos.usuario.strip())
        # Se verifica siempre un hash para no revelar qué usuarios existen
        ok = verificar_contrasena(datos.contrasena, u["hash"] if u else _HASH_FALSO)
        if not (u and ok):
            raise HTTPException(401, "Usuario o contraseña incorrectos.")
        poner_cookie(response, request, await crear_sesion(con, u["id"]))
    return publico(u)


@app.post("/api/logout")
async def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE)
    if token:
        async with estado["pool"].acquire() as con:
            await con.execute("DELETE FROM sesiones WHERE token_hash = $1", hash_token(token))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


async def usuario_de_token(token: str | None):
    if not token:
        return None
    async with estado["pool"].acquire() as con:
        return await con.fetchrow(
            "SELECT u.id, u.nombre, u.usuario FROM sesiones s JOIN usuarios u ON u.id = s.usuario_id "
            "WHERE s.token_hash = $1 AND s.expira > now() AND u.estado = 'activo'", hash_token(token))


@app.get("/api/yo")
async def yo(request: Request):
    u = await usuario_de_token(request.cookies.get(COOKIE))
    if not u:
        raise HTTPException(401, "Inicie sesión para continuar.")
    return publico(u)


@app.post("/interno/sesiones/validar")
async def validar_sesion(datos: Token):
    """Lo usa el API Gateway para saber qué usuario envía cada petición."""
    u = await usuario_de_token(datos.token)
    if not u:
        raise HTTPException(404, "Sesión no válida.")
    return publico(u)


@app.delete("/api/cuenta", status_code=202)
async def eliminar_cuenta(request: Request, response: Response):
    u = await usuario_de_token(request.cookies.get(COOKIE))
    if not u:
        raise HTTPException(401, "Inicie sesión para continuar.")
    saga_id = uuid.uuid4()
    async with estado["pool"].acquire() as con:
        async with con.transaction():
            await con.execute("UPDATE usuarios SET estado = 'eliminando' WHERE id = $1", u["id"])
            await con.execute("DELETE FROM sesiones WHERE usuario_id = $1", u["id"])
            await con.execute("INSERT INTO sagas (id, tipo, usuario_id, estado) VALUES ($1, 'eliminar_cuenta', $2, "
                              "'en_curso')", saga_id, u["id"])
            await registrar_paso(con, saga_id, "cuenta desactivada y sesiones cerradas")
    await estado["bus"].publicar("cuenta.eliminacion_solicitada", {"usuario_id": u["id"], "saga_id": str(saga_id)})
    response.delete_cookie(COOKIE, path="/")
    return {"saga_id": str(saga_id), "estado": "en_curso"}


@app.get("/api/cuenta/eliminacion/{saga_id}")
async def estado_saga(saga_id: uuid.UUID):
    async with estado["pool"].acquire() as con:
        s = await con.fetchrow("SELECT id, estado, pasos, actualizado FROM sagas WHERE id = $1", saga_id)
    if not s:
        raise HTTPException(404, "Proceso no encontrado.")
    return {"saga_id": str(s["id"]), "estado": s["estado"], "pasos": json.loads(s["pasos"]),
            "actualizado": s["actualizado"].isoformat()}


@app.get("/salud")
async def salud():
    async with estado["pool"].acquire() as con:
        await con.fetchval("SELECT 1")
    return {"ok": True, "servicio": "usuarios"}
