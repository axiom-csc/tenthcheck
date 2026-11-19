"""Per-session efficiency metrics from a Claude Code stream-json transcript (PLAN_EFFICIENCY.md).

    python efficiency_metrics.py <session dir> [<session dir> ...]     # prints one JSON line per session

- tokens: from the final `result` event's usage: input, cache_read, cache_creation, output, and `processed`
  (their sum: everything the model read or wrote across all turns);
- tool calls by tool name, `search_calls` (Grep + Glob + Read), `tenthcheck_calls`, `edit_calls` (Edit + Write);
- `tool_result_chars`: characters returned by tools into the context, by tool (what reading/grepping costs in context);
- `model_s` (Claude Code's duration_ms / 1000) and, when results.json is next to the session dir, `wall_s`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _text(content) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(c.get("text", "")) if isinstance(c, dict) else len(str(c)) for c in content)
    return len(json.dumps(content)) if content is not None else 0


def metrics(sess: Path) -> dict:
    calls, names, chars = {}, {}, {}
    out = {"session": sess.name}
    for line in (sess / "stream.jsonl").read_text().splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = ev.get("message") or {}
        content = msg.get("content") if isinstance(msg.get("content"), list) else []
        for b in content:
            if b.get("type") == "tool_use":
                calls[b["name"]] = calls.get(b["name"], 0) + 1
                names[b["id"]] = b["name"]
            elif b.get("type") == "tool_result":
                n = names.get(b.get("tool_use_id"), "?")
                chars[n] = chars.get(n, 0) + _text(b.get("content"))
        if ev.get("type") == "result":
            u = ev.get("usage") or {}
            tok = {"input": u.get("input_tokens", 0), "cache_read": u.get("cache_read_input_tokens", 0),
                   "cache_creation": u.get("cache_creation_input_tokens", 0), "output": u.get("output_tokens", 0)}
            tok["processed"] = sum(tok.values())
            out.update(tokens=tok, num_turns=ev.get("num_turns"), model_s=round((ev.get("duration_ms") or 0) / 1000, 1),
                       reported_cost_usd=ev.get("total_cost_usd"))
    out.update(tool_calls=calls,
               search_calls=sum(calls.get(k, 0) for k in ("Grep", "Glob", "Read")),
               edit_calls=sum(calls.get(k, 0) for k in ("Edit", "Write", "MultiEdit")),
               tenthcheck_calls=sum(v for k, v in calls.items() if k.startswith("mcp__tenthcheck__")),
               tool_result_chars=chars, tool_result_chars_total=sum(chars.values()))
    res = sess.parent / "results.json"
    if res.exists():
        task, arm = sess.name.split("__")
        for r in json.loads(res.read_text())["runs"]:
            if r["task_id"] == task and r["arm"] == arm:
                out["wall_s"] = r["wall_s"]
    return out


if __name__ == "__main__":
    for d in sys.argv[1:]:
        print(json.dumps(metrics(Path(d))))
