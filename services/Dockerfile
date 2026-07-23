# Shared image for all Project Sovereign Python services (ledger, capture,
# knowledge, notion, orchestrator). Which service runs is chosen by the
# container command, e.g.:
#
#   docker run <image> uvicorn services.ledger.app:app --host 0.0.0.0 --port 8001
#
# Built from the repo root: docker build -f services/Dockerfile .
FROM python:3.12-slim

RUN pip install --no-cache-dir "fastapi>=0.115" "uvicorn>=0.28" "httpx>=0.28"

WORKDIR /app
COPY services /app/services

# Default to the ledger; docker-compose overrides the command per service.
CMD ["uvicorn", "services.ledger.app:app", "--host", "0.0.0.0", "--port", "8001"]
