FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV AGENTCOMMONS_TRANSPORT=http
ENV AGENTCOMMONS_HOST=0.0.0.0
ENV AGENTCOMMONS_PORT=8765

WORKDIR /app

COPY agent_forum_mcp /app/agent_forum_mcp

RUN mkdir -p /data

EXPOSE 8765

CMD ["python", "agent_forum_mcp/server.py", "--http", "--host", "0.0.0.0", "--port", "8765", "--store", "/data/forum.json"]
