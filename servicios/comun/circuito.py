"""Circuit Breaker y cliente HTTP entre servicios.

CERRADO       las llamadas pasan normalmente
ABIERTO       tras N fallos seguidos se corta el paso y se responde de inmediato sin llamar al servicio
SEMIABIERTO   pasado el tiempo de espera se deja pasar una llamada de prueba: si responde, se cierra
"""
import logging
import time

import httpx

from .descubrimiento import Resolutor, ServicioNoDisponible

log = logging.getLogger("circuito")


class CircuitoAbierto(Exception):
    pass


class ErrorServicio(Exception):
    """El servicio respondió con un error 5xx."""

    def __init__(self, respuesta: httpx.Response):
        super().__init__(f"{respuesta.status_code}")
        self.respuesta = respuesta


class CircuitBreaker:
    def __init__(self, nombre: str, umbral_fallos: int = 3, espera_s: float = 15.0):
        self.nombre, self.umbral, self.espera = nombre, umbral_fallos, espera_s
        self.estado, self.fallos, self.abierto_desde = "CERRADO", 0, 0.0

    async def llamar(self, funcion):
        if self.estado == "ABIERTO":
            if time.monotonic() - self.abierto_desde < self.espera:
                raise CircuitoAbierto(self.nombre)
            self.estado = "SEMIABIERTO"
            log.info("Circuito %s SEMIABIERTO: llamada de prueba", self.nombre)
        try:
            resultado = await funcion()
        except (httpx.TransportError, ErrorServicio, ServicioNoDisponible):
            self._fallo()
            raise
        if self.estado != "CERRADO" or self.fallos:
            log.info("Circuito %s CERRADO", self.nombre)
        self.estado, self.fallos = "CERRADO", 0
        return resultado

    def _fallo(self):
        self.fallos += 1
        if self.estado == "SEMIABIERTO" or self.fallos >= self.umbral:
            self.estado, self.abierto_desde = "ABIERTO", time.monotonic()
            log.warning("Circuito %s ABIERTO tras %d fallos", self.nombre, self.fallos)


class ClienteServicio:
    """Llama a un servicio por su nombre lógico: Consul da la instancia y el Circuit Breaker la protege."""

    def __init__(self, nombre: str, resolutor: Resolutor, timeout: float = 10.0, **kw_breaker):
        self.nombre, self.resolutor = nombre, resolutor
        self.breaker = CircuitBreaker(nombre, **kw_breaker)
        self.http = httpx.AsyncClient(timeout=timeout)

    async def peticion(self, metodo: str, ruta: str, **kw) -> httpx.Response:
        async def hacer():
            base = await self.resolutor.url(self.nombre)
            r = await self.http.request(metodo, base + ruta, **kw)
            # Un servicio sano que avisa que SU dependencia falló no cuenta como fallo propio
            if r.status_code >= 500 and "x-fallo-dependencia" not in r.headers:
                raise ErrorServicio(r)
            return r
        return await self.breaker.llamar(hacer)
