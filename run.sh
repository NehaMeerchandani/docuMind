#!/usr/bin/env bash
# One command to run everything:  ./run.sh
# Chat mode (HTTP/SSE vs WebSocket) is set by CHAT_TRANSPORT in .env: sse | websocket
cd "$(dirname "$0")" || exit 1
PY=venv/bin/python
export PATH="$HOME/.temporalio/bin:$PATH"
mkdir -p .logs
PIDS=()
cleanup() { echo; echo "Stopping all services..."; kill "${PIDS[@]}" 2>/dev/null; wait 2>/dev/null; exit 0; }
trap cleanup INT TERM

start() { # name, command...
  local name=$1; shift
  "$@" > ".logs/$name.log" 2>&1 &
  PIDS+=($!)
  echo "started $name (pid $!) -> .logs/$name.log"
}

# Redis (needed by Celery + Channels)
if ! redis-cli ping >/dev/null 2>&1; then start redis redis-server; sleep 1; fi

# Docker services (Kafka etc.), if the container exists
for c in docuMind-kafka qdrant; do docker start $c >/dev/null 2>&1 && echo "$c container started"; done

start temporal temporal server start-dev --db-filename temporal.db \
  --search-attribute DocumentId=Int --search-attribute SessionId=Keyword \
  --search-attribute UploadedBy=Keyword --search-attribute ProcessingStep=Keyword
sleep 5
start temporal-worker $PY manage.py run_temporal_worker
start celery venv/bin/celery -A main worker --loglevel=info
start kafka-consumer $PY manage.py consume_document_processing

echo "Mode: $(grep -E '^CHAT_TRANSPORT=' .env)"
echo "App: http://localhost:8000/admin/   (Ctrl+C stops everything)"
# daphne serves both HTTP and WebSocket
venv/bin/daphne -b 0.0.0.0 -p 8000 main.asgi:application
cleanup
