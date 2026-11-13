"""Servicio de métricas: matriz de confusión, aciertos y pendientes de cada usuario. Base de datos propia en Redis.

Es un modelo de lectura que se alimenta solo de eventos. Cada evento suma o resta en contadores, así el resumen
se responde en tiempo constante sin recorrer el historial. Los eventos se aplican una sola vez aunque lleguen
repetidos.
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

import redis.asyncio as redis
from comun.descubrimiento import Registro
from comun.mensajeria import Bus
from fastapi import FastAPI, Header, Query

log = logging.getLogger("metricas")
REDIS_URL = os.getenv("REDIS_URL", "redis://:sigatoka123@redis-metricas:6379/0")
CLASES = ("sana", "sigatoka", "otro")
estado: dict = {}

# Aplica los incrementos de un evento solo si no se había aplicado antes (idempotencia atómica)
APLICAR = """
if redis.call('SADD', KEYS[2], ARGV[1]) == 0 then return 0 end
redis.call('EXPIRE', KEYS[2], 604800)
for i = 2, #ARGV, 2 do redis.call('HINCRBY', KEYS[1], ARGV[i], ARGV[i + 1]) end
redis.call('HINCRBY', KEYS[1], 'seq', 1)
return 1
"""


def clave(uid) -> str:
    return f"metricas:{uid}"


def celda(real, pred) -> str:
    return "pendientes" if real is None else f"m:{real}:{pred}"


async def al_recibir_evento(evento: dict):
    d, tipo = evento["datos"], evento["tipo"]
    uid = d["usuario_id"]
    if tipo == "historial.usuario_purgado":                     # paso 3 de la Saga de eliminación de cuenta
        await estado["redis"].delete(clave(uid), f"{clave(uid)}:eventos")
        await estado["bus"].publicar("metricas.usuario_purgado", {"usuario_id": uid, "saga_id": d["saga_id"]})
        return
    if tipo == "prediccion.registrada":
        cambios = {"total": 1, "pendientes": 1}
    elif tipo == "prediccion.revisada":
        cambios = {celda(d["real_anterior"], d["predicha"]): -1}
        nueva = celda(d["real_nueva"], d["predicha"])
        cambios[nueva] = cambios.get(nueva, 0) + 1
    elif tipo == "prediccion.eliminada":
        cambios = {"total": -1, celda(d["real"], d["predicha"]): -1}
    else:
        return
    args = [evento["evento_id"]] + [x for par in cambios.items() for x in par]
    await estado["aplicar"](keys=[clave(uid), f"{clave(uid)}:eventos"], args=args)


async def resumen(uid: int) -> dict:
    h = {k: int(v) for k, v in (await estado["redis"].hgetall(clave(uid))).items()}
    matriz = {r: {p: h.get(f"m:{r}:{p}", 0) for p in CLASES} for r in CLASES}
    total, pendientes = h.get("total", 0), h.get("pendientes", 0)
    revisadas = total - pendientes
    aciertos = sum(matriz[c][c] for c in CLASES)
    return {
        "total": total, "revisadas": revisadas, "pendientes": pendientes,
        "aciertos": aciertos, "errores": revisadas - aciertos,
        "VP": matriz["sigatoka"]["sigatoka"], "FN": matriz["sigatoka"]["sana"],
        "FP": matriz["sana"]["sigatoka"], "VN": matriz["sana"]["sana"],
        "hojas_rechazadas": matriz["sana"]["otro"] + matriz["sigatoka"]["otro"],
        "matriz": matriz, "version": h.get("seq", 0),
    }


@asynccontextmanager
async def ciclo_de_vida(app: FastAPI):
    estado["redis"] = redis.from_url(REDIS_URL, decode_responses=True)
    estado["aplicar"] = estado["redis"].register_script(APLICAR)
    estado["bus"] = Bus("metricas")
    await estado["bus"].conectar()
    await estado["bus"].suscribir("metricas.eventos", ["prediccion.*", "historial.usuario_purgado"],
                                  al_recibir_evento)
    estado["registro"] = Registro("metricas")
    await estado["registro"].registrar()
    yield
    await estado["registro"].retirar()
    await estado["bus"].cerrar()
    await estado["redis"].aclose()


app = FastAPI(title="Servicio de métricas", version="3.0", lifespan=ciclo_de_vida)


@app.get("/api/metricas")
async def metricas(x_usuario_id: int = Header(...), min_version: int = Query(0, ge=0)):
    """min_version permite leer lo que uno mismo acaba de escribir: espera hasta 2 s a que llegue ese evento."""
    for _ in range(40):
        r = await resumen(x_usuario_id)
        if r["version"] >= min_version:
            return {**r, "al_dia": True}
        await asyncio.sleep(0.05)
    return {**r, "al_dia": False}


@app.get("/salud")
async def salud():
    await estado["redis"].ping()
    return {"ok": True, "servicio": "metricas"}
