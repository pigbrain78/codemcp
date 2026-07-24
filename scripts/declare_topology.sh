#!/bin/sh
# Declares the platform's own topology to the Digital Twin so /impact and
# /probe know the real dependency graph from day one.
#
# Usage:
#   ./scripts/declare_topology.sh                    # localhost ports (bare processes)
#   TWIN_URL=http://localhost:8009 BASE=http://localhost ./scripts/declare_topology.sh
#   TWIN_URL=http://twin:8009 BASE_MODE=compose ./scripts/declare_topology.sh
#
# BASE_MODE=compose uses docker-compose service hostnames instead of
# localhost ports.
set -eu

TWIN_URL="${TWIN_URL:-http://localhost:8009}"
BASE_MODE="${BASE_MODE:-local}"

if [ "$BASE_MODE" = "compose" ]; then
  LEDGER=http://ledger:8001;    GATEWAY=http://gateway:8080
  CAPTURE=http://capture:8002;  KNOWLEDGE=http://knowledge:8003
  NOTION=http://notion:8004;    ORCH=http://orchestrator:8005
  RELAY=http://relay:8006;      EVOLUTION=http://evolution:8007
  GHOST=http://ghost:8008;      TWIN=http://twin:8009
  HEARTBEAT=http://heartbeat:8010
  DASHBOARD=http://dashboard:8011
else
  BASE="${BASE:-http://localhost}"
  LEDGER=$BASE:8001;    GATEWAY=$BASE:8080
  CAPTURE=$BASE:8002;   KNOWLEDGE=$BASE:8003
  NOTION=$BASE:8004;    ORCH=$BASE:8005
  RELAY=$BASE:8006;     EVOLUTION=$BASE:8007
  GHOST=$BASE:8008;     TWIN=$BASE:8009
  HEARTBEAT=$BASE:8010
  DASHBOARD=$BASE:8011
fi

declare_service() {
  curl -sS -X POST "$TWIN_URL/services" \
    -H 'content-type: application/json' \
    -d "$1" > /dev/null
  echo "declared: $1"
}

declare_service "{\"name\":\"ledger\",\"health_url\":\"$LEDGER/health\"}"
declare_service "{\"name\":\"gateway\",\"depends_on\":[\"ledger\"],\"health_url\":\"$GATEWAY/health\"}"
declare_service "{\"name\":\"pocket-os-capture-api\",\"depends_on\":[\"gateway\"],\"health_url\":\"$CAPTURE/health\"}"
declare_service "{\"name\":\"knowledge-graph\",\"depends_on\":[\"gateway\"],\"health_url\":\"$KNOWLEDGE/health\"}"
declare_service "{\"name\":\"manus-prime\",\"depends_on\":[\"gateway\"],\"health_url\":\"$ORCH/health\"}"
declare_service "{\"name\":\"notion-sync\",\"depends_on\":[\"knowledge-graph\"],\"health_url\":\"$NOTION/health\"}"
declare_service "{\"name\":\"event-relay\",\"depends_on\":[\"gateway\"],\"health_url\":\"$RELAY/health\"}"
declare_service "{\"name\":\"evolution-engine\",\"depends_on\":[\"gateway\"],\"health_url\":\"$EVOLUTION/health\"}"
declare_service "{\"name\":\"ghost-team\",\"depends_on\":[\"gateway\"],\"health_url\":\"$GHOST/health\"}"
declare_service "{\"name\":\"digital-twin\",\"depends_on\":[\"gateway\"],\"health_url\":\"$TWIN/health\"}"
declare_service "{\"name\":\"heartbeat\",\"depends_on\":[\"knowledge-graph\",\"digital-twin\",\"ghost-team\",\"event-relay\",\"evolution-engine\"],\"health_url\":\"$HEARTBEAT/health\"}"
declare_service "{\"name\":\"dashboard\",\"depends_on\":[\"gateway\",\"digital-twin\",\"ghost-team\",\"evolution-engine\",\"heartbeat\"],\"health_url\":\"$DASHBOARD/health\"}"

echo "topology declared to $TWIN_URL"
