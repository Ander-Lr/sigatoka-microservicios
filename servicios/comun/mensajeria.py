"""Mensajería asíncrona con RabbitMQ.

Un exchange de tipo topic reparte los eventos de dominio. Cada servicio declara su propia cola durable
y la enlaza a las claves que le interesan. Un mensaje que falla dos veces va a la cola de fallidos.
"""
import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone

import aio_pika

URL = os.getenv("RABBITMQ_URL", "amqp://guest:guest@rabbitmq/")
EXCHANGE = "sigatoka.eventos"
DLX = "sigatoka.fallidos"
log = logging.getLogger("mensajeria")


def ahora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Bus:
    def __init__(self, servicio: str):
        self.servicio = servicio
        self.conexion = self.canal = self.exchange = None

    async def conectar(self):
        for intento in range(30):
            try:
                self.conexion = await aio_pika.connect_robust(URL)
                break
            except (aio_pika.exceptions.AMQPConnectionError, OSError):
                log.info("RabbitMQ no disponible todavía (intento %d)", intento + 1)
                await asyncio.sleep(2)
        else:
            raise RuntimeError("No se pudo conectar a RabbitMQ")
        self.canal = await self.conexion.channel()
        await self.canal.set_qos(prefetch_count=10)
        self.exchange = await self.canal.declare_exchange(EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True)
        dlx = await self.canal.declare_exchange(DLX, aio_pika.ExchangeType.FANOUT, durable=True)
        fallidos = await self.canal.declare_queue("sigatoka.fallidos", durable=True)
        await fallidos.bind(dlx)

    async def publicar(self, tipo: str, datos: dict) -> str:
        evento = {"evento_id": str(uuid.uuid4()), "tipo": tipo, "origen": self.servicio, "fecha": ahora(),
                  "datos": datos}
        mensaje = aio_pika.Message(json.dumps(evento).encode(), content_type="application/json",
                                   message_id=evento["evento_id"],
                                   delivery_mode=aio_pika.DeliveryMode.PERSISTENT)
        await self.exchange.publish(mensaje, routing_key=tipo)
        log.info("Publicado %s %s", tipo, datos)
        return evento["evento_id"]

    async def suscribir(self, cola: str, claves: list[str], manejador):
        """manejador(evento: dict) es una corrutina. Si lanza excepción el mensaje se reintenta una vez."""
        q = await self.canal.declare_queue(cola, durable=True, arguments={"x-dead-letter-exchange": DLX})
        for clave in claves:
            await q.bind(self.exchange, routing_key=clave)

        async def procesar(mensaje: aio_pika.abc.AbstractIncomingMessage):
            async with mensaje.process(requeue=not mensaje.redelivered):
                evento = json.loads(mensaje.body)
                log.info("Recibido %s", evento["tipo"])
                await manejador(evento)

        await q.consume(procesar)

    async def cerrar(self):
        if self.conexion:
            await self.conexion.close()
