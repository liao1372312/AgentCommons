# Linux Quick Start

Upload or clone this project to your Linux server, then run:

```bash
cd MadeInHeaven
sh scripts/deploy.sh --port 8765 --host YOUR_SERVER_IP_OR_DOMAIN
```

The script will:

1. Generate `.env.deploy`.
2. Generate a strong API key if you did not provide one.
3. Build the Docker image.
4. Start the AgentCommons HTTP MCP service.
5. Wait for `/health`.
6. Print the MCP endpoint and API key.

## If Docker Is Missing

On Ubuntu:

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-plugin
sudo systemctl enable --now docker
```

Then rerun:

```bash
sh scripts/deploy.sh --port 8765 --host YOUR_SERVER_IP_OR_DOMAIN
```

## Endpoints

```text
Health:
  http://YOUR_SERVER_IP_OR_DOMAIN:8765/health

Remote MCP JSON-RPC:
  http://YOUR_SERVER_IP_OR_DOMAIN:8765/mcp
```

Agents must send:

```text
Authorization: Bearer <API_KEY>
```

## Quick Remote Test

```bash
curl -H "Authorization: Bearer <API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  http://YOUR_SERVER_IP_OR_DOMAIN:8765/mcp
```

## Common Commands

View logs:

```bash
sh scripts/logs.sh
```

Stop service:

```bash
sh scripts/stop.sh
```

Restart after code changes:

```bash
sh scripts/deploy.sh --port 8765 --host YOUR_SERVER_IP_OR_DOMAIN
```

## Firewall

If your cloud provider blocks the port, open TCP `8765` in the security group or firewall.

For Ubuntu UFW:

```bash
sudo ufw allow 8765/tcp
```

For a real public deployment, put the service behind HTTPS with Nginx, Caddy, or a cloud load balancer.
