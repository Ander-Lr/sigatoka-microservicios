"""Predictor del detector de Sigatoka negra: preprocesamiento, modelo ONNX y detector de imágenes ajenas.

Es la parte de inferencia de la librería de entrenamiento original. El preprocesamiento es idéntico al usado
al entrenar el modelo, por eso no debe modificarse sin reentrenar.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

CLASES = ["sana", "sigatoka", "otro"]    # 1 = positiva (enferma); 2 = no es hoja de banano
NOMBRES = {"sana": "Hoja sana", "sigatoka": "Sigatoka negra", "otro": "No es una hoja de banano"}
MEAN = (0.485, 0.456, 0.406)
STD = (0.229, 0.224, 0.225)
LADO_CACHE = 640                         # tamaño de la caché usada para entrenar


# ----------------------------------------------------------------------------
# Preprocesamiento: idéntico al del entrenamiento
# ----------------------------------------------------------------------------
def cargar_imagen(origen) -> Image.Image:
    """Acepta ruta, bytes, PIL.Image o arreglo numpy (RGB, o BGR si viene de OpenCV
    se debe convertir antes). Corrige la orientación EXIF."""
    if isinstance(origen, Image.Image):
        im = origen
    elif isinstance(origen, np.ndarray):
        im = Image.fromarray(origen)
    elif isinstance(origen, (bytes, bytearray)):
        import io
        im = Image.open(io.BytesIO(origen))
        im.draft("RGB", (2 * LADO_CACHE, 2 * LADO_CACHE))   # igual que con rutas (API == CLI)
    else:
        im = Image.open(origen)
        im.draft("RGB", (2 * LADO_CACHE, 2 * LADO_CACHE))   # decodificación rápida (igual que la caché)
    im = ImageOps.exif_transpose(im).convert("RGB")
    # mismo camino que en el entrenamiento: primero se reduce a 640 px (lado mayor) con LANCZOS
    if max(im.size) > LADO_CACHE:
        im.thumbnail((LADO_CACHE, LADO_CACHE), Image.LANCZOS)
    return im


def vistas_inferencia(im: Image.Image, size: int, tta: bool = False) -> list[Image.Image]:
    """Vista principal: lado corto a `size` y recorte central cuadrado
    (elimina la relación de aspecto, que en este dataset es un atajo).
    Con tta=True agrega volteos horizontal y vertical y promedia (más estable)."""
    w, h = im.size
    s = size / min(w, h)
    r = im.resize((max(size, round(w * s)), max(size, round(h * s))), Image.BICUBIC)
    x0, y0 = (r.width - size) // 2, (r.height - size) // 2
    vistas = [r.crop((x0, y0, x0 + size, y0 + size))]
    if tta:
        vistas.append(vistas[0].transpose(Image.FLIP_LEFT_RIGHT))
        vistas.append(vistas[0].transpose(Image.FLIP_TOP_BOTTOM))
    return vistas


def a_tensor_np(vistas: list[Image.Image]) -> np.ndarray:
    x = np.stack([np.asarray(v, dtype=np.float32) / 255.0 for v in vistas])
    x = (x - np.array(MEAN, np.float32)) / np.array(STD, np.float32)
    return x.transpose(0, 3, 1, 2).copy()     # NCHW


# ----------------------------------------------------------------------------
# Detector de "fuera de distribución" (¿es realmente una hoja de banano?)
# Distancia de Mahalanobis sobre las características internas del modelo.
# ----------------------------------------------------------------------------
@dataclass
class DetectorOOD:
    medias: list            # media por clase  [C, D]
    precision: list         # inversa de la covarianza compartida [D, D]
    umbral: float           # distancia máxima aceptada
    percentil: float

    def distancia(self, feats: np.ndarray) -> np.ndarray:
        m = np.asarray(self.medias)
        p = np.asarray(self.precision)
        d = feats[:, None, :].astype(np.float64) - m[None]          # [N, C, D]
        md = ((d @ p) * d).sum(-1)                                   # forma cuadrática por clase
        return np.sqrt(np.maximum(md.min(1), 0))

    @staticmethod
    def cargar(ruta):
        z = np.load(ruta)
        return DetectorOOD(z["medias"], z["precision"], float(z["umbral"]), float(z["percentil"]))


# ----------------------------------------------------------------------------
# Predictor con ONNX Runtime: no necesita PyTorch ni GPU
# ----------------------------------------------------------------------------
class PredictorSigatoka:
    """Uso:
        p = PredictorSigatoka("modelo")
        r = p.predecir("foto.jpg")
        r["diagnostico"], r["prob_sigatoka"], r["es_hoja_banano"]
    """

    def __init__(self, carpeta="modelo"):
        carpeta = Path(carpeta)
        self.meta = json.loads((carpeta / "metadata.json").read_text())
        self.size = self.meta["img_size"]
        self.umbral_decision = self.meta["umbral_decision"]
        self.ood = DetectorOOD.cargar(carpeta / "detector_ood.npz")
        import onnxruntime as ort
        prov = [p for p in ("CUDAExecutionProvider", "CPUExecutionProvider")
                if p in ort.get_available_providers()]
        self.sess = ort.InferenceSession(str(carpeta / "modelo_sigatoka.onnx"), providers=prov)

    def _ejecutar(self, x: np.ndarray):
        logits, feats = self.sess.run(["logits", "features"], {"imagen": x})
        return logits, feats

    def predecir(self, origen, tta: bool = False) -> dict:
        im = cargar_imagen(origen)
        x = a_tensor_np(vistas_inferencia(im, self.size, tta))
        logits, feats = self._ejecutar(x)
        e = np.exp(logits - logits.max(1, keepdims=True))
        probs = (e / e.sum(1, keepdims=True)).mean(0)
        p_otro = float(probs[2])
        p_sig = float(probs[1] / max(probs[0] + probs[1], 1e-9))   # P(sigatoka | es hoja)
        dist = float(self.ood.distancia(feats[:1])[0])            # distancia a las hojas de entrenamiento
        # Dos niveles: hasta 'umbral' la imagen es típica; entre 'umbral' y 'umbral_alto' se diagnostica
        # con advertencia (imagen poco habitual); por encima, no se considera hoja de banano.
        umbral_alto = self.ood.umbral * self.meta.get("factor_umbral_ood_alto", 1.3)
        es_hoja = (p_otro < self.meta["umbral_otro"]) and (dist <= umbral_alto)
        advertencia = None
        if es_hoja:
            clase = "sigatoka" if p_sig >= self.umbral_decision else "sana"
            confianza = p_sig if clase == "sigatoka" else 1 - p_sig
            if dist > self.ood.umbral:
                advertencia = "Imagen poco habitual: verifique que sea una hoja de banano y repita la foto."
        else:
            clase, confianza = "otro", max(p_otro, 0.5)
        return {
            "clase": clase,
            "diagnostico": NOMBRES[clase],
            "confianza": round(float(confianza), 4),
            "prob_sigatoka": round(p_sig, 4),        # condicionada a que sea hoja
            "prob_no_hoja": round(p_otro, 4),
            "es_hoja_banano": bool(es_hoja),
            "advertencia": advertencia,
            "distancia_ood": round(dist, 2),
            "umbral_ood": round(self.ood.umbral, 2),
            "umbral_ood_alto": round(umbral_alto, 2),
        }
