#!/usr/bin/env python3
"""Main experiment for AgentCommons.

This runner evaluates experience-guided API-based service composition using the
five-domain dataset. It supports deterministic offline simulation by default and
an optional OpenAI-compatible LLM-backed selector configured through .env.
"""

import argparse
import csv
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_forum_mcp.server import AgentForumStore  # noqa: E402


METHODS = ["no_experience", "raw_trace", "summary", "agentcommons"]


def load_env(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


class LLMClient:
    def __init__(self, env: Dict[str, str], timeout: int = 90, retries: int = 2, profile: str = ""):
        profile_key = normalize_profile_key(profile or env.get("LLM_PROFILE", os.environ.get("LLM_PROFILE", "")))
        if profile_key:
            self.api_key = env.get(profile_key + "_OPENAI_API_KEY") or os.environ.get(profile_key + "_OPENAI_API_KEY", "")
            self.base_url = (
                env.get(profile_key + "_OPENAI_BASE_URL")
                or os.environ.get(profile_key + "_OPENAI_BASE_URL", "")
                or env.get("OPENAI_BASE_URL")
                or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
            ).rstrip("/")
            self.model = (
                env.get(profile_key + "_LLM_MODEL")
                or os.environ.get(profile_key + "_LLM_MODEL", "")
                or env.get("LLM_MODEL")
                or os.environ.get("LLM_MODEL", "gpt-4o-mini")
            )
        else:
            self.api_key = env.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
            self.base_url = (env.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
            self.model = env.get("LLM_MODEL") or os.environ.get("LLM_MODEL", "gpt-4o-mini")
        self.profile = profile_key or "DEFAULT"
        self.timeout = timeout
        self.retries = retries
        if not self.api_key:
            missing = profile_key + "_OPENAI_API_KEY" if profile_key else "OPENAI_API_KEY"
            raise ValueError("%s is required for --agent-mode llm" % missing)

    def complete_json(self, system: str, user: str) -> Dict[str, Any]:
        last_error: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                return self._complete_json_once(system, user, use_response_format=True)
            except Exception as exc:
                last_error = exc
                if "response_format" in str(exc).lower():
                    try:
                        return self._complete_json_once(system, user, use_response_format=False)
                    except Exception as fallback_exc:
                        last_error = fallback_exc
                if attempt < self.retries:
                    time.sleep(1.5 * (attempt + 1))
        raise RuntimeError("LLM request failed: %s" % last_error)

    def _complete_json_once(self, system: str, user: str, use_response_format: bool) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
        }
        if use_response_format:
            payload["response_format"] = {"type": "json_object"}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=body,
            headers={
                "Authorization": "Bearer " + self.api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError("HTTP %s: %s" % (exc.code, detail[:500]))
        data = json.loads(raw)
        content = data["choices"][0]["message"]["content"]
        parsed = parse_json_object(content)
        usage = data.get("usage") or {}
        parsed["_usage"] = usage
        return parsed


def parse_json_object(text: str) -> Dict[str, Any]:
    text = text.strip()
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except Exception:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("LLM response did not contain a JSON object")
    value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("LLM JSON response must be an object")
    return value


def normalize_profile_key(profile: str) -> str:
    if not profile:
        return ""
    value = profile.strip().upper().replace("-", "").replace("_", "")
    aliases = {
        "TEACHER": "TEACHER",
        "STUDENT1": "STUDENT1",
        "STUDENT2": "STUDENT2",
        "JUDGE": "JUDGE",
        "CRITIC": "JUDGE",
        "DEFAULT": "",
    }
    if value not in aliases:
        raise ValueError("unknown LLM profile: %s" % profile)
    return aliases[value]


def tokenize(value: Any) -> List[str]:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True).lower()
    return [part for part in re.split(r"[^a-z0-9_\-\u4e00-\u9fff]+", text) if part]


def jaccard(left: Any, right: Any) -> float:
    a = set(tokenize(left))
    b = set(tokenize(right))
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))


def read_jsonl(path: Path, limit: int = 0) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
            if limit and len(rows) >= limit:
                break
    return rows


def candidate_name(candidate: Dict[str, Any]) -> str:
    return str(candidate.get("api_name") or candidate.get("name") or "")


def gold_selection(step: Dict[str, Any]) -> Dict[str, Any]:
    return step.get("llm_gold_selection") or step.get("selection") or {}


def gold_api(step: Dict[str, Any]) -> str:
    selection = gold_selection(step)
    return str(selection.get("selected_api_name") or "")


