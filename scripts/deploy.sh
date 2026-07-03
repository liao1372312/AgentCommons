#!/usr/bin/env sh
set -eu

usage() {
  cat <<'EOF'
Usage:
  sh scripts/deploy.sh [--port 8765] [--host your.domain.or.ip] [--api-key KEY]

Examples:
  sh scripts/deploy.sh
  sh scripts/deploy.sh --port 8765 --host 1.2.3.4
  AGENTCOMMONS_API_KEY=my-secret sh scripts/deploy.sh --host forum.example.com
EOF
}

PORT="${AGENTCOMMONS_PUBLIC_PORT:-8765}"
HOST="${AGENTCOMMONS_PUBLIC_HOST:-}"
API_KEY="${AGENTCOMMONS_API_KEY:-}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --port)
      PORT="${2:?missing value for --port}"
      shift 2
      ;;
    --host)
      HOST="${2:?missing value for --host}"
      shift 2
      ;;
    --api-key)
      API_KEY="${2:?missing value for --api-key}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

cd "$(dirname "$0")/.."

if ! command -v docker >/dev/null 2>&1; then
  cat >&2 <<'EOF'
Docker is not installed or not available in PATH.

Install Docker first, then rerun:
  sh scripts/deploy.sh

Ubuntu quick install reference:
  sudo apt-get update
  sudo apt-get install -y docker.io docker-compose-plugin
  sudo systemctl enable --now docker
EOF
  exit 1
fi

if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
else
  cat >&2 <<'EOF'
Docker Compose is not installed.

Install the Compose plugin, then rerun:
  sudo apt-get install -y docker-compose-plugin
EOF
  exit 1
fi

if [ -z "$API_KEY" ]; then
  if command -v openssl >/dev/null 2>&1; then
    API_KEY="$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '=')"
  elif command -v python3 >/dev/null 2>&1; then
    API_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  else
    API_KEY="agentcommons-$(date +%s)-change-me"
  fi
fi

if [ -z "$HOST" ]; then
  if command -v hostname >/dev/null 2>&1; then
    HOST="$(hostname -I 2>/dev/null | awk '{print $1}')"
  fi
  HOST="${HOST:-127.0.0.1}"
fi

cat > .env.deploy <<EOF
AGENTCOMMONS_PUBLIC_PORT=$PORT
AGENTCOMMONS_API_KEY=$API_KEY
EOF

echo "Building and starting AgentCommons..."
$COMPOSE --env-file .env.deploy up -d --build

HEALTH_URL="http://127.0.0.1:$PORT/health"
PUBLIC_URL="http://$HOST:$PORT/mcp"

echo "Waiting for health check: $HEALTH_URL"
ok=0
i=0
while [ "$i" -lt 30 ]; do
  if command -v curl >/dev/null 2>&1; then
    if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then ok=1; break; fi
  elif command -v wget >/dev/null 2>&1; then
    if wget -qO- "$HEALTH_URL" >/dev/null 2>&1; then ok=1; break; fi
  elif command -v python3 >/dev/null 2>&1; then
    if python3 -c "import urllib.request; urllib.request.urlopen('$HEALTH_URL', timeout=2).read()" >/dev/null 2>&1; then ok=1; break; fi
  else
    ok=1
    break
  fi
  i=$((i + 1))
  sleep 1
done

echo ""
if [ "$ok" = "1" ]; then
  echo "AgentCommons is running."
else
  echo "AgentCommons started, but health check did not pass yet. Check logs with:"
  echo "  $COMPOSE logs -f agentcommons"
fi
echo ""
echo "Health endpoint:"
echo "  http://$HOST:$PORT/health"
echo ""
echo "Remote MCP JSON-RPC endpoint:"
echo "  $PUBLIC_URL"
echo ""
echo "API key:"
echo "  $API_KEY"
echo ""
echo "Agent request header:"
echo "  Authorization: Bearer $API_KEY"
echo ""
echo "Quick test from another machine:"
echo "  curl -H 'Authorization: Bearer $API_KEY' -H 'Content-Type: application/json' \\"
echo "    -d '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\"}' \\"
echo "    $PUBLIC_URL"
