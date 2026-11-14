"""Servicio de historial: registra cada análisis con su miniatura y la revisión manual. Base de datos propia en MongoDB.

Para registrar un análisis llama de forma síncrona al servicio de diagnóstico, protegido con Circuit Breaker,
y después publica eventos para que el servicio de métricas actualice sus contadores sin bloquear al usuario.

Eventos publicados:  prediccion.registrada · prediccion.revisada · prediccion.eliminada
                     historial.usuario_purgado · historial.purga_fallida   (Saga de eliminación de cuenta)
"""
import base64
import csv
import io
import logging
import os
from contextlib import asynccontextmanager
from typing import Literal, Optional

from bson import Binary
from comun.circuito import CircuitoAbierto, ClienteServicio, ErrorServicio
from comun.descubrimiento import Registro, Resolutor, ServicioNoDisponible
from comun.mensajeria import Bus, ahora
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Response, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from pymongo import DESCENDING, AsyncMongoClient, ReturnDocument

log = logging.getLogger("historial")
MONGO_URL = os.getenv("MONGO_URL", "mongodb://sigatoka:sigatoka123@mongo-historial:27017")
SIMULAR_FALLO_PURGA = os.getenv("SIMULAR_FALLO_PURGA", "0") == "1"
CAMPOS_PUBLICOS = {"_id": 0, "id": 1, "fecha": 1, "origen": 1, "nombre_archivo": 1, "ancho": 1, "alto": 1,
                   "clase_predicha": 1, "confianza": 1, "prob_sigatoka": 1, "prob_no_hoja": 1, "advertencia": 1,
                   "distancia_ood": 1, "version_modelo": 1, "clase_real": 1, "revisado_en": 1}
estado: dict = {}


def db():
    return estado["mongo"]["historial"]


async def siguiente(coleccion: str, clave) -> int:
    """Contador atómico. Se usa para el id de cada predicción y para la versión del historial de cada usuario."""
    doc = await db()[coleccion].find_one_and_update({"_id": clave}, {"$inc": {"v": 1}}, upsert=True,
                                                    return_document=ReturnDocument.AFTER)
    return doc["v"]


async def publicar_cambio(tipo: str, usuario_id: int, **datos) -> int:
    """Cada cambio sube la versión del historial del usuario. Métricas la usa para saber si está al día."""
    seq = await siguiente("versiones", usuario_id)
    await estado["bus"].publicar(tipo, {"usuario_id": usuario_id, "seq": seq, **datos})
    return seq


async def purgar_usuario(evento: dict):
    """Paso 2 de la Saga de eliminación de cuenta."""
    d = evento["datos"]
    try:
        if SIMULAR_FALLO_PURGA:
            raise RuntimeError("fallo simulado")
        r = await db().predicciones.delete_many({"usuario_id": d["usuario_id"]})
        await db().versiones.delete_one({"_id": d["usuario_id"]})
    except Exception as e:  # noqa: BLE001
        log.error("No se pudo purgar al usuario %s: %s", d["usuario_id"], e)
        await estado["bus"].publicar("historial.purga_fallida", {**d, "motivo": str(e)})
        return
    await estado["bus"].publicar("historial.usuario_purgado", {**d, "eliminadas": r.deleted_count})


@asynccontextmanager
async def ciclo_de_vida(app: FastAPI):
    estado["mongo"] = AsyncMongoClient(MONGO_URL)
    await db().predicciones.create_index([("usuario_id", 1), ("id", DESCENDING)])
    await db().predicciones.create_index("id", unique=True)
    estado["diagnostico"] = ClienteServicio("diagnostico", Resolutor(), timeout=30, umbral_fallos=3, espera_s=15)
    estado["bus"] = Bus("historial")
    await estado["bus"].conectar()
    await estado["bus"].suscribir("historial.saga", ["cuenta.eliminacion_solicitada"], purgar_usuario)
    estado["registro"] = Registro("historial")
    await estado["registro"].registrar()
    yield
    await estado["registro"].retirar()
    await estado["bus"].cerrar()
    await estado["mongo"].close()


app = FastAPI(title="Servicio de historial", version="3.0", lifespan=ciclo_de_vida)


def usuario(x_usuario_id: int = Header(...)) -> int:
    """El API Gateway valida la sesión y envía el id del usuario en esta cabecera."""
    return x_usuario_id


