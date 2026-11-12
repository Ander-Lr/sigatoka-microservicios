"""Servicio de diagnóstico: valida la imagen y ejecuta el modelo ONNX. No guarda nada (sin estado).

Al no tener estado se puede escalar horizontalmente:  docker compose up -d --scale diagnostico=3
"""
import base64
import io
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from comun.descubrimiento import Registro
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from PIL import Image, ImageOps
from predictor import PredictorSigatoka

log = logging.getLogger("diagnostico")
CARPETA_MODELO = Path(os.getenv("SIGATOKA_MODELO", "/app/modelo"))
MAX_BYTES = int(float(os.getenv("SIGATOKA_MAX_MB", "25")) * 1024 * 1024)
LADO_MINIMO = 224
FORMATOS = {"JPEG": "JPG", "PNG": "PNG", "WEBP": "WEBP"}
Image.MAX_IMAGE_PIXELS = 60_000_000             # protege contra "bombas de descompresión"
estado: dict = {}


@asynccontextmanager
async def ciclo_de_vida(app: FastAPI):
    t0 = time.perf_counter()
    estado["predictor"] = PredictorSigatoka(CARPETA_MODELO)
    log.info("Modelo cargado en %.1f s", time.perf_counter() - t0)
    estado["registro"] = Registro("diagnostico")
    await estado["registro"].registrar()
    yield
    await estado["registro"].retirar()


app = FastAPI(title="Servicio de diagnóstico", version="3.0", lifespan=ciclo_de_vida)


def leer_imagen(imagen: UploadFile) -> tuple[bytes, Image.Image]:
    datos = imagen.file.read(MAX_BYTES + 1)
    if not datos:
        raise HTTPException(400, "El archivo está vacío.")
    if len(datos) > MAX_BYTES:
        raise HTTPException(413, f"La imagen pesa más de {MAX_BYTES // 1024 // 1024} MB.")
    try:
        im = Image.open(io.BytesIO(datos))
        formato = im.format
        im = ImageOps.exif_transpose(im)
        im.load()
    except (OSError, ValueError, Image.DecompressionBombError):
        raise HTTPException(400, "No se pudo leer la imagen. Use un archivo JPG, PNG o WEBP.") from None
    if formato not in FORMATOS:
        raise HTTPException(415, f"Formato {formato} no admitido. Use JPG, PNG o WEBP.")
    if min(im.size) < LADO_MINIMO:
        raise HTTPException(422, f"La imagen es muy pequeña ({im.width}×{im.height} px). "
                                 f"El mínimo es {LADO_MINIMO}×{LADO_MINIMO} px.")
    return datos, im


def miniatura(im: Image.Image) -> str:
    m = im.convert("RGB")
    m.thumbnail((320, 320))
    b = io.BytesIO()
    m.save(b, "JPEG", quality=80)
    return base64.b64encode(b.getvalue()).decode()


def analizar(datos: bytes, tta: bool) -> dict:
    t0 = time.perf_counter()
    r = estado["predictor"].predecir(datos, tta=tta)
    r["ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return r


# 'def' (no async): FastAPI los ejecuta en un hilo aparte y la inferencia no bloquea el servidor
@app.post("/interno/diagnosticos")
def diagnosticar(imagen: UploadFile = File(...), tta: bool = Query(True)):
    """Uso interno del servicio de historial: devuelve además la miniatura y las dimensiones."""
    datos, im = leer_imagen(imagen)
    r = analizar(datos, tta)
    r.update(ancho=im.width, alto=im.height, miniatura_b64=miniatura(im),
             version_modelo=estado["predictor"].meta.get("version", "?"))
    return r


@app.post("/predecir", tags=["integración"])
def predecir(imagen: UploadFile = File(...), tta: bool = Query(False)):
    """Análisis sin sesión y sin guardar, para integraciones externas."""
    datos, _ = leer_imagen(imagen)
    return analizar(datos, tta)


@app.get("/api/requisitos")
def requisitos():
    return {"formatos": list(FORMATOS.values()), "max_mb": MAX_BYTES // 1024 // 1024,
            "lado_minimo": LADO_MINIMO, "entrada_modelo": "224×224 (cuadrado central de la imagen)"}


@app.get("/salud")
def salud():
    p = estado.get("predictor")
    if p is None:
        raise HTTPException(503, "Modelo no cargado")
    return {"ok": True, "servicio": "diagnostico", "modelo": p.meta["arquitectura"], "version": p.meta["version"],
            "clases": p.meta["clases"], "max_mb": MAX_BYTES / 1024 / 1024, "lado_minimo": LADO_MINIMO}
