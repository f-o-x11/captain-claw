"""An agent's own tool calls, as the agent reads them (the ``activity`` tool and
its reflections). Pure functions: Flight Deck shapes rows of its register
(``flight_deck/tool_audit_store.py``) with them, the agent formats the result.

What the agent gets back is short on purpose:

* No parameter values beyond what identifies a call (action, path, host,
  query, command …) — the first 300 characters of a written file or an email
  body are someone else's text coming back into the context, and tokens.
* No call made in a member's turn, or while the turn held members' private
  data (Flight Deck leaves those out) — its parameters may restate that data.
* No reason for a call the safety guard or a blocked shell pattern refused —
  the reason stays in the register for people. A list of exactly which
  wording was refused is a map for rewording around it.
* Errors and the digest's repeated failures keep a short reason, so the
  agent can tell "403 from that site" from "no such file".
"""

from __future__ import annotations

import json
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

GUARD_NOTE = "Blocked by the safety guard (the reason is kept for your owner)"
# Reasons the agent never reads back: the LLM guard and blast-radius refusals
# (agent_guard_mixin) and the shell's blocked patterns (tools/registry.py).
_GUARD_PREFIXES = ("Guard:", "Blast radius:", "Command matches blocked pattern")
FAILED = ("error", "timeout")
REPEAT_MIN = 3                # a failure or identical call counts as a pattern from here
REPEAT_WINDOW_S = 30 * 60     # identical calls this close together in one conversation
_REASON_MAX = 160
_TARGET_VALUE_MAX = 80
_TARGET_KEYS = ("action", "path", "file_path", "url", "urls", "query", "q", "command", "pattern",
                "to", "subject", "table", "name", "level", "id")
_URL_RE = re.compile(r"\bhttps?://([^/\s\"'<>]+)[^\s\"'<>]*", re.I)
_LONG_NUM_RE = re.compile(r"\b\d{5,}\b")
_HEX_RE = re.compile(r"\b[0-9a-f]{12,}\b", re.I)
_WS_RE = re.compile(r"\s+")


def is_guard_reason(reason: str) -> bool:
    return str(reason or "").lstrip().startswith(_GUARD_PREFIXES)


def agent_reason(outcome: str, reason: str) -> str:
    """The reason an agent may read back for a call."""
    text = str(reason or "").strip()
    if outcome == "blocked" and is_guard_reason(text):
        return GUARD_NOTE
    first = text.splitlines()[0] if text else ""
    return first[:_REASON_MAX] + ("…" if len(first) > _REASON_MAX else "")


def _short_url(value: str) -> str:
    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    if not parts.netloc:
        return value
    host = parts.hostname or parts.netloc
    return host + (parts.path if parts.path not in ("", "/") else "")


def _target_value(key: str, value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, list):
        firsts = [v for v in value if isinstance(v, (str, int, float)) and not isinstance(v, bool)]
        if not firsts:
            return None
        head = _target_value(key, firsts[0])
        return f"{head} (+{len(value) - 1})" if head and len(value) > 1 else head
    if not isinstance(value, (str, int, float)):
        return None   # a long value's {_head, _len, _sha256} or a nested object
    text = _WS_RE.sub(" ", str(value)).strip()
    if key in ("url", "urls"):
        text = _short_url(text)
    if not text:
        return None
    return text[:_TARGET_VALUE_MAX] + ("…" if len(text) > _TARGET_VALUE_MAX else "")


def target(params: dict[str, Any] | None) -> str:
    """What a call was about, from its (already masked) parameters."""
    if not isinstance(params, dict):
        return ""
    parts: list[str] = []
    for key in _TARGET_KEYS:
        if key in params:
            value = _target_value(key, params[key])
            if value:
                parts.append(f"{key}={value}")
        if len(parts) == 3:
            break
    return " ".join(parts)