@app.post("/api/predicciones", status_code=201)
async def crear(imagen: UploadFile = File(...), origen: Literal["foto", "camara"] = Form("foto"),
                uid: int = Depends(usuario)):
    contenido = await imagen.read()
    archivos = {"imagen": (imagen.filename or "imagen", contenido, imagen.content_type or "application/octet-stream")}
    try:
        resp = await estado["diagnostico"].peticion("POST", "/interno/diagnosticos", params={"tta": "true"},
                                                    files=archivos)
    except (CircuitoAbierto, ServicioNoDisponible, ErrorServicio, OSError):
        raise HTTPException(503, "El servicio de diagnóstico no está disponible en este momento. "
                                 "El historial sigue funcionando. Intente de nuevo en unos segundos.",
                            headers={"X-Fallo-Dependencia": "diagnostico"}) from None
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, resp.json().get("detail", "Imagen no válida."))
    r = resp.json()
    miniatura = base64.b64decode(r.pop("miniatura_b64"))
    pid = await siguiente("contadores", "predicciones")
    doc = {
        "id": pid, "usuario_id": uid, "fecha": ahora(), "origen": origen,
        "nombre_archivo": (imagen.filename or "")[:120] if origen == "foto" else None,
        "ancho": r["ancho"], "alto": r["alto"], "clase_predicha": r["clase"], "confianza": r["confianza"],
        "prob_sigatoka": r["prob_sigatoka"], "prob_no_hoja": r["prob_no_hoja"], "advertencia": r["advertencia"],
        "distancia_ood": r["distancia_ood"], "version_modelo": r["version_modelo"], "clase_real": None,
        "revisado_en": None, "miniatura": Binary(miniatura),
    }
    await db().predicciones.insert_one(doc)
    r["seq"] = await publicar_cambio("prediccion.registrada", uid, prediccion_id=pid, predicha=r["clase"])
    r["id"] = pid
    return r


@app.get("/api/predicciones")
async def listar(pendientes: bool = False, limite: int = Query(500, ge=1, le=100000), uid: int = Depends(usuario)):
    filtro = {"usuario_id": uid}
    if pendientes:
        filtro["clase_real"] = None
    cursor = db().predicciones.find(filtro, CAMPOS_PUBLICOS).sort("id", DESCENDING).limit(limite)
    return {"predicciones": await cursor.to_list()}


class Correccion(BaseModel):
    clase_real: Optional[Literal["sana", "sigatoka", "otro"]] = None


@app.patch("/api/predicciones/{pid}")
async def corregir(pid: int, datos: Correccion, uid: int = Depends(usuario)):
    antes = await db().predicciones.find_one_and_update(
        {"id": pid, "usuario_id": uid},
        {"$set": {"clase_real": datos.clase_real, "revisado_en": ahora() if datos.clase_real else None}},
        projection={"clase_real": 1, "clase_predicha": 1}, return_document=ReturnDocument.BEFORE)
    if not antes:
        raise HTTPException(404, "Predicción no encontrada.")
    if antes["clase_real"] == datos.clase_real:
        return {"ok": True, "seq": None}
    seq = await publicar_cambio("prediccion.revisada", uid, prediccion_id=pid, predicha=antes["clase_predicha"],
                                real_anterior=antes["clase_real"], real_nueva=datos.clase_real)
    return {"ok": True, "seq": seq}


@app.delete("/api/predicciones/{pid}")
async def eliminar(pid: int, uid: int = Depends(usuario)):
    doc = await db().predicciones.find_one_and_delete({"id": pid, "usuario_id": uid},
                                                       projection={"clase_real": 1, "clase_predicha": 1})
    if not doc:
        raise HTTPException(404, "Predicción no encontrada.")
    seq = await publicar_cambio("prediccion.eliminada", uid, prediccion_id=pid, predicha=doc["clase_predicha"],
                                real=doc["clase_real"])
    return {"ok": True, "seq": seq}


@app.get("/api/predicciones/{pid}/miniatura")
async def ver_miniatura(pid: int, uid: int = Depends(usuario)):
    doc = await db().predicciones.find_one({"id": pid, "usuario_id": uid}, {"miniatura": 1})
    if not doc or not doc.get("miniatura"):
        raise HTTPException(404, "Sin miniatura.")
    return Response(bytes(doc["miniatura"]), media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/predicciones.csv")
async def exportar_csv(uid: int = Depends(usuario)):
    filas = await db().predicciones.find({"usuario_id": uid}, CAMPOS_PUBLICOS).sort("id", 1).to_list()
    b = io.StringIO()
    w = csv.writer(b)
    w.writerow(["id", "fecha_utc", "origen", "archivo", "ancho", "alto", "prediccion_ia", "confianza",
                "prob_sigatoka", "prob_no_hoja", "advertencia", "clase_real", "acierto", "tipo"])
    for f in filas:
        real, pred = f["clase_real"], f["clase_predicha"]
        tipo = ""
        if real in ("sana", "sigatoka") and pred in ("sana", "sigatoka"):
            tipo = {("sigatoka", "sigatoka"): "VP", ("sigatoka", "sana"): "FN",
                    ("sana", "sigatoka"): "FP", ("sana", "sana"): "VN"}[(real, pred)]
        w.writerow([f["id"], f["fecha"], f["origen"], f["nombre_archivo"] or "", f["ancho"], f["alto"], pred,
                    f["confianza"], f["prob_sigatoka"], f["prob_no_hoja"], f["advertencia"] or "",
                    real or "", "" if real is None else int(real == pred), tipo])
    return StreamingResponse(iter(["﻿" + b.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": 'attachment; filename="predicciones_sigatoka.csv"'})


@app.get("/salud")
async def salud():
    await estado["mongo"].admin.command("ping")
    return {"ok": True, "servicio": "historial", "circuito_diagnostico": estado["diagnostico"].breaker.estado}
