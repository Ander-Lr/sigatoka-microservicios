#!/usr/bin/env bash
# Levanta el detector de Sigatoka negra con Docker Compose.
#   ./iniciar.sh            construye, levanta todo y abre http://localhost:8000
#   ./iniciar.sh --detener  detiene los contenedores (los datos se conservan en volúmenes)
set -euo pipefail
cd "$(dirname "$0")"
[[ -f .env ]] || cp .env.example .env
if [[ "${1:-}" == "--detener" ]]; then exec docker compose down; fi

docker compose up -d --build
PUERTO=$(grep -E '^PUERTO=' .env | cut -d= -f2)
URL="http://localhost:${PUERTO:-8000}"
echo "Esperando a que los servicios se registren en Consul…"
for _ in $(seq 90); do curl -sf "$URL/salud" >/dev/null 2>&1 && break; sleep 1; done
echo "  Aplicación  $URL"
echo "  Consul      http://localhost:8500"
echo "  RabbitMQ    http://localhost:15672"
command -v xdg-open >/dev/null && xdg-open "$URL" >/dev/null 2>&1 || true