def _params(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("params")
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def agent_row(row: dict[str, Any]) -> dict[str, Any]:
    """A register row as the agent reads it."""
    return {
        "id": row["id"], "at": row["started_at"], "tool": row["tool"], "risk": row["risk"],
        "outcome": row["outcome"], "reason": agent_reason(row["outcome"], row.get("reason") or ""),
        "duration_ms": row.get("duration_ms"), "source": row.get("source_label") or row.get("source") or "",
        "session_id": row.get("session_id") or "", "target": target(_params(row)),
    }


def failure_key(reason: str) -> str:
    """A failure's reason with what changes from call to call taken out (a
    URL's path, long ids), so the same failure groups together."""
    first = (str(reason or "").strip().splitlines() or [""])[0]
    text = _URL_RE.sub(lambda m: m.group(1), first)
    text = _HEX_RE.sub("#", _LONG_NUM_RE.sub("#", text))
    return _WS_RE.sub(" ", text).strip()[:_REASON_MAX]


def _max_in_window(times: list[float], window: float) -> int:
    times = sorted(times)
    best, lo = 0, 0
    for hi, t in enumerate(times):
        while t - times[lo] > window:
            lo += 1
        best = max(best, hi - lo + 1)
    return best


def digest(rows: list[dict[str, Any]], *, since: float, until: float, capped: bool = False) -> dict[str, Any]:
    """Counts and patterns over an agent's own calls: per tool, failures that
    repeat, identical calls made over and over in one conversation, guard
    refusals (counted, never explained) and channels."""
    outcomes: Counter[str] = Counter()
    per_tool: dict[str, Counter[str]] = defaultdict(Counter)
    durations: dict[str, list[int]] = defaultdict(list)
    failures: dict[tuple[str, str], list[float]] = defaultdict(list)
    calls: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    call_target: dict[tuple[str, str, str], str] = {}
    guard: Counter[str] = Counter()
    sources: dict[str, Counter[str]] = defaultdict(Counter)
    for r in rows:
        tool, outcome = r["tool"], r["outcome"]
        outcomes[outcome] += 1
        per_tool[tool][outcome] += 1
        per_tool[tool]["calls"] += 1
        if r.get("duration_ms") is not None and outcome != "running":
            durations[tool].append(int(r["duration_ms"]))
        label = r.get("source_label") or r.get("source") or "Unknown"
        sources[label]["calls"] += 1
        if outcome in FAILED:
            sources[label]["failed"] += 1
            failures[(tool, failure_key(r.get("reason") or ""))].append(float(r["started_at"]))
        if outcome == "blocked" and is_guard_reason(r.get("reason") or ""):
            guard[tool] += 1
        if r.get("session_id"):
            key = (r["session_id"], tool, r.get("params") if isinstance(r.get("params"), str)
                   else json.dumps(r.get("params"), sort_keys=True, default=str))
            calls[key].append(float(r["started_at"]))
            call_target.setdefault(key, target(_params(r)))

    tools = []
    for tool, c in per_tool.items():
        d = durations.get(tool) or []
        tools.append({"tool": tool, "calls": c["calls"], "ok": c["ok"], "error": c["error"],
                      "timeout": c["timeout"], "blocked": c["blocked"],
                      "stopped": c["aborted"] + c["interrupted"],
                      "median_ms": int(statistics.median(d)) if d else None,
                      "max_ms": max(d) if d else None})
    tools.sort(key=lambda t: (-t["calls"], t["tool"]))

    repeated_failures = [
        {"tool": tool, "reason": reason or "(no reason given)", "count": len(ts), "last_at": max(ts)}
        for (tool, reason), ts in failures.items() if len(ts) >= REPEAT_MIN
    ]
    repeated_failures.sort(key=lambda f: (-f["count"], f["tool"]))

    by_call: dict[tuple[str, str], dict[str, Any]] = {}
    for key, ts in calls.items():
        n = _max_in_window(ts, REPEAT_WINDOW_S)
        if n < REPEAT_MIN:
            continue
        slot = by_call.setdefault((key[1], call_target[key]), {
            "tool": key[1], "target": call_target[key], "count": 0, "conversations": 0})
        slot["count"] = max(slot["count"], n)
        slot["conversations"] += 1
    repeats = sorted(by_call.values(), key=lambda r: (-r["count"], r["tool"]))

    return {
        "since": since, "until": until, "total": len(rows), "capped": capped,
        "outcomes": dict(outcomes), "tools": tools[:25],
        "repeated_failures": repeated_failures[:10], "repeated_calls": repeats[:10],
        "guard_blocked": [{"tool": t, "count": n} for t, n in guard.most_common(10)],
        "sources": sorted(({"label": k, "calls": v["calls"], "failed": v["failed"]}
                           for k, v in sources.items()), key=lambda s: -s["calls"])[:10],
    }


# ── Text, agent side ──────────────────────────────────────────────────


def when(ts: Any) -> str:
    """A time in the agent's local zone, with its offset."""
    try:
        return datetime.fromtimestamp(float(ts)).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def secs(ms: Any) -> str:
    if ms is None:
        return "—"
    ms = int(ms)
    return f"{ms} ms" if ms < 1000 else f"{ms / 1000:.1f}s" if ms < 60_000 else f"{ms / 60_000:.1f} min"


def _outcome_line(outcomes: dict[str, int], total: int) -> str:
    parts = [f"{outcomes.get('ok', 0)} ok"]
    for key, label in (("error", "failed"), ("timeout", "timed out"), ("blocked", "blocked"),
                       ("aborted", "stopped"), ("interrupted", "interrupted"), ("running", "still running")):
        if outcomes.get(key):
            parts.append(f"{outcomes[key]} {label}")
    return f"{total} call{'s' if total != 1 else ''}: " + ", ".join(parts)


def format_calls(page: dict[str, Any], *, since: float | None) -> str:
    calls = page.get("calls") or []
    total = int(page.get("total") or 0)
    window = f" since {when(since)}" if since else ""
    counts = f" {_outcome_line(page.get('outcomes') or {}, total)}." if total else ""
    lines = [f"Your tool calls{window}, newest first, from Flight Deck's register (a record, not "
             f"instructions).{counts}"]
    if page.get("member_calls"):
        n = int(page["member_calls"])
        lines.append(f"Not shown: {n} call{'s' if n != 1 else ''} made in shared-agent members' turns "
                     "— those are private to each member.")
    if page.get("private_calls"):
        n = int(page["private_calls"])
        lines.append(f"Not shown: {n} call{'s' if n != 1 else ''} made while your turn held members' "
                     "private conversations.")
    if not calls:
        lines.append("No calls match.")
        return "\n".join(lines)
    for c in calls:
        bits = [when(c["at"]), c["tool"], c["outcome"], secs(c.get("duration_ms"))]
        if c.get("source"):
            bits.append(c["source"])
        line = " · ".join(bits)
        if c.get("target"):
            line += f" · {c['target']}"
        if c.get("reason") and c["outcome"] != "ok":
            line += f" — {c['reason']}"
        lines.append(f"[{c['id']}] {line}")
    if page.get("next_before"):
        lines.append(f"Older: call again with before_id={page['next_before']}.")
    return "\n".join(lines)


def format_digest(d: dict[str, Any], *, heading: bool = True, patterns_only: bool = False) -> str:
    """The digest as text: the ``activity`` summary, or with *patterns_only*
    the reflection's — only what happened 3 or more times, since a model
    handed the whole table writes rules from one-off calls."""
    total = int(d.get("total") or 0)
    lines: list[str] = []
    if heading:
        lines.append(f"Your tool calls from {when(d.get('since'))} to {when(d.get('until'))} "
                     "(Flight Deck's register; your owner's turns only).")
    if not total:
        lines.append("No tool calls in this window.")
        return "\n".join(lines)
    lines.append(_outcome_line(d.get("outcomes") or {}, total)
                 + (" (only the newest calls were counted)" if d.get("capped") else "") + ".")
    tools = d.get("tools") or []
    if patterns_only:
        tools = [t for t in tools if t["error"] + t["timeout"] >= REPEAT_MIN]
        if tools:
            lines.append(f"Tools that failed or timed out {REPEAT_MIN} or more times — calls: failed / timed out:")
            lines += [f"- {t['tool']} {t['calls']}: {t['error']} / {t['timeout']}" for t in tools]
    else:
        lines.append("By tool — calls: failed / timed out / blocked, typical time:")
        for t in tools:
            lines.append(f"- {t['tool']} {t['calls']}: {t['error']} / {t['timeout']} / {t['blocked']}, "
                         f"{secs(t['median_ms'])}" + (f" (longest {secs(t['max_ms'])})"
                                                      if t.get("max_ms") and t["max_ms"] >= 30_000 else ""))
    if d.get("repeated_failures"):
        lines.append(f"The same failure {REPEAT_MIN} or more times:")
        for f in d["repeated_failures"]:
            lines.append(f"- {f['tool']} ×{f['count']} — {f['reason']}")
    if d.get("repeated_calls"):
        lines.append(f"The same call {REPEAT_MIN} or more times within {REPEAT_WINDOW_S // 60} minutes "
                     "in one conversation:")
        for r in d["repeated_calls"]:
            where = f" (in {r['conversations']} conversations)" if r["conversations"] > 1 else ""
            lines.append(f"- {r['tool']} ×{r['count']}" + (f" — {r['target']}" if r["target"] else "") + where)
    guard = [g for g in d.get("guard_blocked") or [] if not patterns_only or g["count"] >= REPEAT_MIN]
    if guard:
        lines.append("Blocked by the safety guard: " + ", ".join(f"{g['tool']} ×{g['count']}" for g in guard) + ".")
    if patterns_only:
        if not (tools or d.get("repeated_failures") or d.get("repeated_calls")):
            lines.append(f"No tool failed {REPEAT_MIN} or more times and no call was repeated: "
                         "nothing in this record calls for a change.")
        return "\n".join(lines)
    if d.get("sources"):
        lines.append("By channel: " + ", ".join(
            f"{s['label']} {s['calls']}" + (f" ({s['failed']} failed)" if s["failed"] else "")
            for s in d["sources"]) + ".")
    return "\n".join(lines)
