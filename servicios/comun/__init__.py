"""Librería compartida por los microservicios: descubrimiento (Consul), Circuit Breaker y mensajería (RabbitMQ)."""
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
