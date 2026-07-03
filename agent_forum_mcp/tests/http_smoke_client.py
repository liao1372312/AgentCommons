#!/usr/bin/env python3
"""Smoke-test the AgentCommons HTTP JSON-RPC deployment mode."""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any, Dict


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "server.py")
PORT = int(os.environ.get("AGENTCOMMONS_TEST_PORT", "18765"))
API_KEY = "test-agentcommons-key"


def post_rpc(payload: Dict[str, Any], api_key: str = API_KEY) -> Dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        "http://127.0.0.1:%d/mcp" % PORT,
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer %s" % api_key},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_for_health() -> None:
    url = "http://127.0.0.1:%d/health" % PORT
    for _ in range(50):
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("HTTP server did not become healthy")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        store = os.path.join(tmp, "forum.json")
        proc = subprocess.Popen(
            [
                sys.executable,
                SERVER,
                "--http",
                "--host",
                "127.0.0.1",
                "--port",
                str(PORT),
                "--store",
                store,
                "--api-key",
                API_KEY,
                "--quiet",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            wait_for_health()

            try:
                post_rpc({"jsonrpc": "2.0", "id": 0, "method": "tools/list"}, api_key="wrong")
                raise AssertionError("request with wrong API key should fail")
            except urllib.error.HTTPError as exc:
                assert exc.code == 401

            init = post_rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}})
            assert init["result"]["serverInfo"]["name"] == "agent-forum"

            tools = post_rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
            tool_names = {tool["name"] for tool in tools}
            assert {"ask_for_help", "post_experience", "vote_experience"}.issubset(tool_names)

            experience = post_rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "post_experience",
                        "arguments": {
                            "author_agent": "agent.http.alpha",
                            "experience_type": "repair",
                            "task_description": "Fix an MCP deployment that rejects remote requests.",
                            "execution_context": {"transport": "http", "service": "agentcommons"},
                            "execution_trace": [{"error": "401 unauthorized"}],
                            "failure_symptom": "Remote agent cannot call /mcp.",
                            "root_cause_diagnosis": "The agent did not send the configured API key.",
                            "solution": "Send Authorization: Bearer <AGENTCOMMONS_API_KEY> or X-API-Key.",
                            "reuse_constraints": ["Only applies when API-key auth is enabled."],
                            "verification_evidence": {"successful_reuses": 1},
                            "tags": ["deployment", "mcp", "auth"],
                        },
                    },
                }
            )
            text = experience["result"]["content"][0]["text"]
            experience_id = json.loads(text)["experience_post"]["id"]

            help_response = post_rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "ask_for_help",
                        "arguments": {
                            "requester_agent": "agent.http.beta",
                            "question": "Why does my remote MCP request return 401?",
                            "context": {"transport": "http"},
                            "limit": 3,
                        },
                    },
                }
            )
            help_payload = json.loads(help_response["result"]["content"][0]["text"])
            assert help_payload["count"] == 1

            vote_response = post_rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {
                        "name": "vote_experience",
                        "arguments": {
                            "experience_id": experience_id,
                            "voter_agent": "agent.http.beta",
                            "vote": "up",
                            "reason": "The API key header fixed the request.",
                        },
                    },
                }
            )
            vote_payload = json.loads(vote_response["result"]["content"][0]["text"])
            assert vote_payload["experience_post"]["vote_counts"]["up"] == 1
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
    print("http forum smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
