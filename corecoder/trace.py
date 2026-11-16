"""Small, content-free task traces for diagnosing agent runs.

Only the fields listed here may reach disk. Prompts, tool arguments and
outputs, shell commands, verification logs, and model credentials are never
accepted by the recorder.
"""

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

_TASK_ID = re.compile(r"[0-9a-f]{12}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
_TOOLS = frozenset({"read_file", "write_file", "edit_file", "glob", "grep", "bash", "agent", "todo_write"})
_EVENTS = frozenset({
    "task_started", "task_resumed", "task_left", "task_applied", "task_discarded",
    "turn_started", "turn_finished", "model_started", "model_finished",
    "tool_started", "tool_decision", "tool_finished", "worktree_changed", "verify_started", "verify_approval",
    "verify_command", "verify_finished", "repair_started", "repair_stopped",
})
_COUNTS = frozenset({"round", "tool_count", "prompt_tokens", "completion_tokens", "attempt", "exit_code"})
_TEXT = frozenset({"turn_id", "role", "tool", "outcome", "error_type", "reason"})
_REASONS = frozenset({
    "read_only", "preapproved", "approved_once", "approved_always",
    "approval_unavailable", "approval_denied", "persistent_not_allowed",
    "hook_blocked", "plan_mode",
})


def tool_label(name: str) -> str:
    """Avoid persisting untrusted MCP/custom tool names."""
    return name if name in _TOOLS else "external"


class TaskTrace:
    def __init__(self, directory: Path, task_id: str):
        if not _TASK_ID.fullmatch(task_id):
            raise ValueError("Invalid task ID for trace.")
        self.path = directory / f"{task_id}.trace.jsonl"
        self.task_id = task_id
        self._lock = threading.Lock()
        self.last_error: str | None = None

    def record(self, event: str, **fields) -> None:
        """Append a safe event; tracing errors must not interrupt coding."""
        if event not in _EVENTS:
            raise ValueError(f"Unknown trace event: {event}")
        row = {
            "v": 1, "task_id": self.task_id,
            "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": event,
        }
        for key, value in fields.items():
            if value is None:
                continue
            if key in _COUNTS and type(value) is int:
                row[key] = value
            elif key == "seconds" and type(value) in (int, float):
                row[key] = round(float(value), 3)
            elif key in _TEXT and isinstance(value, str):
                if key == "tool":
                    row[key] = tool_label(value)
                elif key == "turn_id" and re.fullmatch(r"[0-9a-f]{12}", value) or key == "role" and value in ("main", "subagent") or key == "outcome" and value in (
                    "completed", "round_limit", "interrupted", "error", "passed",
                    "failed", "environment", "timeout", "missing", "skipped", "stale",
                    "allowed", "denied", "blocked_hook", "blocked_plan", "no_change",
                ) or key == "error_type" and _IDENTIFIER.fullmatch(value) or key == "reason" and value in _REASONS:
                    row[key] = value
            elif key == "diff_digest" and isinstance(value, str) and _SHA256.fullmatch(value):
                row[key] = value
        line = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("ab+") as stream:
                    stream.seek(0, 2)
                    if stream.tell():
                        stream.seek(-1, 2)
                        if stream.read(1) != b"\n":
                            stream.write(b"\n")  # separate a partial event left by a crash
                    stream.write(line.encode("utf-8"))
        except OSError as exc:
            self.last_error = type(exc).__name__

    def read(self) -> list[dict]:
        try:
            with self.path.open(encoding="utf-8") as stream:
                rows = []
                for line in stream:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # an interrupted append may leave a partial final line
                    if (isinstance(row, dict) and row.get("v") == 1 and row.get("task_id") == self.task_id
                            and isinstance(row.get("event"), str) and row["event"] in _EVENTS
                            and isinstance(row.get("at"), str)):
                        rows.append(row)
                return rows
        except FileNotFoundError:
            return []


def describe(row: dict) -> str:
    """Render only recorder-approved metadata, never arbitrary JSON content."""
    event = row["event"]
    bits = [event]
    for key in ("role", "round", "tool", "outcome", "reason", "attempt", "error_type",
                "exit_code", "tool_count", "prompt_tokens", "completion_tokens", "seconds", "diff_digest"):
        if key in row:
            bits.append(f"{key}={row[key]}")
    return f"{row['at']}  " + " ".join(bits)


def latest_issue(rows: list[dict]) -> str | None:
    """Report the last observed problem, not an inferred root cause."""
    for row in reversed(rows):
        event, outcome = row.get("event"), row.get("outcome")
        if event == "task_applied" or (event == "verify_finished" and outcome == "passed"):
            return None
        if event == "verify_finished" and outcome not in ("passed", None):
            return f"verification {outcome}"
        if event == "turn_finished" and outcome in ("round_limit", "interrupted", "error"):
            return f"agent turn {outcome}" + (f" ({row['error_type']})" if "error_type" in row else "")
        if event == "model_finished" and outcome == "error":
            return f"model call failed ({row.get('error_type', 'unknown')})"
        if event == "tool_finished" and outcome in ("denied", "blocked_hook", "blocked_plan", "failed"):
            return f"tool {row.get('tool', 'external')} {outcome}"
    return None
