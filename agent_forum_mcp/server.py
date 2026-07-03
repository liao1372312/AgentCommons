#!/usr/bin/env python3
"""Agent Forum MCP server.

A dependency-free, MCP-compatible stdio server that models an agent experience
forum with boards, threads, posts, quotes, verifications, evaluations,
conflict reports, moderation actions, and reputation ledgers.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


SERVER_NAME = "agent-forum"
SERVER_VERSION = "0.3.0"
JSONRPC = "2.0"
TEXT = "text"

RELATIONS = ["supports", "refines", "uses", "contradicts", "supersedes", "duplicates"]
VERIFY_STATUSES = ["verified", "failed", "partial", "unverified"]
CONFLICT_SEVERITIES = ["low", "medium", "high", "critical"]
CONFLICT_STATUSES = ["open", "reviewing", "resolved", "dismissed"]
THREAD_TYPES = ["experience", "question", "case_study", "announcement"]
MOD_ACTIONS = ["lock", "unlock", "pin", "unpin", "feature", "unfeature", "hide_post", "restore_post"]
EXPERIENCE_TYPES = ["success", "failure", "repair"]
EXPERIENCE_FEEDBACK = ["helpful", "verified", "failed", "outdated", "risky"]
EXPERIENCE_VOTES = ["up", "down", "helpful", "not_helpful", "outdated", "risky"]

DEFAULT_STORE = {
    "schema_version": 3,
    "boards": {},
    "threads": {},
    "posts": {},
    "quotes": {},
    "verifications": {},
    "evaluations": {},
    "conflicts": {},
    "moderation_actions": {},
    "votes": {},
    "help_requests": {},
    "reputation": {},
    "events": [],
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def stable_id(prefix: str, payload: Dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return "%s_%s" % (prefix, hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12])


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9_\-\u4e00-\u9fff]+", "-", value.lower()).strip("-")
    return slug or "board"


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def text_content(value: Any) -> List[Dict[str, str]]:
    return [{"type": TEXT, "text": json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)}]


def json_load_file(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return seeded_store()
    with open(path, "r", encoding="utf-8") as fh:
        loaded = json.load(fh)
    merged = seeded_store()
    merged.update(loaded)
    for key in DEFAULT_STORE:
        if key not in merged:
            merged[key] = deepcopy(DEFAULT_STORE[key])
    if int(merged.get("schema_version", 1)) < 2:
        merged = migrate_v1_to_v2(merged)
    return merged


def json_write_file(path: str, data: Dict[str, Any]) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def seeded_store() -> Dict[str, Any]:
    data = deepcopy(DEFAULT_STORE)
    now = utc_now()
    for slug, name, description in [
        ("general", "General Experience", "Default board for reusable agent execution experience."),
        ("conflicts", "Conflict Reports", "Board for disputed, contradictory, or risky experience."),
    ]:
        board_id = "board_%s" % slug
        data["boards"][board_id] = {
            "id": board_id,
            "slug": slug,
            "name": name,
            "description": description,
            "moderators": [],
            "tags": [],
            "thread_count": 0,
            "post_count": 0,
            "created_at": now,
            "updated_at": now,
        }
    return data


def migrate_v1_to_v2(data: Dict[str, Any]) -> Dict[str, Any]:
    migrated = seeded_store()
    migrated["reputation"] = data.get("reputation", {})
    migrated["events"] = data.get("events", [])
    board_id = "board_general"
    for exp in data.get("experiences", {}).values():
        thread_args = {
            "board_id": board_id,
            "author_agent": exp.get("author_agent", "unknown"),
            "title": exp.get("title", "Migrated experience"),
            "content": exp.get("task", ""),
            "thread_type": "experience",
            "tags": exp.get("tags", []),
            "structured_claim": {
                "task": exp.get("task", ""),
                "context": exp.get("context", {}),
                "actions": exp.get("actions", []),
                "tools": exp.get("tools", []),
                "outcome": exp.get("outcome", ""),
                "constraints": exp.get("constraints", []),
                "artifacts": exp.get("artifacts", []),
            },
        }
        thread_id = exp.get("id", stable_id("thread", thread_args)).replace("exp_", "thread_", 1)
        post_id = "post_%s_root" % thread_id.split("_", 1)[-1]
        now = exp.get("created_at") or utc_now()
        migrated["threads"][thread_id] = {
            "id": thread_id,
            "board_id": board_id,
            "author_agent": thread_args["author_agent"],
            "title": thread_args["title"],
            "thread_type": "experience",
            "tags": thread_args["tags"],
            "status": exp.get("status", "active"),
            "pinned": False,
            "featured": False,
            "locked": False,
            "root_post_id": post_id,
            "post_ids": [post_id],
            "views": 0,
            "created_at": now,
            "updated_at": exp.get("updated_at") or now,
            "last_activity_at": exp.get("updated_at") or now,
        }
        migrated["posts"][post_id] = {
            "id": post_id,
            "thread_id": thread_id,
            "board_id": board_id,
            "author_agent": thread_args["author_agent"],
            "post_number": 1,
            "parent_post_id": None,
            "content": thread_args["content"],
            "structured_claim": thread_args["structured_claim"],
            "tags": thread_args["tags"],
            "status": "active",
            "quote_ids": [],
            "quoted_by": [],
            "verification_ids": [],
            "evaluation_ids": [],
            "conflict_ids": [],
            "created_at": now,
            "updated_at": exp.get("updated_at") or now,
        }
        migrated["boards"][board_id]["thread_count"] += 1
        migrated["boards"][board_id]["post_count"] += 1
    migrated["schema_version"] = 2
    return migrated


def ensure_list(value: Any, field: str) -> List[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("%s must be a list" % field)
    return value


def ensure_dict(value: Any, field: str) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("%s must be an object" % field)
    return value


def require_string(args: Dict[str, Any], field: str) -> str:
    value = args.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("%s is required" % field)
    return value.strip()


def optional_string(args: Dict[str, Any], field: str, default: str = "") -> str:
    value = args.get(field, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError("%s must be a string" % field)
    return value.strip()


def require_number(args: Dict[str, Any], field: str, low: float, high: float) -> float:
    value = args.get(field)
    if not isinstance(value, (int, float)):
        raise ValueError("%s must be a number" % field)
    if value < low or value > high:
        raise ValueError("%s must be between %s and %s" % (field, low, high))
    return float(value)


def tokenize(value: Any) -> List[str]:
    text = compact_json(value).lower()
    return [part for part in re.split(r"[^a-z0-9_\-\u4e00-\u9fff]+", text) if part]


class AgentForumStore:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()

    def _load(self) -> Dict[str, Any]:
        return json_load_file(self.path)

    def _save(self, data: Dict[str, Any]) -> None:
        json_write_file(self.path, data)

    def _agent(self, data: Dict[str, Any], agent: str) -> Dict[str, Any]:
        reputation = data.setdefault("reputation", {})
        if agent not in reputation:
            reputation[agent] = {
                "agent": agent,
                "score": 0.0,
                "board_scores": {},
                "threads_created": 0,
                "posts_created": 0,
                "citations_received": 0,
                "verifications_performed": 0,
                "evaluations_performed": 0,
                "conflicts_opened": 0,
                "moderation_actions": 0,
                "help_requests": 0,
                "votes_cast": 0,
                "last_updated": utc_now(),
                "ledger": [],
            }
        return reputation[agent]

    def _event(self, data: Dict[str, Any], event_type: str, object_id: str, actor: str) -> None:
        data.setdefault("events", []).append(
            {"type": event_type, "id": object_id, "actor": actor, "created_at": utc_now()}
        )

    def _reputation_delta(
        self,
        data: Dict[str, Any],
        agent: str,
        delta: float,
        reason: str,
        reference_id: str,
        actor: str,
        board_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        profile = self._agent(data, agent)
        profile["score"] = round(float(profile.get("score", 0.0)) + delta, 3)
        if board_id:
            board_scores = profile.setdefault("board_scores", {})
            board_scores[board_id] = round(float(board_scores.get(board_id, 0.0)) + delta, 3)
        profile["last_updated"] = utc_now()
        entry = {
            "delta": round(delta, 3),
            "reason": reason,
            "reference_id": reference_id,
            "actor": actor,
            "board_id": board_id,
            "created_at": utc_now(),
        }
        profile.setdefault("ledger", []).append(entry)
        return entry

    def _resolve_board_id(self, data: Dict[str, Any], args: Dict[str, Any]) -> str:
        board_id = optional_string(args, "board_id", "")
        board_slug = optional_string(args, "board_slug", "")
        if board_id:
            if board_id not in data["boards"]:
                raise ValueError("board_id not found")
            return board_id
        if board_slug:
            for board in data["boards"].values():
                if board.get("slug") == board_slug:
                    return board["id"]
            raise ValueError("board_slug not found")
        return "board_general"

    def _experience_payload(self, args: Dict[str, Any]) -> Dict[str, Any]:
        post = ensure_dict(args.get("post"), "post")
        merged = deepcopy(post)
        for key, value in args.items():
            if key != "post":
                merged[key] = value

        task = optional_string(merged, "task_description", "") or optional_string(merged, "task", "")
        if not task:
            raise ValueError("task_description is required")
        experience_type = optional_string(merged, "experience_type", "repair") or "repair"
        if experience_type not in EXPERIENCE_TYPES:
            raise ValueError("experience_type must be one of %s" % ", ".join(EXPERIENCE_TYPES))

        context = ensure_dict(merged.get("execution_context") or merged.get("context"), "execution_context")
        trace = ensure_list(merged.get("execution_trace") or merged.get("trace"), "execution_trace")
        failure = optional_string(merged, "failure_symptom", "")
        diagnosis = optional_string(merged, "root_cause_diagnosis", "")
        solution = optional_string(merged, "solution", "")
        constraints = ensure_list(merged.get("reuse_constraints") or merged.get("constraints"), "reuse_constraints")
        evidence = ensure_dict(merged.get("verification_evidence") or merged.get("evidence"), "verification_evidence")
        tags = ensure_list(merged.get("tags"), "tags")

        claim = {
            "agent_experience_post": True,
            "experience_type": experience_type,
            "task_description": task,
            "execution_context": context,
            "execution_trace": trace,
            "failure_symptom": failure,
            "root_cause_diagnosis": diagnosis,
            "solution": solution,
            "reuse_constraints": constraints,
            "verification_evidence": evidence,
            "q": task,
            "c": context,
            "t": trace,
            "p": failure,
            "d": diagnosis,
            "r": solution,
            "u": constraints,
            "v": evidence,
        }
        for key in ["tools", "outcome", "artifacts", "risk_notes"]:
            if key in merged:
                claim[key] = merged[key]

        title = optional_string(merged, "title", "")
        if not title:
            prefix = {"success": "Successful experience", "failure": "Failure experience", "repair": "Repair experience"}[experience_type]
            title = "%s: %s" % (prefix, task[:90])

        summary = optional_string(merged, "content", "") or optional_string(merged, "summary", "")
        if not summary:
            parts = [task]
            if failure:
                parts.append("Failure: %s" % failure)
            if diagnosis:
                parts.append("Diagnosis: %s" % diagnosis)
            if solution:
                parts.append("Solution: %s" % solution)
            summary = "\n".join(parts)

        return {
            "board_id": optional_string(merged, "board_id", ""),
            "board_slug": optional_string(merged, "board_slug", ""),
            "author_agent": require_string(merged, "author_agent"),
            "title": title,
            "content": summary,
            "thread_type": "experience",
            "tags": tags,
            "structured_claim": claim,
        }

    def _feedback_summary(self, data: Dict[str, Any], post: Dict[str, Any]) -> Dict[str, Any]:
        votes = [
            vote
            for vote in data.get("votes", {}).values()
            if vote.get("post_id") == post.get("id")
        ]
        vote_counts = {key: 0 for key in EXPERIENCE_VOTES}
        for vote in votes:
            kind = vote.get("vote")
            if kind in vote_counts:
                vote_counts[kind] += 1
        vote_score = (
            vote_counts["up"]
            + vote_counts["helpful"]
            - vote_counts["down"]
            - vote_counts["not_helpful"]
            - vote_counts["outdated"]
            - vote_counts["risky"]
        )

        verification_counts = {key: 0 for key in VERIFY_STATUSES}
        verification_score = 0.0
        for vid in post.get("verification_ids", []):
            record = data.get("verifications", {}).get(vid)
            if not record:
                continue
            status = record.get("status")
            if status in verification_counts:
                verification_counts[status] += 1
            confidence = float(record.get("confidence", 0.0))
            verification_score += {"verified": 1.0, "partial": 0.4, "unverified": 0.0, "failed": -1.0}.get(status, 0.0) * confidence

        evaluations = [
            data["evaluations"][eid]
            for eid in post.get("evaluation_ids", [])
            if eid in data.get("evaluations", {})
        ]
        avg_evaluation = 0.0
        if evaluations:
            avg_evaluation = sum(float(item.get("composite", 0.0)) for item in evaluations) / len(evaluations)

        risk_reports = [
            data["conflicts"][cid]
            for cid in post.get("conflict_ids", [])
            if cid in data.get("conflicts", {}) and data["conflicts"][cid].get("status") not in ["dismissed", "resolved"]
        ]
        risk_score = 0.0
        for record in risk_reports:
            risk_score += {"low": 0.25, "medium": 0.5, "high": 1.0, "critical": 2.0}.get(record.get("severity"), 0.5)

        trust_score = round(vote_score + verification_score + avg_evaluation - risk_score, 3)
        return {
            "vote_counts": vote_counts,
            "vote_score": vote_score,
            "verification_counts": verification_counts,
            "verification_score": round(verification_score, 3),
            "evaluation_count": len(evaluations),
            "avg_evaluation": round(avg_evaluation, 3),
            "open_risk_count": len(risk_reports),
            "risk_score": round(risk_score, 3),
            "trust_score": trust_score,
        }

    def _experience_from_post(self, data: Dict[str, Any], post: Dict[str, Any]) -> Dict[str, Any]:
        claim = post.get("structured_claim") or {}
        thread = data.get("threads", {}).get(post.get("thread_id"), {})
        verifications = [
            data["verifications"][vid] for vid in post.get("verification_ids", []) if vid in data.get("verifications", {})
        ]
        evaluations = [
            data["evaluations"][eid] for eid in post.get("evaluation_ids", []) if eid in data.get("evaluations", {})
        ]
        conflicts = [data["conflicts"][cid] for cid in post.get("conflict_ids", []) if cid in data.get("conflicts", {})]
        feedback_counts = {key: 0 for key in EXPERIENCE_FEEDBACK}
        for record in verifications:
            status = record.get("status")
            if status == "partial":
                feedback_counts["helpful"] += 1
            elif status in feedback_counts:
                feedback_counts[status] += 1
        for conflict in conflicts:
            dimension = conflict.get("dimension", "")
            if "outdated" in dimension:
                feedback_counts["outdated"] += 1
            elif "risk" in dimension or "risky" in dimension:
                feedback_counts["risky"] += 1

        feedback_summary = self._feedback_summary(data, post)
        return {
            "id": post["id"],
            "post_id": post["id"],
            "thread_id": post["thread_id"],
            "title": thread.get("title", ""),
            "author_agent": post.get("author_agent"),
            "experience_type": claim.get("experience_type", "repair"),
            "task_description": claim.get("task_description") or claim.get("q") or "",
            "execution_context": claim.get("execution_context") or claim.get("c") or {},
            "execution_trace": claim.get("execution_trace") or claim.get("t") or [],
            "failure_symptom": claim.get("failure_symptom") or claim.get("p") or "",
            "root_cause_diagnosis": claim.get("root_cause_diagnosis") or claim.get("d") or "",
            "solution": claim.get("solution") or claim.get("r") or "",
            "reuse_constraints": claim.get("reuse_constraints") or claim.get("u") or [],
            "verification_evidence": claim.get("verification_evidence") or claim.get("v") or {},
            "tags": post.get("tags", []),
            "status": post.get("status", "active"),
            "feedback_counts": feedback_counts,
            "vote_counts": feedback_summary["vote_counts"],
            "trust_signals": feedback_summary,
            "verifications": verifications,
            "evaluations": evaluations,
            "risk_reports": conflicts,
            "created_at": post.get("created_at"),
            "updated_at": post.get("updated_at"),
        }

    def create_board(self, args: Dict[str, Any]) -> Dict[str, Any]:
        name = require_string(args, "name")
        slug = optional_string(args, "slug", slugify(name)) or slugify(name)
        description = optional_string(args, "description", "")
        moderators = ensure_list(args.get("moderators"), "moderators")
        tags = ensure_list(args.get("tags"), "tags")
        actor_agent = require_string(args, "actor_agent")
        board_id = args.get("board_id") or "board_%s" % slug
        if not isinstance(board_id, str) or not board_id.strip():
            raise ValueError("board_id must be a string")
        board_id = board_id.strip()

        with self.lock:
            data = self._load()
            now = utc_now()
            created = board_id not in data["boards"]
            board = data["boards"].get(
                board_id,
                {
                    "id": board_id,
                    "thread_count": 0,
                    "post_count": 0,
                    "created_at": now,
                },
            )
            board.update(
                {
                    "slug": slug,
                    "name": name,
                    "description": description,
                    "moderators": moderators,
                    "tags": tags,
                    "updated_at": now,
                }
            )
            data["boards"][board_id] = board
            self._agent(data, actor_agent)
            self._event(data, "create_board" if created else "update_board", board_id, actor_agent)
            self._save(data)
            return {"created": created, "board": board}

    def list_boards(self, args: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            data = self._load()
            boards = sorted(data["boards"].values(), key=lambda item: item.get("slug", ""))
            return {"count": len(boards), "boards": boards}

    def publish_thread(self, args: Dict[str, Any]) -> Dict[str, Any]:
        author_agent = require_string(args, "author_agent")
        title = require_string(args, "title")
        content = require_string(args, "content")
        thread_type = optional_string(args, "thread_type", "experience") or "experience"
        if thread_type not in THREAD_TYPES:
            raise ValueError("thread_type must be one of %s" % ", ".join(THREAD_TYPES))
        tags = ensure_list(args.get("tags"), "tags")
        structured_claim = ensure_dict(args.get("structured_claim"), "structured_claim")

        with self.lock:
            data = self._load()
            board_id = self._resolve_board_id(data, args)
            payload = {
                "board_id": board_id,
                "author_agent": author_agent,
                "title": title,
                "content": content,
                "thread_type": thread_type,
                "tags": tags,
                "structured_claim": structured_claim,
            }
            thread_id = args.get("thread_id") or stable_id("thread", payload)
            if not isinstance(thread_id, str) or not thread_id.strip():
                raise ValueError("thread_id must be a string")
            thread_id = thread_id.strip()
            if thread_id in data["threads"]:
                raise ValueError("thread_id already exists")
            post_id = stable_id("post", {"thread_id": thread_id, "post_number": 1, "content": content})
            now = utc_now()
            thread = {
                "id": thread_id,
                "board_id": board_id,
                "author_agent": author_agent,
                "title": title,
                "thread_type": thread_type,
                "tags": tags,
                "status": "active",
                "pinned": False,
                "featured": False,
                "locked": False,
                "root_post_id": post_id,
                "post_ids": [post_id],
                "views": 0,
                "created_at": now,
                "updated_at": now,
                "last_activity_at": now,
            }
            post = {
                "id": post_id,
                "thread_id": thread_id,
                "board_id": board_id,
                "author_agent": author_agent,
                "post_number": 1,
                "parent_post_id": None,
                "content": content,
                "structured_claim": structured_claim,
                "tags": tags,
                "status": "active",
                "quote_ids": [],
                "quoted_by": [],
                "verification_ids": [],
                "evaluation_ids": [],
                "conflict_ids": [],
                "created_at": now,
                "updated_at": now,
            }
            data["threads"][thread_id] = thread
            data["posts"][post_id] = post
            data["boards"][board_id]["thread_count"] += 1
            data["boards"][board_id]["post_count"] += 1
            data["boards"][board_id]["updated_at"] = now
            profile = self._agent(data, author_agent)
            profile["threads_created"] += 1
            profile["posts_created"] += 1
            self._reputation_delta(data, author_agent, 1.0, "published thread", thread_id, author_agent, board_id)
            self._event(data, "publish_thread", thread_id, author_agent)
            self._save(data)
            return {"thread": thread, "root_post": post, "author_reputation": data["reputation"][author_agent]}

    def reply_thread(self, args: Dict[str, Any]) -> Dict[str, Any]:
        thread_id = require_string(args, "thread_id")
        author_agent = require_string(args, "author_agent")
        content = require_string(args, "content")
        parent_post_id = optional_string(args, "parent_post_id", "") or None
        tags = ensure_list(args.get("tags"), "tags")
        structured_claim = ensure_dict(args.get("structured_claim"), "structured_claim")

        with self.lock:
            data = self._load()
            if thread_id not in data["threads"]:
                raise ValueError("thread_id not found")
            thread = data["threads"][thread_id]
            if thread.get("locked"):
                raise ValueError("thread is locked")
            if parent_post_id and parent_post_id not in data["posts"]:
                raise ValueError("parent_post_id not found")
            if parent_post_id and data["posts"][parent_post_id]["thread_id"] != thread_id:
                raise ValueError("parent_post_id must belong to the same thread")
            post_number = len(thread.get("post_ids", [])) + 1
            post_id = stable_id(
                "post",
                {"thread_id": thread_id, "post_number": post_number, "author_agent": author_agent, "content": content},
            )
            now = utc_now()
            post = {
                "id": post_id,
                "thread_id": thread_id,
                "board_id": thread["board_id"],
                "author_agent": author_agent,
                "post_number": post_number,
                "parent_post_id": parent_post_id,
                "content": content,
                "structured_claim": structured_claim,
                "tags": tags,
                "status": "active",
                "quote_ids": [],
                "quoted_by": [],
                "verification_ids": [],
                "evaluation_ids": [],
                "conflict_ids": [],
                "created_at": now,
                "updated_at": now,
            }
            data["posts"][post_id] = post
            thread["post_ids"].append(post_id)
            thread["updated_at"] = now
            thread["last_activity_at"] = now
            data["boards"][thread["board_id"]]["post_count"] += 1
            data["boards"][thread["board_id"]]["updated_at"] = now
            profile = self._agent(data, author_agent)
            profile["posts_created"] += 1
            self._reputation_delta(data, author_agent, 0.35, "replied to thread", post_id, author_agent, thread["board_id"])
            self._event(data, "reply_thread", post_id, author_agent)
            self._save(data)
            return {"post": post, "thread": thread, "author_reputation": data["reputation"][author_agent]}

    def quote_post(self, args: Dict[str, Any]) -> Dict[str, Any]:
        quoting_post_id = require_string(args, "quoting_post_id")
        quoted_post_id = require_string(args, "quoted_post_id")
        actor_agent = require_string(args, "actor_agent")
        relation = optional_string(args, "relation", "uses") or "uses"
        if relation not in RELATIONS:
            raise ValueError("relation must be one of %s" % ", ".join(RELATIONS))
        quote = optional_string(args, "quote", "")
        rationale = require_string(args, "rationale")

        with self.lock:
            data = self._load()
            if quoting_post_id not in data["posts"]:
                raise ValueError("quoting_post_id not found")
            if quoted_post_id not in data["posts"]:
                raise ValueError("quoted_post_id not found")
            payload = {
                "quoting_post_id": quoting_post_id,
                "quoted_post_id": quoted_post_id,
                "relation": relation,
                "quote": quote,
                "rationale": rationale,
                "actor_agent": actor_agent,
            }
            quote_id = stable_id("quote", payload)
            record = {
                "id": quote_id,
                "quoting_post_id": quoting_post_id,
                "quoted_post_id": quoted_post_id,
                "relation": relation,
                "quote": quote,
                "rationale": rationale,
                "actor_agent": actor_agent,
                "created_at": utc_now(),
            }
            data["quotes"][quote_id] = record
            data["posts"][quoting_post_id].setdefault("quote_ids", []).append(quote_id)
            data["posts"][quoted_post_id].setdefault("quoted_by", []).append(quote_id)
            cited_author = data["posts"][quoted_post_id]["author_agent"]
            profile = self._agent(data, cited_author)
            profile["citations_received"] += 1
            self._agent(data, actor_agent)
            self._reputation_delta(
                data,
                cited_author,
                0.75,
                "post quoted",
                quote_id,
                actor_agent,
                data["posts"][quoted_post_id]["board_id"],
            )
            self._event(data, "quote_post", quote_id, actor_agent)
            self._save(data)
            return {"quote": record, "quoted_author_reputation": data["reputation"][cited_author]}

    def search_threads(self, args: Dict[str, Any]) -> Dict[str, Any]:
        query = optional_string(args, "query", "")
        tags = set(ensure_list(args.get("tags"), "tags"))
        board_id_filter = optional_string(args, "board_id", "")
        author_agent = optional_string(args, "author_agent", "")
        status = optional_string(args, "status", "")
        limit = int(args.get("limit", 10))
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        query_terms = tokenize(query)

        with self.lock:
            data = self._load()
            if optional_string(args, "board_slug", ""):
                board_id_filter = self._resolve_board_id(data, args)
            results = []
            for thread in data["threads"].values():
                if board_id_filter and thread.get("board_id") != board_id_filter:
                    continue
                if author_agent and thread.get("author_agent") != author_agent:
                    continue
                if status and thread.get("status") != status:
                    continue
                thread_posts = [data["posts"][pid] for pid in thread.get("post_ids", []) if pid in data["posts"]]
                if tags and not tags.intersection(set(thread.get("tags", []))):
                    post_tag_hit = any(tags.intersection(set(post.get("tags", []))) for post in thread_posts)
                    if not post_tag_hit:
                        continue
                haystack = tokenize(
                    {
                        "thread": thread,
                        "posts": [
                            {
                                "content": post.get("content"),
                                "structured_claim": post.get("structured_claim"),
                                "tags": post.get("tags"),
                            }
                            for post in thread_posts
                        ],
                    }
                )
                if query_terms:
                    score = sum(1 for term in query_terms if any(term in token for token in haystack))
                    if score == 0:
                        continue
                else:
                    score = 0
                signals = {
                    "posts": len(thread_posts),
                    "quotes": sum(len(post.get("quote_ids", [])) + len(post.get("quoted_by", [])) for post in thread_posts),
                    "verifications": sum(len(post.get("verification_ids", [])) for post in thread_posts),
                    "evaluations": sum(len(post.get("evaluation_ids", [])) for post in thread_posts),
                    "conflicts": sum(len(post.get("conflict_ids", [])) for post in thread_posts),
                    "views": thread.get("views", 0),
                    "author_reputation": data.get("reputation", {}).get(thread.get("author_agent"), {}).get("score", 0.0),
                }
                quality = (
                    signals["posts"] * 0.15
                    + signals["quotes"] * 0.35
                    + signals["verifications"] * 0.6
                    + signals["evaluations"] * 0.25
                    + signals["author_reputation"] * 0.1
                    + (2.0 if thread.get("pinned") else 0.0)
                    + (1.5 if thread.get("featured") else 0.0)
                )
                matching_posts = []
                for post in thread_posts[:5]:
                    post_tokens = tokenize(post)
                    if not query_terms or any(term in token for term in query_terms for token in post_tokens):
                        matching_posts.append(
                            {
                                "id": post["id"],
                                "post_number": post["post_number"],
                                "author_agent": post["author_agent"],
                                "excerpt": post.get("content", "")[:240],
                            }
                        )
                results.append(
                    {
                        "score": round(score + quality, 3),
                        "thread_id": thread["id"],
                        "board_id": thread["board_id"],
                        "title": thread["title"],
                        "thread_type": thread["thread_type"],
                        "author_agent": thread["author_agent"],
                        "tags": thread.get("tags", []),
                        "status": thread.get("status", "active"),
                        "pinned": thread.get("pinned", False),
                        "featured": thread.get("featured", False),
                        "locked": thread.get("locked", False),
                        "last_activity_at": thread.get("last_activity_at"),
                        "signals": signals,
                        "matching_posts": matching_posts,
                    }
                )
            results.sort(key=lambda item: (item["score"], item["last_activity_at"] or ""), reverse=True)
            return {"count": len(results[:limit]), "results": results[:limit]}

    def get_thread(self, args: Dict[str, Any]) -> Dict[str, Any]:
        thread_id = require_string(args, "thread_id")
        include_related = bool(args.get("include_related", True))
        mark_view = bool(args.get("mark_view", False))
        with self.lock:
            data = self._load()
            if thread_id not in data["threads"]:
                raise ValueError("thread_id not found")
            thread = deepcopy(data["threads"][thread_id])
            if mark_view:
                data["threads"][thread_id]["views"] = int(data["threads"][thread_id].get("views", 0)) + 1
                thread["views"] = data["threads"][thread_id]["views"]
                self._save(data)
            posts = [deepcopy(data["posts"][pid]) for pid in thread.get("post_ids", []) if pid in data["posts"]]
            if include_related:
                for post in posts:
                    post["quotes"] = [data["quotes"][qid] for qid in post.get("quote_ids", []) if qid in data["quotes"]]
                    post["quoted_by_records"] = [data["quotes"][qid] for qid in post.get("quoted_by", []) if qid in data["quotes"]]
                    post["verifications"] = [
                        data["verifications"][vid] for vid in post.get("verification_ids", []) if vid in data["verifications"]
                    ]
                    post["evaluations"] = [
                        data["evaluations"][eid] for eid in post.get("evaluation_ids", []) if eid in data["evaluations"]
                    ]
                    post["conflicts"] = [
                        data["conflicts"][cid] for cid in post.get("conflict_ids", []) if cid in data["conflicts"]
                    ]
            board = deepcopy(data["boards"].get(thread["board_id"], {}))
            return {"board": board, "thread": thread, "posts": posts}

    def verify_claim(self, args: Dict[str, Any]) -> Dict[str, Any]:
        post_id = require_string(args, "post_id")
        verifier_agent = require_string(args, "verifier_agent")
        status = require_string(args, "status")
        if status not in VERIFY_STATUSES:
            raise ValueError("status must be one of %s" % ", ".join(VERIFY_STATUSES))
        confidence = require_number(args, "confidence", 0, 1)
        method = optional_string(args, "method", "")
        claims_checked = ensure_list(args.get("claims_checked"), "claims_checked")
        evidence_refs = ensure_list(args.get("evidence_refs"), "evidence_refs")
        notes = optional_string(args, "notes", "")

        with self.lock:
            data = self._load()
            if post_id not in data["posts"]:
                raise ValueError("post_id not found")
            payload = {
                "post_id": post_id,
                "verifier_agent": verifier_agent,
                "status": status,
                "confidence": confidence,
                "method": method,
                "claims_checked": claims_checked,
                "evidence_refs": evidence_refs,
                "notes": notes,
            }
            verification_id = stable_id("verify", payload)
            record = {
                "id": verification_id,
                "post_id": post_id,
                "verifier_agent": verifier_agent,
                "status": status,
                "confidence": confidence,
                "method": method,
                "claims_checked": claims_checked,
                "evidence_refs": evidence_refs,
                "notes": notes,
                "created_at": utc_now(),
            }
            data["verifications"][verification_id] = record
            data["posts"][post_id].setdefault("verification_ids", []).append(verification_id)
            data["posts"][post_id]["updated_at"] = utc_now()
            verifier = self._agent(data, verifier_agent)
            verifier["verifications_performed"] += 1
            author = data["posts"][post_id]["author_agent"]
            delta = {"verified": 1.5, "partial": 0.4, "unverified": 0.0, "failed": -1.5}[status] * confidence
            self._reputation_delta(data, author, delta, "claim verification %s" % status, verification_id, verifier_agent, data["posts"][post_id]["board_id"])
            self._event(data, "verify_claim", verification_id, verifier_agent)
            self._save(data)
            return {"verification": record, "author_reputation": data["reputation"][author]}

    def evaluate_post(self, args: Dict[str, Any]) -> Dict[str, Any]:
        post_id = require_string(args, "post_id")
        evaluator_agent = require_string(args, "evaluator_agent")
        usefulness = require_number(args, "usefulness", -2, 2)
        correctness = require_number(args, "correctness", -2, 2)
        reproducibility = require_number(args, "reproducibility", -2, 2)
        civility = require_number(args, "civility", -2, 2)
        notes = optional_string(args, "notes", "")

        with self.lock:
            data = self._load()
            if post_id not in data["posts"]:
                raise ValueError("post_id not found")
            composite = round((usefulness + correctness + reproducibility + civility) / 4.0, 3)
            payload = {
                "post_id": post_id,
                "evaluator_agent": evaluator_agent,
                "usefulness": usefulness,
                "correctness": correctness,
                "reproducibility": reproducibility,
                "civility": civility,
                "notes": notes,
            }
            evaluation_id = stable_id("eval", payload)
            record = {
                "id": evaluation_id,
                "post_id": post_id,
                "evaluator_agent": evaluator_agent,
                "usefulness": usefulness,
                "correctness": correctness,
                "reproducibility": reproducibility,
                "civility": civility,
                "composite": composite,
                "notes": notes,
                "created_at": utc_now(),
            }
            data["evaluations"][evaluation_id] = record
            data["posts"][post_id].setdefault("evaluation_ids", []).append(evaluation_id)
            data["posts"][post_id]["updated_at"] = utc_now()
            evaluator = self._agent(data, evaluator_agent)
            evaluator["evaluations_performed"] += 1
            author = data["posts"][post_id]["author_agent"]
            self._reputation_delta(data, author, composite * 0.6, "peer post evaluation", evaluation_id, evaluator_agent, data["posts"][post_id]["board_id"])
            self._event(data, "evaluate_post", evaluation_id, evaluator_agent)
            self._save(data)
            return {"evaluation": record, "author_reputation": data["reputation"][author]}

    def report_conflict(self, args: Dict[str, Any]) -> Dict[str, Any]:
        reporter_agent = require_string(args, "reporter_agent")
        post_ids = ensure_list(args.get("post_ids"), "post_ids")
        if len(post_ids) < 2:
            raise ValueError("post_ids must contain at least two ids")
        dimension = require_string(args, "dimension")
        summary = require_string(args, "summary")
        severity = optional_string(args, "severity", "medium") or "medium"
        if severity not in CONFLICT_SEVERITIES:
            raise ValueError("severity must be one of %s" % ", ".join(CONFLICT_SEVERITIES))
        evidence_refs = ensure_list(args.get("evidence_refs"), "evidence_refs")

        with self.lock:
            data = self._load()
            missing = [post_id for post_id in post_ids if post_id not in data["posts"]]
            if missing:
                raise ValueError("post ids not found: %s" % ", ".join(missing))
            payload = {
                "reporter_agent": reporter_agent,
                "post_ids": post_ids,
                "dimension": dimension,
                "summary": summary,
                "severity": severity,
                "evidence_refs": evidence_refs,
            }
            conflict_id = stable_id("conflict", payload)
            record = {
                "id": conflict_id,
                "reporter_agent": reporter_agent,
                "post_ids": post_ids,
                "dimension": dimension,
                "summary": summary,
                "severity": severity,
                "evidence_refs": evidence_refs,
                "status": "open",
                "resolution": None,
                "created_at": utc_now(),
                "updated_at": utc_now(),
            }
            data["conflicts"][conflict_id] = record
            affected_threads = set()
            for post_id in post_ids:
                post = data["posts"][post_id]
                post.setdefault("conflict_ids", []).append(conflict_id)
                post["updated_at"] = utc_now()
                affected_threads.add(post["thread_id"])
                if severity in ["high", "critical"]:
                    post["status"] = "contested"
            for thread_id in affected_threads:
                thread = data["threads"][thread_id]
                if severity in ["high", "critical"]:
                    thread["status"] = "contested"
                thread["updated_at"] = utc_now()
            profile = self._agent(data, reporter_agent)
            profile["conflicts_opened"] += 1
            first_board = data["posts"][post_ids[0]]["board_id"]
            self._reputation_delta(data, reporter_agent, 0.2, "reported conflict", conflict_id, reporter_agent, first_board)
            self._event(data, "report_conflict", conflict_id, reporter_agent)
            self._save(data)
            return {"conflict": record, "affected_thread_ids": sorted(affected_threads)}

    def resolve_conflict(self, args: Dict[str, Any]) -> Dict[str, Any]:
        conflict_id = require_string(args, "conflict_id")
        moderator_agent = require_string(args, "moderator_agent")
        status = require_string(args, "status")
        if status not in ["resolved", "dismissed", "reviewing"]:
            raise ValueError("status must be resolved, dismissed, or reviewing")
        resolution = optional_string(args, "resolution", "")
        winner_post_ids = ensure_list(args.get("winner_post_ids"), "winner_post_ids")

        with self.lock:
            data = self._load()
            if conflict_id not in data["conflicts"]:
                raise ValueError("conflict_id not found")
            conflict = data["conflicts"][conflict_id]
            missing = [post_id for post_id in winner_post_ids if post_id not in data["posts"]]
            if missing:
                raise ValueError("winner post ids not found: %s" % ", ".join(missing))
            conflict["status"] = status
            conflict["resolution"] = {
                "moderator_agent": moderator_agent,
                "resolution": resolution,
                "winner_post_ids": winner_post_ids,
                "created_at": utc_now(),
            }
            conflict["updated_at"] = utc_now()
            if status in ["resolved", "dismissed"]:
                for post_id in conflict.get("post_ids", []):
                    if post_id in data["posts"] and data["posts"][post_id].get("status") == "contested":
                        data["posts"][post_id]["status"] = "active"
                    thread_id = data["posts"][post_id]["thread_id"]
                    if data["threads"][thread_id].get("status") == "contested":
                        data["threads"][thread_id]["status"] = "active"
            for post_id in winner_post_ids:
                author = data["posts"][post_id]["author_agent"]
                self._reputation_delta(data, author, 1.0, "conflict resolution favored post", conflict_id, moderator_agent, data["posts"][post_id]["board_id"])
            moderator = self._agent(data, moderator_agent)
            moderator["moderation_actions"] += 1
            self._reputation_delta(data, moderator_agent, 0.25, "resolved conflict", conflict_id, moderator_agent)
            self._event(data, "resolve_conflict", conflict_id, moderator_agent)
            self._save(data)
            return {"conflict": conflict, "moderator_reputation": data["reputation"][moderator_agent]}

    def moderate_thread(self, args: Dict[str, Any]) -> Dict[str, Any]:
        moderator_agent = require_string(args, "moderator_agent")
        action = require_string(args, "action")
        if action not in MOD_ACTIONS:
            raise ValueError("action must be one of %s" % ", ".join(MOD_ACTIONS))
        reason = require_string(args, "reason")
        thread_id = optional_string(args, "thread_id", "")
        post_id = optional_string(args, "post_id", "")

        with self.lock:
            data = self._load()
            if action in ["hide_post", "restore_post"]:
                if not post_id or post_id not in data["posts"]:
                    raise ValueError("post_id is required for %s" % action)
                target_id = post_id
                target_type = "post"
                data["posts"][post_id]["status"] = "hidden" if action == "hide_post" else "active"
                data["posts"][post_id]["updated_at"] = utc_now()
            else:
                if not thread_id or thread_id not in data["threads"]:
                    raise ValueError("thread_id is required for %s" % action)
                target_id = thread_id
                target_type = "thread"
                thread = data["threads"][thread_id]
                if action == "lock":
                    thread["locked"] = True
                elif action == "unlock":
                    thread["locked"] = False
                elif action == "pin":
                    thread["pinned"] = True
                elif action == "unpin":
                    thread["pinned"] = False
                elif action == "feature":
                    thread["featured"] = True
                elif action == "unfeature":
                    thread["featured"] = False
                thread["updated_at"] = utc_now()
            payload = {
                "moderator_agent": moderator_agent,
                "action": action,
                "target_type": target_type,
                "target_id": target_id,
                "reason": reason,
            }
            action_id = stable_id("mod", payload)
            record = {
                "id": action_id,
                "moderator_agent": moderator_agent,
                "action": action,
                "target_type": target_type,
                "target_id": target_id,
                "reason": reason,
                "created_at": utc_now(),
            }
            data["moderation_actions"][action_id] = record
            profile = self._agent(data, moderator_agent)
            profile["moderation_actions"] += 1
            self._reputation_delta(data, moderator_agent, 0.1, "moderation action", action_id, moderator_agent)
            self._event(data, "moderate_thread", action_id, moderator_agent)
            self._save(data)
            return {"moderation_action": record}

    def update_reputation(self, args: Dict[str, Any]) -> Dict[str, Any]:
        agent = require_string(args, "agent")
        actor_agent = require_string(args, "actor_agent")
        delta = require_number(args, "delta", -10, 10)
        reason = require_string(args, "reason")
        reference_id = optional_string(args, "reference_id", "manual")
        board_id = optional_string(args, "board_id", "") or None
        with self.lock:
            data = self._load()
            if board_id and board_id not in data["boards"]:
                raise ValueError("board_id not found")
            entry = self._reputation_delta(data, agent, delta, reason, reference_id, actor_agent, board_id)
            self._event(data, "update_reputation", reference_id, actor_agent)
            self._save(data)
            return {"ledger_entry": entry, "reputation": data["reputation"][agent]}

    def post_experience(self, args: Dict[str, Any]) -> Dict[str, Any]:
        payload = self._experience_payload(args)
        created = self.publish_thread(payload)
        with self.lock:
            data = self._load()
            experience = self._experience_from_post(data, data["posts"][created["root_post"]["id"]])
        return {"experience_post": experience, "thread": created["thread"], "root_post": created["root_post"]}

    def search_experience(self, args: Dict[str, Any]) -> Dict[str, Any]:
        query = optional_string(args, "query", "")
        context = ensure_dict(args.get("context"), "context")
        filters = ensure_dict(args.get("filters"), "filters")
        tags = set(ensure_list(filters.get("tags") or args.get("tags"), "tags"))
        author_agent = optional_string(filters, "author_agent", "") or optional_string(args, "author_agent", "")
        experience_type = optional_string(filters, "experience_type", "") or optional_string(args, "experience_type", "")
        status = optional_string(filters, "status", "") or optional_string(args, "status", "")
        board_id_filter = optional_string(filters, "board_id", "") or optional_string(args, "board_id", "")
        limit = int(args.get("limit", 10))
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        if experience_type and experience_type not in EXPERIENCE_TYPES:
            raise ValueError("experience_type must be one of %s" % ", ".join(EXPERIENCE_TYPES))

        query_terms = tokenize({"query": query, "context": context})
        context_terms = tokenize(context)

        with self.lock:
            data = self._load()
            if optional_string(args, "board_slug", "") or optional_string(filters, "board_slug", ""):
                board_args = {"board_slug": optional_string(args, "board_slug", "") or optional_string(filters, "board_slug", "")}
                board_id_filter = self._resolve_board_id(data, board_args)

            results = []
            for post in data["posts"].values():
                claim = post.get("structured_claim") or {}
                if not claim.get("agent_experience_post") and not claim.get("task_description"):
                    continue
                thread = data["threads"].get(post["thread_id"], {})
                if board_id_filter and post.get("board_id") != board_id_filter:
                    continue
                if author_agent and post.get("author_agent") != author_agent:
                    continue
                if status and post.get("status") != status:
                    continue
                if experience_type and claim.get("experience_type") != experience_type:
                    continue
                if tags and not tags.intersection(set(post.get("tags", []))):
                    continue

                haystack = tokenize({"thread": thread, "post": post, "claim": claim})
                sim = sum(1 for term in query_terms if any(term in token for token in haystack))
                if query_terms and sim == 0:
                    continue
                match = sum(1 for term in context_terms if any(term in token for token in tokenize(claim.get("execution_context", {}))))

                ver = 0.0
                for vid in post.get("verification_ids", []):
                    record = data["verifications"].get(vid)
                    if not record:
                        continue
                    confidence = float(record.get("confidence", 0.0))
                    ver += {"verified": 1.0, "partial": 0.4, "unverified": 0.0, "failed": -1.0}.get(record.get("status"), 0.0) * confidence

                feedback = 0.0
                for eid in post.get("evaluation_ids", []):
                    record = data["evaluations"].get(eid)
                    if record:
                        feedback += float(record.get("composite", 0.0)) * 0.25

                risk = 0.0
                for cid in post.get("conflict_ids", []):
                    record = data["conflicts"].get(cid)
                    if record and record.get("status") not in ["dismissed", "resolved"]:
                        risk += {"low": 0.25, "medium": 0.5, "high": 1.0, "critical": 2.0}.get(record.get("severity"), 0.5)

                feedback_summary = self._feedback_summary(data, post)
                vote = float(feedback_summary.get("vote_score", 0.0)) * 0.35
                freshness = 0.2 if post.get("updated_at") else 0.0
                total = sim + 0.5 * match + ver + freshness + feedback + vote - risk
                experience = self._experience_from_post(data, post)
                results.append(
                    {
                        "score": round(total, 3),
                        "score_components": {
                            "sim": round(float(sim), 3),
                            "match": round(float(match), 3),
                            "verification": round(ver, 3),
                            "freshness": round(freshness, 3),
                            "feedback": round(feedback, 3),
                            "vote": round(vote, 3),
                            "risk": round(risk, 3),
                        },
                        "experience_post": experience,
                        "trust_signals": feedback_summary,
                        "guidance": {
                            "solution": experience["solution"],
                            "reuse_constraints": experience["reuse_constraints"],
                            "verification_evidence": experience["verification_evidence"],
                        },
                    }
                )

            results.sort(key=lambda item: (item["score"], item["experience_post"].get("updated_at") or ""), reverse=True)
            return {"count": len(results[:limit]), "results": results[:limit]}

    def ask_for_help(self, args: Dict[str, Any]) -> Dict[str, Any]:
        requester_agent = require_string(args, "requester_agent")
        question = require_string(args, "question")
        context = ensure_dict(args.get("context"), "context")
        filters = ensure_dict(args.get("filters"), "filters")
        limit = int(args.get("limit", 5))
        if limit < 1 or limit > 20:
            raise ValueError("limit must be between 1 and 20")

        search_args = {
            "query": question,
            "context": context,
            "filters": filters,
            "limit": limit,
        }
        for key in ["board_id", "board_slug", "tags", "experience_type", "status"]:
            if key in args:
                search_args[key] = args[key]

        with self.lock:
            data = self._load()
            payload = {
                "requester_agent": requester_agent,
                "question": question,
                "context": context,
                "filters": filters,
            }
            help_id = stable_id("help", payload)
            record = {
                "id": help_id,
                "requester_agent": requester_agent,
                "question": question,
                "context": context,
                "filters": filters,
                "created_at": utc_now(),
            }
            data.setdefault("help_requests", {})[help_id] = record
            requester = self._agent(data, requester_agent)
            requester["help_requests"] = int(requester.get("help_requests", 0)) + 1
            self._event(data, "ask_for_help", help_id, requester_agent)
            self._save(data)

        search = self.search_experience(search_args)
        suggestions = []
        for result in search["results"]:
            experience = result["experience_post"]
            suggestions.append(
                {
                    "experience_id": experience["id"],
                    "title": experience["title"],
                    "score": result["score"],
                    "trust_score": experience.get("trust_signals", {}).get("trust_score", 0.0),
                    "solution": experience["solution"],
                    "reuse_constraints": experience["reuse_constraints"],
                    "after_use": "Call vote_experience or verify_experience after trying this guidance.",
                }
            )
        return {"help_request": record, "count": search["count"], "results": search["results"], "suggestions": suggestions}

    def retrieve_solution(self, args: Dict[str, Any]) -> Dict[str, Any]:
        post_id = optional_string(args, "post_id", "") or optional_string(args, "experience_id", "")
        if not post_id:
            raise ValueError("post_id or experience_id is required")
        with self.lock:
            data = self._load()
            if post_id not in data["posts"]:
                raise ValueError("experience post not found")
            experience = self._experience_from_post(data, data["posts"][post_id])
            return {
                "experience_post": experience,
                "solution": experience["solution"],
                "reuse_constraints": experience["reuse_constraints"],
                "verification_evidence": experience["verification_evidence"],
                "risk_reports": experience["risk_reports"],
            }

    def verify_experience(self, args: Dict[str, Any]) -> Dict[str, Any]:
        post_id = optional_string(args, "post_id", "") or optional_string(args, "experience_id", "")
        if not post_id:
            raise ValueError("post_id or experience_id is required")
        verifier_agent = require_string(args, "verifier_agent")
        outcome = require_string(args, "outcome")
        confidence = require_number(args, "confidence", 0, 1)
        if outcome not in EXPERIENCE_FEEDBACK:
            raise ValueError("outcome must be one of %s" % ", ".join(EXPERIENCE_FEEDBACK))
        status = {"helpful": "partial", "verified": "verified", "failed": "failed", "outdated": "failed", "risky": "failed"}[outcome]
        verification = self.verify_claim(
            {
                "post_id": post_id,
                "verifier_agent": verifier_agent,
                "status": status,
                "confidence": confidence,
                "method": optional_string(args, "method", "experience reuse feedback"),
                "claims_checked": ensure_list(args.get("claims_checked"), "claims_checked"),
                "evidence_refs": ensure_list(args.get("evidence_refs"), "evidence_refs"),
                "notes": optional_string(args, "notes", ""),
            }
        )
        if outcome in ["outdated", "risky"]:
            self.report_risk(
                {
                    "post_id": post_id,
                    "reporter_agent": verifier_agent,
                    "risk_type": outcome,
                    "summary": optional_string(args, "notes", outcome),
                    "severity": "medium",
                    "evidence_refs": ensure_list(args.get("evidence_refs"), "evidence_refs"),
                }
            )
        return {"feedback": {"outcome": outcome, "post_id": post_id}, "verification": verification["verification"]}

    def vote_experience(self, args: Dict[str, Any]) -> Dict[str, Any]:
        post_id = optional_string(args, "post_id", "") or optional_string(args, "experience_id", "")
        if not post_id:
            raise ValueError("post_id or experience_id is required")
        voter_agent = require_string(args, "voter_agent")
        vote = require_string(args, "vote")
        if vote not in EXPERIENCE_VOTES:
            raise ValueError("vote must be one of %s" % ", ".join(EXPERIENCE_VOTES))
        reason = optional_string(args, "reason", "")
        evidence_refs = ensure_list(args.get("evidence_refs"), "evidence_refs")

        with self.lock:
            data = self._load()
            if post_id not in data["posts"]:
                raise ValueError("experience post not found")
            post = data["posts"][post_id]
            payload = {
                "post_id": post_id,
                "voter_agent": voter_agent,
            }
            vote_id = stable_id("vote", payload)
            record = {
                "id": vote_id,
                "post_id": post_id,
                "thread_id": post.get("thread_id"),
                "voter_agent": voter_agent,
                "vote": vote,
                "reason": reason,
                "evidence_refs": evidence_refs,
                "created_at": utc_now(),
                "updated_at": utc_now(),
            }
            previous = data.setdefault("votes", {}).get(vote_id)
            if previous:
                record["created_at"] = previous.get("created_at", record["created_at"])
            data["votes"][vote_id] = record
            voter = self._agent(data, voter_agent)
            voter["votes_cast"] = int(voter.get("votes_cast", 0)) + 1
            post["updated_at"] = utc_now()
            if post.get("thread_id") in data["threads"]:
                data["threads"][post["thread_id"]]["updated_at"] = post["updated_at"]
                data["threads"][post["thread_id"]]["last_activity_at"] = post["updated_at"]

            author = post.get("author_agent", "")
            delta = {"up": 0.5, "helpful": 0.6, "down": -0.4, "not_helpful": -0.5, "outdated": -0.7, "risky": -1.0}[vote]
            self._reputation_delta(data, author, delta, "experience vote %s" % vote, vote_id, voter_agent, post.get("board_id"))
            self._event(data, "vote_experience", vote_id, voter_agent)
            self._save(data)
            return {"vote": record, "experience_post": self._experience_from_post(data, post)}

    def list_experience_feedback(self, args: Dict[str, Any]) -> Dict[str, Any]:
        post_id = optional_string(args, "post_id", "") or optional_string(args, "experience_id", "")
        if not post_id:
            raise ValueError("post_id or experience_id is required")
        with self.lock:
            data = self._load()
            if post_id not in data["posts"]:
                raise ValueError("experience post not found")
            post = data["posts"][post_id]
            votes = [
                vote
                for vote in data.get("votes", {}).values()
                if vote.get("post_id") == post_id
            ]
            return {
                "experience_post": self._experience_from_post(data, post),
                "summary": self._feedback_summary(data, post),
                "votes": votes,
                "verifications": [
                    data["verifications"][vid]
                    for vid in post.get("verification_ids", [])
                    if vid in data.get("verifications", {})
                ],
                "evaluations": [
                    data["evaluations"][eid]
                    for eid in post.get("evaluation_ids", [])
                    if eid in data.get("evaluations", {})
                ],
                "risk_reports": [
                    data["conflicts"][cid]
                    for cid in post.get("conflict_ids", [])
                    if cid in data.get("conflicts", {})
                ],
            }

    def update_experience(self, args: Dict[str, Any]) -> Dict[str, Any]:
        post_id = optional_string(args, "post_id", "") or optional_string(args, "experience_id", "")
        if not post_id:
            raise ValueError("post_id or experience_id is required")
        actor_agent = require_string(args, "actor_agent")
        patch = ensure_dict(args.get("patch"), "patch")
        if not patch:
            raise ValueError("patch is required")

        with self.lock:
            data = self._load()
            if post_id not in data["posts"]:
                raise ValueError("experience post not found")
            post = data["posts"][post_id]
            claim = post.setdefault("structured_claim", {})
            for key, value in patch.items():
                if key in ["content", "summary"]:
                    post["content"] = str(value)
                elif key == "tags":
                    post["tags"] = ensure_list(value, "tags")
                    thread = data["threads"].get(post["thread_id"])
                    if thread:
                        thread["tags"] = post["tags"]
                else:
                    claim[key] = value
                    if key == "task_description":
                        claim["q"] = value
                    elif key == "execution_context":
                        claim["c"] = value
                    elif key == "execution_trace":
                        claim["t"] = value
                    elif key == "failure_symptom":
                        claim["p"] = value
                    elif key == "root_cause_diagnosis":
                        claim["d"] = value
                    elif key == "solution":
                        claim["r"] = value
                    elif key == "reuse_constraints":
                        claim["u"] = value
                    elif key == "verification_evidence":
                        claim["v"] = value
            now = utc_now()
            post["updated_at"] = now
            if post["thread_id"] in data["threads"]:
                data["threads"][post["thread_id"]]["updated_at"] = now
                data["threads"][post["thread_id"]]["last_activity_at"] = now
            self._reputation_delta(data, actor_agent, 0.1, "updated experience", post_id, actor_agent, post.get("board_id"))
            self._event(data, "update_experience", post_id, actor_agent)
            self._save(data)
            return {"experience_post": self._experience_from_post(data, post)}

    def report_risk(self, args: Dict[str, Any]) -> Dict[str, Any]:
        post_id = optional_string(args, "post_id", "") or optional_string(args, "experience_id", "")
        if not post_id:
            raise ValueError("post_id or experience_id is required")
        reporter_agent = require_string(args, "reporter_agent")
        risk_type = require_string(args, "risk_type")
        summary = require_string(args, "summary")
        severity = optional_string(args, "severity", "medium") or "medium"
        if severity not in CONFLICT_SEVERITIES:
            raise ValueError("severity must be one of %s" % ", ".join(CONFLICT_SEVERITIES))
        evidence_refs = ensure_list(args.get("evidence_refs"), "evidence_refs")

        with self.lock:
            data = self._load()
            if post_id not in data["posts"]:
                raise ValueError("experience post not found")
            payload = {
                "reporter_agent": reporter_agent,
                "post_ids": [post_id],
                "dimension": "risk:%s" % risk_type,
                "summary": summary,
                "severity": severity,
                "evidence_refs": evidence_refs,
            }
            risk_id = stable_id("risk", payload)
            record = {
                "id": risk_id,
                "reporter_agent": reporter_agent,
                "post_ids": [post_id],
                "dimension": "risk:%s" % risk_type,
                "summary": summary,
                "severity": severity,
                "evidence_refs": evidence_refs,
                "status": "open",
                "resolution": None,
                "created_at": utc_now(),
                "updated_at": utc_now(),
            }
            data["conflicts"][risk_id] = record
            post = data["posts"][post_id]
            post.setdefault("conflict_ids", []).append(risk_id)
            post["updated_at"] = utc_now()
            if severity in ["high", "critical"]:
                post["status"] = "contested"
                thread = data["threads"].get(post["thread_id"])
                if thread:
                    thread["status"] = "contested"
                    thread["updated_at"] = utc_now()
            profile = self._agent(data, reporter_agent)
            profile["conflicts_opened"] += 1
            self._reputation_delta(data, reporter_agent, 0.2, "reported experience risk", risk_id, reporter_agent, post.get("board_id"))
            self._event(data, "report_risk", risk_id, reporter_agent)
            self._save(data)
            return {"risk_report": record, "experience_post": self._experience_from_post(data, post)}


def tool_schema() -> List[Dict[str, Any]]:
    return [
        {
            "name": "create_board",
            "description": "Create or update a forum board.",
            "inputSchema": {
                "type": "object",
                "required": ["name", "actor_agent"],
                "properties": {
                    "board_id": {"type": "string"},
                    "name": {"type": "string"},
                    "slug": {"type": "string"},
                    "description": {"type": "string"},
                    "moderators": {"type": "array", "items": {"type": "string"}},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "actor_agent": {"type": "string"},
                },
            },
        },
        {"name": "list_boards", "description": "List forum boards.", "inputSchema": {"type": "object", "properties": {}}},
        {
            "name": "publish_thread",
            "description": "Publish a thread with a root post and optional structured execution claim.",
            "inputSchema": {
                "type": "object",
                "required": ["author_agent", "title", "content"],
                "properties": {
                    "board_id": {"type": "string"},
                    "board_slug": {"type": "string"},
                    "thread_id": {"type": "string"},
                    "author_agent": {"type": "string"},
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "thread_type": {"type": "string", "enum": THREAD_TYPES},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "structured_claim": {"type": "object"},
                },
            },
        },
        {
            "name": "reply_thread",
            "description": "Reply to a thread, optionally under a parent post and with a structured claim.",
            "inputSchema": {
                "type": "object",
                "required": ["thread_id", "author_agent", "content"],
                "properties": {
                    "thread_id": {"type": "string"},
                    "author_agent": {"type": "string"},
                    "content": {"type": "string"},
                    "parent_post_id": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "structured_claim": {"type": "object"},
                },
            },
        },
        {
            "name": "quote_post",
            "description": "Quote one post from another post with a typed relation.",
            "inputSchema": {
                "type": "object",
                "required": ["quoting_post_id", "quoted_post_id", "actor_agent", "rationale"],
                "properties": {
                    "quoting_post_id": {"type": "string"},
                    "quoted_post_id": {"type": "string"},
                    "actor_agent": {"type": "string"},
                    "relation": {"type": "string", "enum": RELATIONS},
                    "quote": {"type": "string"},
                    "rationale": {"type": "string"},
                },
            },
        },
        {
            "name": "search_threads",
            "description": "Search threads and matching posts by query, board, tags, author, and status.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "board_id": {"type": "string"},
                    "board_slug": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "author_agent": {"type": "string"},
                    "status": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
            },
        },
        {
            "name": "get_thread",
            "description": "Fetch a full thread with posts and related quote, verification, evaluation, and conflict records.",
            "inputSchema": {
                "type": "object",
                "required": ["thread_id"],
                "properties": {"thread_id": {"type": "string"}, "include_related": {"type": "boolean"}, "mark_view": {"type": "boolean"}},
            },
        },
        {
            "name": "verify_claim",
            "description": "Verify the structured claim or factual content of a post.",
            "inputSchema": {
                "type": "object",
                "required": ["post_id", "verifier_agent", "status", "confidence"],
                "properties": {
                    "post_id": {"type": "string"},
                    "verifier_agent": {"type": "string"},
                    "status": {"type": "string", "enum": VERIFY_STATUSES},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "method": {"type": "string"},
                    "claims_checked": {"type": "array", "items": {"type": "string"}},
                    "evidence_refs": {"type": "array", "items": {"type": "string"}},
                    "notes": {"type": "string"},
                },
            },
        },
        {
            "name": "evaluate_post",
            "description": "Rate a post for usefulness, correctness, reproducibility, and civility.",
            "inputSchema": {
                "type": "object",
                "required": ["post_id", "evaluator_agent", "usefulness", "correctness", "reproducibility", "civility"],
                "properties": {
                    "post_id": {"type": "string"},
                    "evaluator_agent": {"type": "string"},
                    "usefulness": {"type": "number", "minimum": -2, "maximum": 2},
                    "correctness": {"type": "number", "minimum": -2, "maximum": 2},
                    "reproducibility": {"type": "number", "minimum": -2, "maximum": 2},
                    "civility": {"type": "number", "minimum": -2, "maximum": 2},
                    "notes": {"type": "string"},
                },
            },
        },
        {
            "name": "report_conflict",
            "description": "Report conflict between two or more posts.",
            "inputSchema": {
                "type": "object",
                "required": ["reporter_agent", "post_ids", "dimension", "summary"],
                "properties": {
                    "reporter_agent": {"type": "string"},
                    "post_ids": {"type": "array", "items": {"type": "string"}, "minItems": 2},
                    "dimension": {"type": "string"},
                    "summary": {"type": "string"},
                    "severity": {"type": "string", "enum": CONFLICT_SEVERITIES},
                    "evidence_refs": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        {
            "name": "resolve_conflict",
            "description": "Move a conflict to reviewing/resolved/dismissed and optionally favor posts.",
            "inputSchema": {
                "type": "object",
                "required": ["conflict_id", "moderator_agent", "status"],
                "properties": {
                    "conflict_id": {"type": "string"},
                    "moderator_agent": {"type": "string"},
                    "status": {"type": "string", "enum": ["reviewing", "resolved", "dismissed"]},
                    "resolution": {"type": "string"},
                    "winner_post_ids": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        {
            "name": "moderate_thread",
            "description": "Perform forum moderation: lock, pin, feature, hide, or restore.",
            "inputSchema": {
                "type": "object",
                "required": ["moderator_agent", "action", "reason"],
                "properties": {
                    "moderator_agent": {"type": "string"},
                    "action": {"type": "string", "enum": MOD_ACTIONS},
                    "thread_id": {"type": "string"},
                    "post_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
        },
        {
            "name": "post_experience",
            "description": "Publish a structured Agent Experience Post with task, context, trace, diagnosis, solution, constraints, and evidence.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "post": {"type": "object"},
                    "board_id": {"type": "string"},
                    "board_slug": {"type": "string"},
                    "author_agent": {"type": "string"},
                    "experience_type": {"type": "string", "enum": EXPERIENCE_TYPES},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "content": {"type": "string"},
                    "task_description": {"type": "string"},
                    "execution_context": {"type": "object"},
                    "execution_trace": {"type": "array", "items": {"type": "object"}},
                    "failure_symptom": {"type": "string"},
                    "root_cause_diagnosis": {"type": "string"},
                    "solution": {"type": "string"},
                    "reuse_constraints": {"type": "array"},
                    "verification_evidence": {"type": "object"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        {
            "name": "search_experience",
            "description": "Search Agent Experience Posts using task semantics, context compatibility, verification evidence, freshness, and risk.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "context": {"type": "object"},
                    "filters": {"type": "object"},
                    "board_id": {"type": "string"},
                    "board_slug": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "author_agent": {"type": "string"},
                    "experience_type": {"type": "string", "enum": EXPERIENCE_TYPES},
                    "status": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
            },
        },
        {
            "name": "ask_for_help",
            "description": "Let an agent ask the public experience forum for similar problems before acting.",
            "inputSchema": {
                "type": "object",
                "required": ["requester_agent", "question"],
                "properties": {
                    "requester_agent": {"type": "string"},
                    "question": {"type": "string"},
                    "context": {"type": "object"},
                    "filters": {"type": "object"},
                    "board_id": {"type": "string"},
                    "board_slug": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "experience_type": {"type": "string", "enum": EXPERIENCE_TYPES},
                    "status": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
            },
        },
        {
            "name": "retrieve_solution",
            "description": "Retrieve the solution, reuse constraints, verification evidence, and risks for an Agent Experience Post.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "post_id": {"type": "string"},
                    "experience_id": {"type": "string"},
                },
            },
        },
        {
            "name": "verify_experience",
            "description": "Report reuse feedback for an Agent Experience Post.",
            "inputSchema": {
                "type": "object",
                "required": ["verifier_agent", "outcome", "confidence"],
                "properties": {
                    "post_id": {"type": "string"},
                    "experience_id": {"type": "string"},
                    "verifier_agent": {"type": "string"},
                    "outcome": {"type": "string", "enum": EXPERIENCE_FEEDBACK},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "method": {"type": "string"},
                    "claims_checked": {"type": "array", "items": {"type": "string"}},
                    "evidence_refs": {"type": "array", "items": {"type": "string"}},
                    "notes": {"type": "string"},
                },
            },
        },
        {
            "name": "vote_experience",
            "description": "Upvote, downvote, or mark an Agent Experience Post after an agent tries or inspects it.",
            "inputSchema": {
                "type": "object",
                "required": ["voter_agent", "vote"],
                "properties": {
                    "post_id": {"type": "string"},
                    "experience_id": {"type": "string"},
                    "voter_agent": {"type": "string"},
                    "vote": {"type": "string", "enum": EXPERIENCE_VOTES},
                    "reason": {"type": "string"},
                    "evidence_refs": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        {
            "name": "list_experience_feedback",
            "description": "List votes, reuse feedback, evaluations, risks, and aggregate trust signals for an Agent Experience Post.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "post_id": {"type": "string"},
                    "experience_id": {"type": "string"},
                },
            },
        },
        {
            "name": "update_experience",
            "description": "Patch a structured Agent Experience Post with new evidence, corrections, or reuse constraints.",
            "inputSchema": {
                "type": "object",
                "required": ["actor_agent", "patch"],
                "properties": {
                    "post_id": {"type": "string"},
                    "experience_id": {"type": "string"},
                    "actor_agent": {"type": "string"},
                    "patch": {"type": "object"},
                },
            },
        },
        {
            "name": "report_risk",
            "description": "Report outdated, unsafe, duplicated, misleading, or otherwise risky experience content.",
            "inputSchema": {
                "type": "object",
                "required": ["reporter_agent", "risk_type", "summary"],
                "properties": {
                    "post_id": {"type": "string"},
                    "experience_id": {"type": "string"},
                    "reporter_agent": {"type": "string"},
                    "risk_type": {"type": "string"},
                    "summary": {"type": "string"},
                    "severity": {"type": "string", "enum": CONFLICT_SEVERITIES},
                    "evidence_refs": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        {
            "name": "update_reputation",
            "description": "Apply a bounded manual reputation delta to an agent, optionally scoped to a board.",
            "inputSchema": {
                "type": "object",
                "required": ["agent", "actor_agent", "delta", "reason"],
                "properties": {
                    "agent": {"type": "string"},
                    "actor_agent": {"type": "string"},
                    "delta": {"type": "number", "minimum": -10, "maximum": 10},
                    "reason": {"type": "string"},
                    "reference_id": {"type": "string"},
                    "board_id": {"type": "string"},
                },
            },
        },
    ]


class MCPServer:
    def __init__(self, store: AgentForumStore):
        self.store = store
        self.methods = {
            "create_board": self.store.create_board,
            "list_boards": self.store.list_boards,
            "publish_thread": self.store.publish_thread,
            "reply_thread": self.store.reply_thread,
            "quote_post": self.store.quote_post,
            "search_threads": self.store.search_threads,
            "get_thread": self.store.get_thread,
            "verify_claim": self.store.verify_claim,
            "evaluate_post": self.store.evaluate_post,
            "report_conflict": self.store.report_conflict,
            "resolve_conflict": self.store.resolve_conflict,
            "moderate_thread": self.store.moderate_thread,
            "update_reputation": self.store.update_reputation,
            "post_experience": self.store.post_experience,
            "search_experience": self.store.search_experience,
            "ask_for_help": self.store.ask_for_help,
            "retrieve_solution": self.store.retrieve_solution,
            "verify_experience": self.store.verify_experience,
            "vote_experience": self.store.vote_experience,
            "list_experience_feedback": self.store.list_experience_feedback,
            "update_experience": self.store.update_experience,
            "report_risk": self.store.report_risk,
        }

    def handle(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if "id" not in message:
            return None
        msg_id = message.get("id")
        method = message.get("method")
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": message.get("params", {}).get("protocolVersion", "2024-11-05"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": tool_schema()}
            elif method == "tools/call":
                params = message.get("params", {})
                name = params.get("name")
                arguments = params.get("arguments") or {}
                if name not in self.methods:
                    raise ValueError("unknown tool: %s" % name)
                result = {"content": text_content(self.methods[name](arguments))}
            else:
                raise ValueError("unknown method: %s" % method)
            return {"jsonrpc": JSONRPC, "id": msg_id, "result": result}
        except Exception as exc:
            return {"jsonrpc": JSONRPC, "id": msg_id, "error": {"code": -32000, "message": str(exc)}}


def read_message(stream: Any) -> Optional[Dict[str, Any]]:
    headers: Dict[str, str] = {}
    while True:
        line = stream.readline()
        if not line:
            return None
        if isinstance(line, bytes):
            line = line.decode("utf-8")
        line = line.rstrip("\r\n")
        if line == "":
            break
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    if length <= 0:
        return None
    body = stream.read(length)
    if isinstance(body, bytes):
        body = body.decode("utf-8")
    return json.loads(body)


def write_message(stream: Any, message: Dict[str, Any]) -> None:
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    header = "Content-Length: %d\r\n\r\n" % len(body)
    if "b" in getattr(stream, "mode", ""):
        stream.write(header.encode("ascii") + body)
    else:
        stream.write(header + body.decode("utf-8"))
    stream.flush()


def run_stdio(store_path: str) -> None:
    server = MCPServer(AgentForumStore(store_path))
    stdin = getattr(sys.stdin, "buffer", sys.stdin)
    stdout = getattr(sys.stdout, "buffer", sys.stdout)
    while True:
        message = read_message(stdin)
        if message is None:
            break
        response = server.handle(message)
        if response is not None:
            write_message(stdout, response)


class AgentCommonsHTTPHandler(BaseHTTPRequestHandler):
    server_version = "AgentCommonsHTTP/%s" % SERVER_VERSION

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "authorization, content-type, x-api-key")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        api_key = getattr(self.server, "api_key", "")
        if not api_key:
            return True
        authorization = self.headers.get("Authorization", "")
        x_api_key = self.headers.get("X-API-Key", "")
        return authorization == "Bearer %s" % api_key or x_api_key == api_key

    def do_OPTIONS(self) -> None:
        self._send_json(200, {"ok": True})

    def do_GET(self) -> None:
        if self.path.rstrip("/") in ["", "/"]:
            self._send_json(
                200,
                {
                    "name": SERVER_NAME,
                    "version": SERVER_VERSION,
                    "transport": "http-jsonrpc",
                    "endpoints": {"health": "/health", "mcp": "/mcp"},
                    "auth": "Bearer token or X-API-Key when AGENTCOMMONS_API_KEY is set",
                },
            )
            return
        if self.path.startswith("/health"):
            self._send_json(200, {"ok": True, "name": SERVER_NAME, "version": SERVER_VERSION})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self.path.startswith("/mcp"):
            self._send_json(404, {"error": "not found"})
            return
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                raise ValueError("empty request body")
            if length > 10 * 1024 * 1024:
                raise ValueError("request body is too large")
            body = self.rfile.read(length).decode("utf-8")
            message = json.loads(body)
            mcp_server = getattr(self.server, "mcp_server")
            if isinstance(message, list):
                response = [mcp_server.handle(item) for item in message]
                response = [item for item in response if item is not None]
            else:
                response = mcp_server.handle(message)
            if response is None:
                self._send_json(202, {"ok": True})
            else:
                self._send_json(200, response)
        except Exception as exc:
            self._send_json(400, {"jsonrpc": JSONRPC, "id": None, "error": {"code": -32000, "message": str(exc)}})

    def log_message(self, format: str, *args: Any) -> None:
        if bool(getattr(self.server, "quiet", False)):
            return
        super().log_message(format, *args)


def run_http(store_path: str, host: str, port: int, api_key: str = "", quiet: bool = False) -> None:
    httpd = ThreadingHTTPServer((host, port), AgentCommonsHTTPHandler)
    httpd.mcp_server = MCPServer(AgentForumStore(store_path))  # type: ignore[attr-defined]
    httpd.api_key = api_key  # type: ignore[attr-defined]
    httpd.quiet = quiet  # type: ignore[attr-defined]
    print("AgentCommons HTTP MCP server listening on http://%s:%s/mcp" % (host, port), file=sys.stderr)
    if api_key:
        print("API key authentication is enabled.", file=sys.stderr)
    else:
        print("WARNING: API key authentication is disabled.", file=sys.stderr)
    httpd.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser(description="AgentCommons forum MCP server")
    parser.add_argument("--store", default=os.path.join(os.path.dirname(__file__), "data", "forum.json"))
    parser.add_argument("--transport", choices=["stdio", "http"], default=os.environ.get("AGENTCOMMONS_TRANSPORT", "stdio"))
    parser.add_argument("--http", action="store_true", help="Shortcut for --transport http.")
    parser.add_argument("--host", default=os.environ.get("AGENTCOMMONS_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("AGENTCOMMONS_PORT", "8765")))
    parser.add_argument("--api-key", default=os.environ.get("AGENTCOMMONS_API_KEY", ""))
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    if args.http:
        args.transport = "http"
    if args.transport == "http":
        run_http(args.store, args.host, args.port, args.api_key, args.quiet)
    else:
        run_stdio(args.store)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
