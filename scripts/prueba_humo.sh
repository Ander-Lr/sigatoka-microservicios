#!/usr/bin/env bash
# Prueba de humo de punta a punta contra el API Gateway.
# Recorre registro, análisis, revisión, historial con métricas, CSV y la Saga de eliminación de cuenta.
#   ./scripts/prueba_humo.sh [URL]      por defecto http://localhost:8000
set -euo pipefail
URL="${1:-http://localhost:8000}"
RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
COOKIES="$(mktemp)"; trap 'rm -f "$COOKIES"' EXIT
H=(-H "X-Requested-With: fetch")
USUARIO="humo$RANDOM"

paso() { printf '  %-45s' "$1"; }
ok() { echo "ok"; }
falla() { echo "FALLA: $1"; exit 1; }
campo() { python3 -c "import sys, json; print(json.load(sys.stdin)$1)"; }

paso "Esperando servicios sanos en Consul"
for _ in $(seq 120); do curl -sf "$URL/salud" >/dev/null 2>&1 && break; sleep 1; done
curl -sf "$URL/salud" >/dev/null || falla "algún servicio no está sano"; ok

paso "Interfaz web a través del gateway"
[[ $(curl -s -o /dev/null -w '%{http_code}' "$URL/") == 200 ]] || falla "la página no carga"; ok

paso "Registro de usuario"
curl -sf -c "$COOKIES" "${H[@]}" -H 'Content-Type: application/json' -X POST "$URL/api/registro" \
  -d "{\"nombre\":\"Prueba\",\"usuario\":\"$USUARIO\",\"contrasena\":\"clave12345\"}" >/dev/null || falla "registro"; ok

paso "Análisis de una hoja con Sigatoka"
CLASE=$(curl -sf -b "$COOKIES" "${H[@]}" -F "imagen=@$RAIZ/servicios/web/static/ejemplos/sigatoka.jpg" \
  -F origen=foto "$URL/api/predicciones" | campo "['clase']")
[[ $CLASE == sigatoka ]] || falla "se esperaba sigatoka y llegó $CLASE"; ok

paso "Análisis de una hoja sana"
CLASE=$(curl -sf -b "$COOKIES" "${H[@]}" -F "imagen=@$RAIZ/servicios/web/static/ejemplos/sana.jpg" \
  -F origen=foto "$URL/api/predicciones" | campo "['clase']")
[[ $CLASE == sana ]] || falla "se esperaba sana y llegó $CLASE"; ok

paso "Revisión manual y métricas al día"
ID=$(curl -sf -b "$COOKIES" "$URL/api/predicciones" | campo "['predicciones'][-1]['id']")
VP=$(curl -sf -b "$COOKIES" "${H[@]}" -H 'Content-Type: application/json' -X PATCH "$URL/api/predicciones/$ID" \
  -d '{"clase_real":"sigatoka"}' | campo "['resumen']['VP']")
[[ $VP == 1 ]] || falla "VP esperado 1, llegó $VP"; ok

paso "Historial compuesto con resumen"
TOTAL=$(curl -sf -b "$COOKIES" "$URL/api/predicciones" | campo "['resumen']['total']")
[[ $TOTAL == 2 ]] || falla "total esperado 2, llegó $TOTAL"; ok

paso "Exportación CSV"
[[ $(curl -sf -b "$COOKIES" "$URL/api/predicciones.csv" | wc -l) -eq 3 ]] || falla "el CSV no tiene 2 filas"; ok

paso "Saga de eliminación de cuenta"
SAGA=$(curl -sf -b "$COOKIES" "${H[@]}" -X DELETE "$URL/api/cuenta" | campo "['saga_id']")
for _ in $(seq 20); do
  ESTADO=$(curl -sf "$URL/api/cuenta/eliminacion/$SAGA" | campo "['estado']")
  [[ $ESTADO != en_curso ]] && break; sleep 0.5
done
[[ $ESTADO == completada ]] || falla "la Saga terminó en estado $ESTADO"; ok

echo "Todas las pruebas pasaron."
