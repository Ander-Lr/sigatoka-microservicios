"""Descubrimiento de servicios con Consul.

Cada instancia se registra al arrancar con un chequeo de salud HTTP y se retira al apagarse.
Los clientes preguntan a Consul por las instancias sanas de un servicio y reparten la carga entre ellas.
"""
import asyncio
import logging
import os
import random
import socket
import time

import httpx

CONSUL = os.getenv("CONSUL_URL", "http://consul:8500")
log = logging.getLogger("descubrimiento")


class ServicioNoDisponible(Exception):
    """No hay instancias sanas del servicio solicitado."""


class Registro:
    def __init__(self, nombre: str, puerto: int = 8000):
        self.nombre, self.puerto = nombre, puerto
        self.id = f"{nombre}-{socket.gethostname()}"

    async def registrar(self):
        ip = socket.gethostbyname(socket.gethostname())
        cuerpo = {
            "ID": self.id, "Name": self.nombre, "Address": ip, "Port": self.puerto,
            "Check": {"HTTP": f"http://{ip}:{self.puerto}/salud", "Interval": "5s", "Timeout": "2s",
                      "DeregisterCriticalServiceAfter": "1m"},
        }
        for _ in range(30):
            try:
                async with httpx.AsyncClient(timeout=3) as c:
                    (await c.put(f"{CONSUL}/v1/agent/service/register", json=cuerpo)).raise_for_status()
                log.info("Registrado en Consul como %s (%s:%s)", self.id, ip, self.puerto)
                return
            except httpx.HTTPError:
                await asyncio.sleep(2)
        log.error("No se pudo registrar %s en Consul", self.id)

    async def retirar(self):
        try:
            async with httpx.AsyncClient(timeout=3) as c:
                await c.put(f"{CONSUL}/v1/agent/service/deregister/{self.id}")
        except httpx.HTTPError:
            pass


class Resolutor:
    """Traduce un nombre de servicio a la URL de una instancia sana. Guarda la lista unos segundos."""

    def __init__(self, ttl: float = 5.0):
        self.ttl = ttl
        self._cache: dict[str, tuple[float, list[str]]] = {}
        self._http = httpx.AsyncClient(timeout=3)

    async def instancias(self, nombre: str) -> list[str]:
        t, urls = self._cache.get(nombre, (0.0, []))
        if time.monotonic() - t < self.ttl and urls:
            return urls
        try:
            r = await self._http.get(f"{CONSUL}/v1/health/service/{nombre}", params={"passing": "true"})
            r.raise_for_status()
            urls = [f"http://{s['Service']['Address']}:{s['Service']['Port']}" for s in r.json()]
        except httpx.HTTPError:
            log.warning("Consul no responde; se usa la última lista conocida de %s", nombre)
        self._cache[nombre] = (time.monotonic(), urls)
        return urls

    async def url(self, nombre: str) -> str:
        urls = await self.instancias(nombre)
        if not urls:
            raise ServicioNoDisponible(nombre)
        return random.choice(urls)
