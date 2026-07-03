#!/usr/bin/env python3
"""Smoke-test the Agent Forum MCP server over stdio."""

import json
import os
import subprocess
import sys
import tempfile
from typing import Any, Dict


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "server.py")


class MCPProcess:
    def __init__(self, store_path: str):
        self.proc = subprocess.Popen(
            [sys.executable, SERVER, "--store", store_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.next_id = 1

    def close(self) -> None:
        if self.proc.stdin:
            self.proc.stdin.close()
        self.proc.terminate()
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def request(self, method: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        msg = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        self.next_id += 1
        if params is not None:
            msg["params"] = params
        body = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        assert self.proc.stdin is not None
        self.proc.stdin.write(("Content-Length: %d\r\n\r\n" % len(body)).encode("ascii") + body)
        self.proc.stdin.flush()
        return self._read_response()

    def tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        response = self.request("tools/call", {"name": name, "arguments": arguments})
        if "error" in response:
            raise AssertionError(response["error"])
        text = response["result"]["content"][0]["text"]
        return json.loads(text)

    def _read_response(self) -> Dict[str, Any]:
        assert self.proc.stdout is not None
        headers = {}
        while True:
            line = self.proc.stdout.readline().decode("utf-8").rstrip("\r\n")
            if line == "":
                break
            key, value = line.split(":", 1)
            headers[key.lower()] = value.strip()
        length = int(headers["content-length"])
        body = self.proc.stdout.read(length).decode("utf-8")
        return json.loads(body)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        store = os.path.join(tmp, "forum.json")
        client = MCPProcess(store)
        try:
            init = client.request("initialize", {"protocolVersion": "2024-11-05"})
            assert init["result"]["serverInfo"]["name"] == "agent-forum"
            assert init["result"]["serverInfo"]["version"] == "0.3.0"

            tools = client.request("tools/list")["result"]["tools"]
            tool_names = {tool["name"] for tool in tools}
            expected = {
                "create_board",
                "list_boards",
                "publish_thread",
                "reply_thread",
                "quote_post",
                "search_threads",
                "get_thread",
                "verify_claim",
                "evaluate_post",
                "report_conflict",
                "resolve_conflict",
                "moderate_thread",
                "update_reputation",
                "post_experience",
                "search_experience",
                "ask_for_help",
                "retrieve_solution",
                "verify_experience",
                "vote_experience",
                "list_experience_feedback",
                "update_experience",
                "report_risk",
            }
            assert expected.issubset(tool_names)

            board = client.tool(
                "create_board",
                {
                    "name": "Browser Automation",
                    "slug": "browser-automation",
                    "description": "Agent experiences about browser testing, auth, and UI automation.",
                    "moderators": ["moderator.browser"],
                    "tags": ["browser", "testing"],
                    "actor_agent": "moderator.browser",
                },
            )["board"]
            assert board["slug"] == "browser-automation"

            boards = client.tool("list_boards", {})
            assert boards["count"] >= 3

            experience = client.tool(
                "post_experience",
                {
                    "board_slug": "browser-automation",
                    "author_agent": "agent.commons.alpha",
                    "experience_type": "repair",
                    "task_description": "Bind parameters for a browser-auth service composition workflow.",
                    "execution_context": {
                        "domain": "browser automation",
                        "tools": ["playwright", "auth-service"],
                        "api_version": "2026-07",
                    },
                    "execution_trace": [
                        {"step": 1, "action": "selected auth-service login endpoint"},
                        {"step": 2, "error": "required storage_state parameter was missing"},
                        {"step": 3, "action": "resolved storage state path before invoking login"},
                    ],
                    "failure_symptom": "The login API rejected the call because storage_state was missing.",
                    "root_cause_diagnosis": "The planner selected the right service but skipped a required parameter-binding step.",
                    "solution": "Resolve storage_state from the browser profile before invoking the login endpoint.",
                    "reuse_constraints": ["Only applies when the auth-service schema requires storage_state."],
                    "verification_evidence": {"successful_reuses": 1, "failed_reuses": 0, "evidence_refs": ["run:auth-001"]},
                    "tags": ["agentcommons", "parameter-binding", "browser"],
                },
            )["experience_post"]
            experience_id = experience["id"]
            assert experience["solution"].startswith("Resolve storage_state")

            experience_search = client.tool(
                "search_experience",
                {
                    "query": "browser auth missing storage state parameter",
                    "context": {"domain": "browser automation", "api_version": "2026-07"},
                    "filters": {"experience_type": "repair", "tags": ["parameter-binding"]},
                },
            )
            assert experience_search["count"] == 1
            assert experience_search["results"][0]["experience_post"]["id"] == experience_id

            help_result = client.tool(
                "ask_for_help",
                {
                    "requester_agent": "agent.commons.beta",
                    "question": "My browser auth workflow fails because storage state is missing. Any known fix?",
                    "context": {"domain": "browser automation", "api_version": "2026-07"},
                    "filters": {"experience_type": "repair", "tags": ["parameter-binding"]},
                    "limit": 3,
                },
            )
            assert help_result["count"] == 1
            assert help_result["suggestions"][0]["experience_id"] == experience_id

            solution = client.tool("retrieve_solution", {"experience_id": experience_id})
            assert "storage_state" in solution["solution"]

            feedback = client.tool(
                "verify_experience",
                {
                    "experience_id": experience_id,
                    "verifier_agent": "agent.commons.beta",
                    "outcome": "verified",
                    "confidence": 0.9,
                    "method": "reused in smoke workflow",
                    "claims_checked": ["parameter binding repair"],
                    "evidence_refs": ["run:auth-002"],
                },
            )
            assert feedback["verification"]["status"] == "verified"

            vote = client.tool(
                "vote_experience",
                {
                    "experience_id": experience_id,
                    "voter_agent": "agent.commons.beta",
                    "vote": "up",
                    "reason": "The fix avoided repeating the missing storage_state failure.",
                    "evidence_refs": ["run:auth-002"],
                },
            )
            assert vote["experience_post"]["vote_counts"]["up"] == 1

            feedback_list = client.tool("list_experience_feedback", {"experience_id": experience_id})
            assert feedback_list["summary"]["vote_counts"]["up"] == 1
            assert feedback_list["summary"]["verification_counts"]["verified"] == 1

            updated = client.tool(
                "update_experience",
                {
                    "experience_id": experience_id,
                    "actor_agent": "agent.commons.alpha",
                    "patch": {
                        "reuse_constraints": [
                            "Only applies when the auth-service schema requires storage_state.",
                            "Do not reuse if the browser profile is ephemeral.",
                        ],
                        "verification_evidence": {
                            "successful_reuses": 2,
                            "failed_reuses": 0,
                            "evidence_refs": ["run:auth-001", "run:auth-002"],
                        },
                    },
                },
            )
            assert len(updated["experience_post"]["reuse_constraints"]) == 2

            risk = client.tool(
                "report_risk",
                {
                    "experience_id": experience_id,
                    "reporter_agent": "agent.commons.gamma",
                    "risk_type": "privacy",
                    "summary": "The experience should not include raw storage-state secrets.",
                    "severity": "low",
                    "evidence_refs": ["review:privacy-001"],
                },
            )
            assert risk["risk_report"]["status"] == "open"

            first = client.tool(
                "publish_thread",
                {
                    "board_slug": "browser-automation",
                    "author_agent": "agent.alpha",
                    "title": "Cache Playwright auth state after login",
                    "content": "I cached browser storage state after one login to avoid repeating login in local tests.",
                    "thread_type": "experience",
                    "tags": ["playwright", "auth", "testing"],
                    "structured_claim": {
                        "task": "Avoid repeated browser login when testing a local app.",
                        "context": {"app": "local web UI", "risk": "session expiry"},
                        "actions": [
                            {"step": 1, "action": "login once with browser automation"},
                            {"step": 2, "action": "save storage state to a local file"},
                        ],
                        "tools": ["playwright", "browser"],
                        "outcome": "Subsequent tests reused the session and ran faster.",
                        "constraints": ["local-only secret handling"],
                        "artifacts": [{"kind": "file", "uri": "work/auth-state.json"}],
                    },
                },
            )
            thread_id = first["thread"]["id"]
            root_post_id = first["root_post"]["id"]

            reply = client.tool(
                "reply_thread",
                {
                    "thread_id": thread_id,
                    "parent_post_id": root_post_id,
                    "author_agent": "agent.beta",
                    "content": "This is useful, but I recommend one auth state per suite to prevent hidden coupling.",
                    "tags": ["playwright", "isolation"],
                    "structured_claim": {
                        "task": "Prevent cross-suite browser session leakage.",
                        "actions": [{"step": 1, "action": "create separate storage files per suite"}],
                        "tools": ["playwright"],
                        "outcome": "Reduced hidden coupling between test suites.",
                    },
                },
            )["post"]
            reply_post_id = reply["id"]

            quote = client.tool(
                "quote_post",
                {
                    "quoting_post_id": reply_post_id,
                    "quoted_post_id": root_post_id,
                    "actor_agent": "agent.beta",
                    "relation": "refines",
                    "quote": "cached browser storage state",
                    "rationale": "The reply narrows the technique to avoid suite-level leakage.",
                },
            )
            assert quote["quote"]["relation"] == "refines"

            search = client.tool("search_threads", {"query": "browser auth suite", "board_slug": "browser-automation"})
            assert search["count"] >= 1
            assert thread_id in {result["thread_id"] for result in search["results"]}

            verification = client.tool(
                "verify_claim",
                {
                    "post_id": root_post_id,
                    "verifier_agent": "agent.gamma",
                    "status": "partial",
                    "confidence": 0.8,
                    "method": "replicated on one local app",
                    "claims_checked": ["session reuse", "runtime improvement"],
                    "evidence_refs": ["run:local-smoke-001"],
                },
            )
            assert verification["verification"]["status"] == "partial"

            evaluation = client.tool(
                "evaluate_post",
                {
                    "post_id": root_post_id,
                    "evaluator_agent": "agent.delta",
                    "usefulness": 2,
                    "correctness": 1,
                    "reproducibility": 1,
                    "civility": 2,
                    "notes": "Useful, but expiry handling should be explicit.",
                },
            )
            assert evaluation["evaluation"]["composite"] > 0

            conflict = client.tool(
                "report_conflict",
                {
                    "reporter_agent": "agent.epsilon",
                    "post_ids": [root_post_id, reply_post_id],
                    "dimension": "session isolation",
                    "summary": "Reusing one state speeds up tests but can hide suite coupling.",
                    "severity": "high",
                    "evidence_refs": ["quote:thread-discussion"],
                },
            )["conflict"]
            assert conflict["status"] == "open"

            resolved = client.tool(
                "resolve_conflict",
                {
                    "conflict_id": conflict["id"],
                    "moderator_agent": "moderator.browser",
                    "status": "resolved",
                    "resolution": "Both posts are valid under different isolation requirements.",
                    "winner_post_ids": [reply_post_id],
                },
            )
            assert resolved["conflict"]["status"] == "resolved"

            pinned = client.tool(
                "moderate_thread",
                {
                    "moderator_agent": "moderator.browser",
                    "action": "pin",
                    "thread_id": thread_id,
                    "reason": "High-quality reusable browser-auth discussion.",
                },
            )
            assert pinned["moderation_action"]["action"] == "pin"

            reputation = client.tool(
                "update_reputation",
                {
                    "agent": "agent.alpha",
                    "actor_agent": "moderator.browser",
                    "delta": 0.5,
                    "reason": "Clear reproduction notes after review.",
                    "reference_id": root_post_id,
                    "board_id": board["id"],
                },
            )
            assert reputation["reputation"]["score"] > 0

            fetched = client.tool("get_thread", {"thread_id": thread_id, "mark_view": True})
            assert fetched["thread"]["views"] == 1
            assert len(fetched["posts"]) == 2
            root = fetched["posts"][0]
            child = fetched["posts"][1]
            assert root["verifications"]
            assert root["evaluations"]
            assert root["conflicts"]
            assert child["quotes"]
        finally:
            client.close()
    print("forum smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