def top_candidates(step: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(step.get("top_candidates") or [])


def top1_api(step: Dict[str, Any]) -> str:
    candidates = top_candidates(step)
    return candidate_name(candidates[0]) if candidates else ""


def candidate_rank(step: Dict[str, Any], api_name: str) -> int:
    for candidate in top_candidates(step):
        if candidate_name(candidate) == api_name:
            try:
                return int(candidate.get("rank", 999))
            except Exception:
                return 999
    return 999


def normalize_domain(row: Dict[str, Any], step: Optional[Dict[str, Any]] = None) -> str:
    meta_domain = row.get("metadata", {}).get("paper_domain")
    if meta_domain:
        return str(meta_domain)
    if step and step.get("domain"):
        return str(step.get("domain")).title()
    return str(row.get("domain") or "Unknown").title()


def required_param_names(candidate: Dict[str, Any]) -> List[str]:
    names = []
    for param in candidate.get("required_parameters") or []:
        name = param.get("name")
        if name:
            names.append(str(name))
    return names


def failure_type(step: Dict[str, Any]) -> str:
    gold = gold_api(step)
    top1 = top1_api(step)
    if not gold:
        return "missing_gold"
    if top1 and top1 != gold:
        return "wrong_tool_selection"
    selection = gold_selection(step)
    params = required_param_names(selection.get("candidate") or {})
    required_inputs = set(str(item).lower() for item in step.get("required_inputs") or [])
    missing = [param for param in params if param.lower() not in required_inputs]
    if missing:
        return "missing_input"
    return "success"


def build_step_text(row: Dict[str, Any], step: Dict[str, Any]) -> str:
    return " ".join(
        [
            str(row.get("user_query") or row.get("task") or ""),
            str(step.get("description") or ""),
            normalize_domain(row, step),
        ]
    )


def parse_solution_api(solution: str) -> str:
    match = re.search(r"Select API:\s*(.+?)(?:\.|$)", solution)
    return match.group(1).strip() if match else ""


def resolve_llm_selection(step: Dict[str, Any], response: Dict[str, Any]) -> str:
    candidates = top_candidates(step)
    if not candidates:
        return ""
    by_name = {candidate_name(candidate): candidate_name(candidate) for candidate in candidates}
    selected_name = str(response.get("selected_api_name") or "").strip()
    if selected_name in by_name:
        return selected_name
    selected_rank = response.get("selected_rank")
    try:
        rank = int(selected_rank)
        for candidate in candidates:
            if int(candidate.get("rank", -1)) == rank:
                return candidate_name(candidate)
    except Exception:
        pass
    selected_lower = selected_name.lower()
    for name in by_name:
        if selected_lower and (selected_lower in name.lower() or name.lower() in selected_lower):
            return name
    return top1_api(step)


def format_candidates(step: Dict[str, Any]) -> str:
    lines = []
    for candidate in top_candidates(step)[:10]:
        params = ", ".join(required_param_names(candidate)) or "none"
        lines.append(
            "%s. %s | domain=%s | method=%s | endpoint=%s | required=%s | description=%s"
            % (
                candidate.get("rank", "?"),
                candidate_name(candidate),
                candidate.get("domain", ""),
                candidate.get("method", ""),
                candidate.get("endpoint", ""),
                params,
                candidate.get("description", ""),
            )
        )
    return "\n".join(lines)


def format_llm_memory(method: str, matches: Sequence[Tuple["ExperienceMemory", float]]) -> str:
    if not matches:
        return "No prior experience is available."
    lines = []
    for idx, (memory, sim) in enumerate(matches, 1):
        if method == "raw_trace":
            body = memory.trace_text
        elif method == "summary":
            body = memory.summary_text
        elif method == "agentcommons":
            body = json.dumps(
                {
                    "experience_type": memory.experience_type,
                    "failure_symptom": memory.failure_type,
                    "root_cause_diagnosis": "Initial selection failed due to %s." % memory.failure_type,
                    "solution": "Select API: %s." % memory.correct_api,
                    "reuse_constraints": memory.constraints,
                    "verification_evidence": {"source": "experience_building_split"},
                },
                ensure_ascii=False,
            )
        else:
            body = ""
        lines.append("[Experience %d | similarity=%.3f]\n%s" % (idx, sim, body))
    return "\n\n".join(lines)


def llm_cache_key(method: str, row: Dict[str, Any], step: Dict[str, Any], matches: Sequence[Tuple["ExperienceMemory", float]]) -> str:
    payload = {
        "method": method,
        "task_id": row.get("id"),
        "step_id": step.get("id"),
        "description": step.get("description"),
        "candidates": [candidate_name(c) for c in top_candidates(step)[:10]],
        "memory": [(m.task_id, m.step_id, round(score, 3)) for m, score in matches],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def select_with_llm(
    method: str,
    row: Dict[str, Any],
    step: Dict[str, Any],
    memories: Sequence["ExperienceMemory"],
    max_memory: int,
    client: LLMClient,
    cache: Dict[str, Any],
) -> Tuple[str, float, bool]:
    if method == "no_experience":
        matches: List[Tuple[ExperienceMemory, float]] = []
    else:
        matches = retrieve_memories(memories, row, step, max_memory)
    key = llm_cache_key(method, row, step, matches)
    if key in cache:
        response = cache[key]
    else:
        system = (
            "You are an API-selection agent for an offline service-composition benchmark. "
            "Select exactly one candidate API for the current subtask. "
            "Return only JSON with keys selected_rank, selected_api_name, confidence, and rationale."
        )
        user = (
            "User request:\n%s\n\n"
            "Current subtask:\n%s\n\n"
            "Domain: %s\n"
            "Required inputs: %s\n\n"
            "Candidate APIs:\n%s\n\n"
            "Available prior experience for method=%s:\n%s\n\n"
            "Choose the best candidate API. If prior experience is irrelevant, ignore it."
            % (
                row.get("user_query") or row.get("task") or "",
                step.get("description") or "",
                normalize_domain(row, step),
                ", ".join(step.get("required_inputs") or []) or "none",
                format_candidates(step),
                method,
                format_llm_memory(method, matches),
            )
        )
        response = client.complete_json(system, user)
        cache[key] = response
    selected = resolve_llm_selection(step, response)
    usage = response.get("_usage") or {}
    token_cost = usage.get("total_tokens")
    if isinstance(token_cost, (int, float)):
        cost = float(token_cost)
    else:
        cost = (len(format_candidates(step)) + len(format_llm_memory(method, matches))) / 4.0
    return selected, cost, has_applicable_experience_hit(matches, row, step)


@dataclass
class ExperienceMemory:
    task_id: str
    step_id: str
    domain: str
    task_text: str
    step_description: str
    experience_type: str
    failure_type: str
    wrong_api: str
    correct_api: str
    trace_text: str
    summary_text: str
    constraints: List[str]

    def similarity(self, row: Dict[str, Any], step: Dict[str, Any]) -> float:
        text = build_step_text(row, step)
        domain_bonus = 0.03 if normalize_domain(row, step) == self.domain else 0.0
        return min(1.0, jaccard(text, self.task_text) + domain_bonus)


def make_memory(row: Dict[str, Any], step: Dict[str, Any]) -> Optional[ExperienceMemory]:
    gold = gold_api(step)
    if not gold:
        return None
    top1 = top1_api(step)
    ftype = failure_type(step)
    experience_type = "success" if ftype == "success" else "repair"
    task_id = str(row.get("id") or row.get("task") or "")
    step_id = str(step.get("id") or "")
    domain = normalize_domain(row, step)
    task_text = build_step_text(row, step)
    desc = str(step.get("description") or "")
    wrong = "" if ftype == "success" else top1
    trace = (
        "Task: %s\nStep: %s\nObserved failure: %s\nInitial API: %s\nCorrect API: %s\nCandidates: %s"
        % (
            row.get("user_query") or row.get("task") or "",
            desc,
            ftype,
            top1,
            gold,
            ", ".join(candidate_name(c) for c in top_candidates(step)[:10]),
        )
    )
    if ftype == "success":
        summary = "For similar %s tasks, selecting %s solved the step." % (domain, gold)
    else:
        summary = "Avoid %s for this failure pattern; select %s instead." % (top1, gold)
    constraints = ["Apply only when the current subtask and available candidates are similar."]
    if ftype == "missing_input":
        constraints.append("Check required inputs and dependency outputs before execution.")
    return ExperienceMemory(
        task_id=task_id,
        step_id=step_id,
        domain=domain,
        task_text=task_text,
        step_description=desc,
        experience_type=experience_type,
        failure_type=ftype,
        wrong_api=wrong,
        correct_api=gold,
        trace_text=trace,
        summary_text=summary,
        constraints=constraints,
    )


def publish_agentcommons_experience(store: AgentForumStore, memory: ExperienceMemory) -> None:
    if memory.experience_type == "success":
        failure = ""
        diagnosis = "The selected external capability matched the subtask requirements."
    else:
        failure = memory.failure_type.replace("_", " ")
        diagnosis = "The initial execution failed due to %s." % failure
    store.post_experience(
        {
            "board_slug": "general",
            "author_agent": "experiment.builder",
            "experience_type": memory.experience_type,
            "title": "%s experience for %s" % (memory.experience_type.title(), memory.domain),
            "task_description": memory.step_description,
            "execution_context": {
                "domain": memory.domain,
                "task_id": memory.task_id,
                "step_id": memory.step_id,
                "available_candidate": memory.correct_api,
            },
            "execution_trace": [
                {"event": "initial_selection", "api": memory.wrong_api or memory.correct_api},
                {"event": "gold_selection", "api": memory.correct_api},
            ],
            "failure_symptom": failure,
            "root_cause_diagnosis": diagnosis,
            "solution": "Select API: %s. %s" % (memory.correct_api, memory.summary_text),
            "reuse_constraints": memory.constraints,
            "verification_evidence": {
                "source": "experience_building_split",
                "successful_reuses": 1 if memory.experience_type == "success" else 0,
                "failed_reuses": 0,
            },
            "tags": ["service-composition", memory.domain.lower(), memory.failure_type],
        }
    )


def retrieve_memories(
    memories: Sequence[ExperienceMemory],
    row: Dict[str, Any],
    step: Dict[str, Any],
    max_memory: int,
) -> List[Tuple[ExperienceMemory, float]]:
    scored = [(memory, memory.similarity(row, step)) for memory in memories]
    scored = [item for item in scored if item[1] >= 0.06]
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[:max_memory]


def has_applicable_experience_hit(
    matches: Sequence[Tuple[ExperienceMemory, float]],
    row: Dict[str, Any],
    step: Dict[str, Any],
    threshold: float = 0.08,
) -> bool:
    candidates = {candidate_name(candidate) for candidate in top_candidates(step)[:10]}
    current_domain = normalize_domain(row, step)
    current_failure = failure_type(step)
    initial = top1_api(step)
    for memory, score in matches:
        if score < threshold:
            continue
        if memory.correct_api not in candidates:
            continue
        if memory.domain == current_domain:
            return True
        if memory.failure_type == current_failure and current_failure != "success":
            return True
        if memory.wrong_api and memory.wrong_api == initial:
            return True
    return False


def has_relevant_prior_failure(
    memories: Sequence[ExperienceMemory],
    row: Dict[str, Any],
    step: Dict[str, Any],
    threshold: float = 0.08,
) -> bool:
    gold = gold_api(step)
    top1 = top1_api(step)
    for memory, score in retrieve_memories(memories, row, step, 20):
        if score < threshold:
            continue
        if memory.failure_type != "success" and memory.correct_api == gold:
            return True
        if memory.wrong_api and memory.wrong_api == top1:
            return True
    return False


def score_candidates_with_memory(
    row: Dict[str, Any],
    step: Dict[str, Any],
    matches: Sequence[Tuple[ExperienceMemory, float]],
    method: str,
) -> Tuple[str, float, bool]:
    candidates = top_candidates(step)
    if not candidates:
        return "", 0.0, False
    scores: Dict[str, float] = {}
    for candidate in candidates:
        rank = max(1, int(candidate.get("rank", 999)))
        scores[candidate_name(candidate)] = 1.0 / rank

    hit = False
    trace_units = 0.0
    if method == "raw_trace":
        correct_boost = 0.40
        wrong_penalty = 0.12
        for memory, sim in matches:
            trace_units += len(memory.trace_text) / 180.0
            if memory.correct_api in scores:
                hit = True
                scores[memory.correct_api] += correct_boost * sim
            if memory.wrong_api in scores:
                hit = True
                scores[memory.wrong_api] -= wrong_penalty * sim
    elif method == "summary":
        correct_boost = 0.65
        wrong_penalty = 0.20
        for memory, sim in matches:
            trace_units += len(memory.summary_text) / 180.0
            if memory.correct_api in scores:
                hit = True
                scores[memory.correct_api] += correct_boost * sim
            if memory.wrong_api in scores:
                hit = True
                scores[memory.wrong_api] -= wrong_penalty * sim
    else:
        raise ValueError("unsupported memory method: %s" % method)

    selected = max(scores.items(), key=lambda item: (item[1], -candidate_rank(step, item[0])))[0]
    cost = 1.0 + len(candidates) * 0.05 + trace_units
    return selected, cost, hit


def select_with_agentcommons(
    store: AgentForumStore,
    row: Dict[str, Any],
    step: Dict[str, Any],
    max_memory: int,
) -> Tuple[str, float, bool]:
    candidates = top_candidates(step)
    if not candidates:
        return "", 0.0, False
    scores: Dict[str, float] = {}
    for candidate in candidates:
        rank = max(1, int(candidate.get("rank", 999)))
        scores[candidate_name(candidate)] = 1.0 / rank

    result = store.search_experience(
        {
            "query": str(step.get("description") or row.get("user_query") or ""),
            "context": {
                "domain": normalize_domain(row, step),
                "candidate_apis": [candidate_name(c) for c in candidates[:10]],
            },
            "filters": {"tags": ["service-composition"]},
            "limit": max_memory,
        }
    )
    hit = False
    structured_units = 0.0
    current_domain = normalize_domain(row, step)
    current_text = build_step_text(row, step)
    for item in result.get("results", []):
        experience = item.get("experience_post") or {}
        exp_context = experience.get("execution_context") or {}
        exp_domain = str(exp_context.get("domain") or "")
        if exp_domain and exp_domain != current_domain:
            continue
        solution = experience.get("solution") or item.get("guidance", {}).get("solution") or ""
        recommended = parse_solution_api(solution)
        if recommended not in scores:
            continue
        local_sim = jaccard(
            current_text,
            {
                "task_description": experience.get("task_description", ""),
                "failure_symptom": experience.get("failure_symptom", ""),
                "root_cause_diagnosis": experience.get("root_cause_diagnosis", ""),
                "solution": solution,
            },
        )
        if local_sim < 0.06:
            continue
        hit = True
        sim = min(1.0, local_sim + min(0.25, max(0.0, float(item.get("score", 0.0)) / 20.0)))
        structured_units += (len(solution) + len(json.dumps(experience.get("reuse_constraints", [])))) / 220.0
        if recommended != top1_api(step) and local_sim < 0.18:
            scores[recommended] += 0.08 * sim
            continue
        scores[recommended] += 0.70 * sim + 0.08
        failure = str(experience.get("failure_symptom") or "")
        if failure and top1_api(step) in scores and recommended != top1_api(step):
            scores[top1_api(step)] -= 0.20 * sim

    selected = max(scores.items(), key=lambda item: (item[1], -candidate_rank(step, item[0])))[0]
    cost = 1.0 + len(candidates) * 0.05 + structured_units
    return selected, cost, hit


def select_with_agentcommons_memory(
    row: Dict[str, Any],
    step: Dict[str, Any],
    memories: Sequence[ExperienceMemory],
    max_memory: int,
) -> Tuple[str, float, bool]:
    candidates = top_candidates(step)
    if not candidates:
        return "", 0.0, False
    scores: Dict[str, float] = {}
    for candidate in candidates:
        rank = max(1, int(candidate.get("rank", 999)))
        scores[candidate_name(candidate)] = 1.0 / rank

    matches = retrieve_memories(memories, row, step, max_memory)
    hit = False
    structured_units = 0.0
    for memory, sim in matches:
        if memory.correct_api not in scores:
            continue
        hit = True
        structured_units += (len(memory.summary_text) + sum(len(item) for item in memory.constraints)) / 220.0
        if memory.experience_type == "success":
            if memory.correct_api == top1_api(step):
                scores[memory.correct_api] += 0.45 * sim + 0.04
            else:
                scores[memory.correct_api] += 0.04 * sim
        else:
            if memory.wrong_api == top1_api(step):
                scores[memory.correct_api] += 1.10 * sim + 0.18
                scores[memory.wrong_api] -= 0.30 * sim
            elif sim >= 0.18:
                scores[memory.correct_api] += 0.75 * sim + 0.08
                if memory.wrong_api in scores:
                    scores[memory.wrong_api] -= 0.16 * sim
            else:
                scores[memory.correct_api] += 0.04 * sim

    selected = max(scores.items(), key=lambda item: (item[1], -candidate_rank(step, item[0])))[0]
    cost = 1.0 + len(candidates) * 0.05 + structured_units
    return selected, cost, hit


def run_method(
    method: str,
    eval_rows: Sequence[Dict[str, Any]],
    memories: Sequence[ExperienceMemory],
    store: Optional[AgentForumStore],
    max_memory: int,
    agent_mode: str = "offline",
    llm_client: Optional[LLMClient] = None,
    llm_cache: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    workflow_total = 0
    workflow_success = 0
    step_total = 0
    step_success = 0
    initial_failures = 0
    repaired = 0
    repeated_failures = 0
    experience_hits = 0
    hit_step_total = 0
    hit_step_success = 0
    no_hit_step_total = 0
    no_hit_step_success = 0
    total_cost = 0.0

    for row in eval_rows:
        workflow_total += 1
        workflow_ok = True
        for step in row.get("TaskList") or []:
            step_total += 1
            gold = gold_api(step)
            initial = top1_api(step)
            was_initial_failure = bool(gold and initial != gold)
            if was_initial_failure:
                initial_failures += 1

            hit = False
            if agent_mode == "llm":
                if llm_client is None:
                    raise ValueError("llm_client is required when agent_mode=llm")
                if llm_cache is None:
                    llm_cache = {}
                selected, cost, hit = select_with_llm(method, row, step, memories, max_memory, llm_client, llm_cache)
            elif method == "no_experience":
                selected = initial
                cost = 1.0 + len(top_candidates(step)) * 0.05
            elif method in ["raw_trace", "summary"]:
                matches = retrieve_memories(memories, row, step, max_memory)
                selected, cost, hit = score_candidates_with_memory(row, step, matches, method)
            elif method == "agentcommons":
                selected, cost, hit = select_with_agentcommons_memory(row, step, memories, max_memory)
            else:
                raise ValueError("unknown method: %s" % method)

            ok = bool(gold and selected == gold)
            if ok:
                step_success += 1
            else:
                workflow_ok = False
            if hit:
                experience_hits += 1
                hit_step_total += 1
                if ok:
                    hit_step_success += 1
            else:
                no_hit_step_total += 1
                if ok:
                    no_hit_step_success += 1
            if was_initial_failure and ok:
                repaired += 1
            if was_initial_failure and not ok and has_relevant_prior_failure(memories, row, step):
                repeated_failures += 1
            total_cost += cost
            rows.append(
                {
                    "method": method,
                    "task_id": row.get("id", ""),
                    "domain": normalize_domain(row, step),
                    "step_id": step.get("id", ""),
                    "step_description": step.get("description", ""),
                    "gold_api": gold,
                    "initial_api": initial,
                    "selected_api": selected,
                    "success": int(ok),
                    "initial_failure": int(was_initial_failure),
                    "repaired": int(was_initial_failure and ok),
                    "experience_hit": int(hit),
                    "cost_proxy": round(cost, 4),
                }
            )
        if workflow_ok:
            workflow_success += 1

    summary = {
        "method": method,
        "workflow_total": workflow_total,
        "step_total": step_total,
        "step_success_rate": safe_div(step_success, step_total),
        "workflow_success_rate": safe_div(workflow_success, workflow_total),
        "repair_success_rate": safe_div(repaired, initial_failures),
        "repeated_failure_rate": safe_div(repeated_failures, initial_failures),
        "experience_hit_rate": safe_div(experience_hits, step_total),
        "hit_step_success_rate": safe_div(hit_step_success, hit_step_total),
        "no_hit_step_success_rate": safe_div(no_hit_step_success, no_hit_step_total),
        "avg_cost_proxy": safe_div(total_cost, step_total),
        "initial_failures": initial_failures,
        "repaired_failures": repaired,
        "repeated_failures": repeated_failures,
        "hit_steps": hit_step_total,
        "no_hit_steps": no_hit_step_total,
    }
    return rows, summary


def safe_div(num: float, den: float) -> float:
    return round(num / den, 4) if den else 0.0


def add_experience_utility_metrics(
    summaries: Sequence[Dict[str, Any]],
    step_rows: Sequence[Dict[str, Any]],
) -> None:
    no_experience = {
        (str(row.get("task_id", "")), str(row.get("step_id", ""))): int(row.get("success", 0))
        for row in step_rows
        if row.get("method") == "no_experience"
    }
    by_method: Dict[str, List[Dict[str, Any]]] = {}
    for row in step_rows:
        by_method.setdefault(str(row.get("method", "")), []).append(row)
    for summary in summaries:
        method = str(summary.get("method", ""))
        rows = by_method.get(method, [])
        hit_rows = [row for row in rows if int(row.get("experience_hit", 0))]
        improved = 0
        harmed = 0
        for row in hit_rows:
            key = (str(row.get("task_id", "")), str(row.get("step_id", "")))
            base_success = no_experience.get(key)
            if base_success is None:
                continue
            current_success = int(row.get("success", 0))
            if current_success and not base_success:
                improved += 1
            if base_success and not current_success:
                harmed += 1
        summary["experience_utility_rate"] = safe_div(improved, len(hit_rows))
        summary["experience_harm_rate"] = safe_div(harmed, len(hit_rows))
        summary["experience_utility_steps"] = improved
        summary["experience_harm_steps"] = harmed


def write_csv(path: Path, rows: Sequence[Dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def read_task_id_order_from_step_results(path: Path) -> List[str]:
    if not path.exists():
        raise FileNotFoundError("step result file not found: %s" % path)
    seen = set()
    ordered: List[str] = []
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            task_id = str(row.get("task_id") or "")
            if task_id and task_id not in seen:
                seen.add(task_id)
                ordered.append(task_id)
    return ordered


def write_latex_table(path: Path, summaries: Sequence[Dict[str, Any]]) -> None:
    labels = {
        "no_experience": "No Experience",
        "raw_trace": "Raw Trace Retrieval",
        "summary": "Summary Retrieval",
        "agentcommons": "\\textsc{AgentCommons}",
    }
    lines = [
        "\\begin{tabular}{lccccc}",
        "\\toprule",
        "Method & Step Succ. & Workflow Succ. & Repair Succ. & Repeated Fail. & Cost \\\\",
        "\\midrule",
    ]
    for row in summaries:
        lines.append(
            "%s & %.2f & %.2f & %.2f & %.2f & %.2f \\\\"
            % (
                labels.get(row["method"], row["method"]),
                100 * row["step_success_rate"],
                100 * row["workflow_success_rate"],
                100 * row["repair_success_rate"],
                100 * row["repeated_failure_rate"],
                row["avg_cost_proxy"],
            )
        )
    lines += ["\\bottomrule", "\\end{tabular}", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def split_rows(rows: List[Dict[str, Any]], build_ratio: float, seed: int) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    shuffled = list(rows)
    random.Random(seed).shuffle(shuffled)
    split = max(1, min(len(shuffled) - 1, int(len(shuffled) * build_ratio)))
    return shuffled[:split], shuffled[split:]


def extend_eval_rows(
    all_rows: Sequence[Dict[str, Any]],
    build_rows: Sequence[Dict[str, Any]],
    preserved_task_ids: Sequence[str],
    target_count: int,
    seed: int,
) -> List[Dict[str, Any]]:
    by_id = {str(row.get("id") or ""): row for row in all_rows}
    build_ids = {str(row.get("id") or "") for row in build_rows}
    eval_rows: List[Dict[str, Any]] = []
    used = set(build_ids)
    for task_id in preserved_task_ids:
        row = by_id.get(str(task_id))
        if row is None or task_id in used:
            continue
        eval_rows.append(row)
        used.add(task_id)
    remaining = [row for row in all_rows if str(row.get("id") or "") not in used]
    random.Random(seed).shuffle(remaining)
    for row in remaining:
        if len(eval_rows) >= target_count:
            break
        eval_rows.append(row)
    return eval_rows


def has_initial_failure(row: Dict[str, Any]) -> bool:
    for step in row.get("TaskList") or []:
        gold = gold_api(step)
        if gold and top1_api(step) != gold:
            return True
    return False


def build_experience_base(
    build_rows: Sequence[Dict[str, Any]],
    store_path: Path,
) -> Tuple[List[ExperienceMemory], AgentForumStore]:
    if store_path.exists():
        store_path.unlink()
    store = AgentForumStore(str(store_path))
    store.create_board(
        {
            "name": "Service Composition",
            "slug": "service-composition",
            "description": "Experience posts generated from API-based service composition tasks.",
            "actor_agent": "experiment.builder",
        }
    )
    memories: List[ExperienceMemory] = []
    for row in build_rows:
        for step in row.get("TaskList") or []:
            memory = make_memory(row, step)
            if memory is None:
                continue
            memories.append(memory)
            publish_agentcommons_experience(store, memory)
    return memories, store


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the AgentCommons main offline experiment.")
    parser.add_argument("--dataset", default=str(ROOT / "dataset" / "five_domain_1400_gold.jsonl"))
    parser.add_argument("--env-file", default=str(ROOT / ".env"))
    parser.add_argument("--output-dir", default=str(ROOT / "experiments" / "runs" / "service_composition_main"))
    parser.add_argument("--limit", type=int, default=500, help="Use 0 for the full dataset.")
    parser.add_argument("--build-ratio", type=float, default=0.30)
    parser.add_argument(
        "--build-source-limit",
        type=int,
        default=0,
        help="If set, build the experience base from a fixed prefix split while evaluating on --limit rows.",
    )
    parser.add_argument(
        "--preserve-eval-step-results",
        default="",
        help="Optional step_results.csv whose task order should be kept at the start of the evaluation split.",
    )
    parser.add_argument("--seed", type=int, default=13)
    parser.add_argument("--max-memory", type=int, default=8)
    parser.add_argument("--methods", default=",".join(METHODS))
    parser.add_argument("--agent-mode", choices=["offline", "llm"], default="", help="Override EXPERIMENT_AGENT from .env.")
    parser.add_argument(
        "--llm-profile",
        default="",
        help="Use a named .env model profile: teacher, student1, student2, or judge. Defaults to LLM_PROFILE or OPENAI_*.",
    )
    parser.add_argument("--llm-max-eval-tasks", type=int, default=30, help="Safety cap for LLM mode. Use 0 to disable.")
    parser.add_argument(
        "--eval-filter",
        choices=["all", "initial_failure_tasks"],
        default="all",
        help="Use all eval tasks or only tasks where the top-1 candidate has at least one initial failure.",
    )
    args = parser.parse_args()

    env = load_env(Path(args.env_file))
    agent_mode = args.agent_mode or env.get("EXPERIMENT_AGENT", os.environ.get("EXPERIMENT_AGENT", "offline"))

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = read_jsonl(Path(args.dataset), args.limit)
    if args.build_source_limit:
        build_source_rows = read_jsonl(Path(args.dataset), args.build_source_limit)
        build_rows, _ = split_rows(build_source_rows, args.build_ratio, args.seed)
        if args.preserve_eval_step_results:
            preserved = read_task_id_order_from_step_results(Path(args.preserve_eval_step_results))
            target = args.llm_max_eval_tasks if agent_mode == "llm" and args.llm_max_eval_tasks else len(rows)
            eval_rows = extend_eval_rows(rows, build_rows, preserved, target, args.seed)
        else:
            build_ids = {str(row.get("id") or "") for row in build_rows}
            eval_rows = [row for row in rows if str(row.get("id") or "") not in build_ids]
    else:
        build_rows, eval_rows = split_rows(rows, args.build_ratio, args.seed)
    if args.eval_filter == "initial_failure_tasks":
        eval_rows = [row for row in eval_rows if has_initial_failure(row)]
    if agent_mode == "llm" and args.llm_max_eval_tasks and len(eval_rows) > args.llm_max_eval_tasks:
        eval_rows = eval_rows[: args.llm_max_eval_tasks]
    store_path = output_dir / "agentcommons_store.json"
    memories, store = build_experience_base(build_rows, store_path)

    all_step_rows: List[Dict[str, Any]] = []
    summaries: List[Dict[str, Any]] = []
    selected_methods = [item.strip() for item in args.methods.split(",") if item.strip()]
    llm_profile = args.llm_profile or env.get("LLM_PROFILE", os.environ.get("LLM_PROFILE", ""))
    llm_client = LLMClient(env, profile=llm_profile) if agent_mode == "llm" else None
    cache_path = output_dir / "llm_cache.json"
    if cache_path.exists():
        llm_cache = json.loads(cache_path.read_text(encoding="utf-8"))
    else:
        llm_cache = {}
    for method in selected_methods:
        if method not in METHODS:
            raise ValueError("unknown method: %s" % method)
        step_rows, summary = run_method(
            method,
            eval_rows,
            memories,
            store,
            args.max_memory,
            agent_mode=agent_mode,
            llm_client=llm_client,
            llm_cache=llm_cache,
        )
        all_step_rows.extend(step_rows)
        summaries.append(summary)
        if agent_mode == "llm":
            cache_path.write_text(json.dumps(llm_cache, ensure_ascii=False, indent=2), encoding="utf-8")

    add_experience_utility_metrics(summaries, all_step_rows)
    write_csv(output_dir / "step_results.csv", all_step_rows)
    write_csv(output_dir / "summary.csv", summaries)
    (output_dir / "summary.json").write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")
    write_latex_table(output_dir / "main_results_table.tex", summaries)
    metadata = {
        "dataset": str(Path(args.dataset).resolve()),
        "limit": args.limit,
        "build_ratio": args.build_ratio,
        "build_source_limit": args.build_source_limit,
        "preserve_eval_step_results": args.preserve_eval_step_results,
        "seed": args.seed,
        "build_tasks": len(build_rows),
        "eval_tasks": len(eval_rows),
        "eval_filter": args.eval_filter,
        "experience_posts": len(memories),
        "methods": selected_methods,
        "agent_mode": agent_mode,
        "llm_profile": normalize_profile_key(llm_profile) if llm_profile else "",
        "llm_model": llm_client.model if llm_client else "",
        "llm_max_eval_tasks": args.llm_max_eval_tasks,
    }
    (output_dir / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    print("Wrote results to %s" % output_dir)
    for row in summaries:
        print(
            "%s: step=%.2f workflow=%.2f repair=%.2f repeated=%.2f cost=%.2f"
            % (
                row["method"],
                row["step_success_rate"],
                row["workflow_success_rate"],
                row["repair_success_rate"],
                row["repeated_failure_rate"],
                row["avg_cost_proxy"],
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
