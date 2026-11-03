"""Run CoreCoder's local coding-task benchmark without touching the user's projects.

Mock mode validates the harness; live mode spends API tokens and executes
model-written Python during verification. Use live mode only in a disposable VM.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from corecoder.llm import LLM, LLMResponse, LiteLLM, ToolCall
from corecoder.memory import MemoryStore
from corecoder.rules import RuleLoader, project_id
from corecoder.task_cli import TaskController
from corecoder.tasks import TaskManager
from corecoder.tools import workspace_tools
from corecoder.workspace import child_environment


ROOT = Path(__file__).resolve().parent
MODULES = {"numbers": "number_ops.py", "strings": "strings.py", "records": "records.py"}
ALLOWED_TOOLS = {"read_file", "write_file", "edit_file", "glob", "grep", "todo_write"}
DEFAULT_AGENT_ROUNDS = 20
PERSONAL_RULE = (
    "Project coding conventions: keep existing public function signatures; "
    "edit implementation files only; preserve input order and do not mutate "
    "caller-owned collections unless explicitly requested.\n"
)


def load_cases() -> list[dict]:
    cases = json.loads((ROOT / "cases.json").read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not cases:
        raise ValueError("cases.json must be a nonempty list")
    seen = set()
    for case in cases:
        if not isinstance(case, dict) or case.get("fixture") not in MODULES:
            raise ValueError("unknown fixture")
        if not re.fullmatch(r"[a-z]+-[a-z]+", case.get("id", "")) or case["id"] in seen:
            raise ValueError("invalid or duplicate case id")
        if not re.fullmatch(r"test_[a-z]+", case.get("test", "")):
            raise ValueError("invalid public test name")
        if not isinstance(case.get("prompt"), str) or not isinstance(case.get("memory"), str):
            raise ValueError("case needs prompt and memory")
        seen.add(case["id"])
    return cases


def _git(repo: Path, *args: str) -> str:
    environment = child_environment()
    environment["GIT_AUTHOR_DATE"] = "2000-01-01T00:00:00+00:00"
    environment["GIT_COMMITTER_DATE"] = "2000-01-01T00:00:00+00:00"
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=environment, check=False,
    )
    if proc.returncode:
        raise RuntimeError(f"git {args[0]} failed: {proc.stderr[-500:]}")
    return proc.stdout.strip()


def make_repo(case: dict, target: Path) -> str:
    shutil.copytree(ROOT / "fixtures" / case["fixture"], target,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"))
    config = target / ".corecoder" / "verify.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"commands": [{
        "name": "public test",
        "argv": [sys.executable, "-m", "unittest", f"tests.public_checks.PublicTests.{case['test']}"],
        "timeout": 30,
    }]}, indent=2), encoding="utf-8")
    _git(target, "init")
    _git(target, "config", "user.name", "CoreCoder Eval")
    _git(target, "config", "user.email", "eval@example.invalid")
    _git(target, "config", "core.autocrlf", "false")
    _git(target, "config", "commit.gpgsign", "false")
    _git(target, "add", "-A")
    _git(target, "commit", "-m", "fixture base")
    return _git(target, "rev-parse", "HEAD")


class MockLLM:
    """Reference writer for harness self-tests, never an agent-quality result."""

    model = "mock-reference"

    def __init__(self, case: dict):
        self.case = case
        self.called = False

    def chat(self, **_kwargs):
        if not self.called:
            self.called = True
            filename = MODULES[self.case["fixture"]]
            content = (ROOT / "golden" / self.case["fixture"] / filename).read_text(encoding="utf-8")
            return LLMResponse(tool_calls=[ToolCall("eval-write", "write_file", {"file_path": filename, "content": content})])
        return LLMResponse(content="Reference implementation written.")


class EvalBudgetExceeded(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"evaluation {reason} limit reached")


class AgentRoundLimitExceeded(RuntimeError):
    """The model kept requesting tools until this chat's round cap."""


class RestrictedEvalTool:
    """Enforce the stated edit scope before a mutating tool reaches the disk."""

    def __init__(self, inner, workspace, expected: str, events: list[dict]):
        self.inner = inner
        self.workspace = workspace
        self.expected = workspace.resolve(expected)
        self.events = events
        self.name = inner.name
        self.description = inner.description

    def schema(self):
        return self.inner.schema()

    def execute(self, **kwargs):
        if self.name in {"write_file", "edit_file"}:
            try:
                allowed = self.workspace.resolve(kwargs["file_path"]) == self.expected
            except (KeyError, OSError, TypeError, ValueError):
                allowed = False
            if not allowed:
                self.events.append({"tool": self.name, "outcome": "blocked_scope"})
                return (
                    "Error: evaluation policy allows edits to "
                    f"{self.expected.name} only. Do not create scratch test scripts; "
                    "finish your response for automatic verification."
                )
        result = self.inner.execute(**kwargs)
        self.events.append({"tool": self.name, "outcome": "error" if result.startswith("Error") else "ok"})
        return result


class CountedLLM:
    def __init__(self, inner, max_calls: int, max_tokens: int, deadline: float):
        self.inner = inner
        self.model = inner.model
        self.max_calls = max_calls
        self.max_tokens = max_tokens
        self.deadline = deadline
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def chat(self, **kwargs):
        if self.calls >= self.max_calls:
            raise EvalBudgetExceeded("calls")
        if time.monotonic() >= self.deadline:
            raise EvalBudgetExceeded("time")
        if self.prompt_tokens + self.completion_tokens >= self.max_tokens:
            raise EvalBudgetExceeded("tokens")
        self.calls += 1
        response = self.inner.chat(**kwargs)
        self.prompt_tokens += response.prompt_tokens
        self.completion_tokens += response.completion_tokens
        return response


def _safe_tools(workspace, expected: str, events: list[dict]):
    return [RestrictedEvalTool(tool, workspace, expected, events)
            if tool.name in {"write_file", "edit_file"} else tool
            for tool in workspace_tools(workspace) if tool.name in ALLOWED_TOOLS]


def _guard(expected: str):
    def check(task, manager):
        manager.patch(task)
        changed = _git(Path(task.worktree), "diff", "--cached", "--name-only", task.base_sha).splitlines()
        if any(path != expected for path in changed):
            raise RuntimeError("evaluation policy: change outside the allowed implementation file")
    return check


def _hidden(case: dict, repo: Path, suite: str = "all") -> bool:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "hidden_tests" / "check.py"), case["id"], str(repo), suite],
        cwd=repo, env=child_environment(), capture_output=True, timeout=30, check=False,
    )
    return proc.returncode == 0


def _core_digest() -> str:
    digest = hashlib.sha256()
    root = ROOT.parent / "corecoder"
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _benchmark_digest() -> str:
    """Version both case prompts and executable checks, not just cases.json."""
    digest = hashlib.sha256()
    paths = [ROOT / "cases.json"]
    for folder in ("fixtures", "golden", "hidden_tests"):
        paths.extend((ROOT / folder).rglob("*.py"))
    for path in sorted(paths):
        digest.update(path.relative_to(ROOT).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def run_one(case: dict, mode: str, repeat: int, *, mock: bool, config=None,
            max_calls: int = 20, max_tokens: int = 60000, max_seconds: int = 600,
            agent_rounds: int = DEFAULT_AGENT_ROUNDS) -> dict:
    started = time.monotonic()
    record = {"case": case["id"], "fixture": case["fixture"], "category": case["category"],
              "mode": mode, "repeat": repeat, "mock": mock, "verify": "not_run",
              "applied": False, "hidden_pass": False, "hidden_common_pass": False,
              "hidden_memory_pass": False, "success": False, "repair_rounds": 0,
              "model_calls": 0, "prompt_tokens": None, "completion_tokens": None,
              "memory_hit_count": 0, "rule_source_count": 0,
              "tool_names": [], "tool_events": [], "policy_attempts": 0,
              "changed_paths": [], "error": None, "stop_reason": None,
              "error_reason": None, "limit_reason": None}
    with tempfile.TemporaryDirectory(prefix="corecoder-eval-") as temporary:
        temp = Path(temporary)
        repo = temp / "repo"
        counted = None
        try:
            record["base_sha"] = make_repo(case, repo)
            record["baseline_hidden_pass"] = _hidden(case, repo)
            if record["baseline_hidden_pass"]:
                raise RuntimeError("case calibration failed: baseline passes hidden checks")
            home = temp / "home"
            rules = RuleLoader(repo, home=home)
            store = MemoryStore(temp / "memory.sqlite3")
            if mode == "personalized":
                path = rules.paths[1][1]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(PERSONAL_RULE, encoding="utf-8")
                store.add(case["memory"], "project", project_id(repo))
            provider_class = LiteLLM if config is not None and config.provider == "litellm" else LLM
            inner = MockLLM(case) if mock else provider_class(
                config.model, config.api_key, config.base_url,
                temperature=config.temperature, max_tokens=config.max_tokens,
            )
            counted = CountedLLM(inner, max_calls, max_tokens, started + max_seconds)
            ordinary = SimpleNamespace(
                llm=counted, tools=[], context=SimpleNamespace(
                    max_tokens=128000 if config is None else config.max_context_tokens),
                hooks=None, memory_store=store, rules=rules, plan_mode=False,
            )
            messages = []
            class Console:
                def print(self, value, **_kwargs):
                    if isinstance(value, str) and "automatic repair" in value:
                        record["repair_rounds"] += 1
                    messages.append(str(value)[:80])

            def confirm(question):
                return "n" if "memory candidates" in question else "y"

            manager = TaskManager(temp / "tasks", temp / "worktrees")
            controller = TaskController(
                ordinary, Console(), lambda *_args: "once", confirm,
                manager=manager,
                tool_factory=lambda workspace: _safe_tools(
                    workspace, MODULES[case["fixture"]], record["tool_events"]),
                max_rounds=agent_rounds,
                verification_guard=_guard(MODULES[case["fixture"]]),
            )
            task, _warnings = manager.start(repo, case["prompt"], counted.model)
            controller._activate(task)
            record["rule_source_count"] = sum(
                bool(source.content) for source in controller.agent.rules.load().sources
            )
            def chat(agent, user_input):
                response = agent.chat(
                    user_input, on_tool=lambda name, _args: record["tool_names"].append(name),
                )
                if agent.last_stop_reason == "round_limit":
                    raise AgentRoundLimitExceeded("agent tool-call round limit reached")
                return response
            before = manager.digest(task)
            chat(controller.agent, case["prompt"])
            record["memory_hit_count"] = len(controller.agent.last_memories.hits)
            controller.after_chat(before, chat)
            record["verify"] = task.verify_status
            manager.patch(task)
            record["changed_paths"] = _git(Path(task.worktree), "diff", "--cached", "--name-only", task.base_sha).splitlines()
            if manager.verification_current(task):
                controller.command("/task apply")
                record["applied"] = task.status == "applied"
            if record["applied"]:
                record["hidden_common_pass"] = _hidden(case, repo, "common")
                record["hidden_memory_pass"] = _hidden(case, repo, "memory")
                record["hidden_pass"] = record["hidden_common_pass"] and record["hidden_memory_pass"]
            record["success"] = bool(record["applied"] and record["hidden_pass"])
            if record["success"]:
                record["stop_reason"] = "completed"
            elif not record["applied"]:
                record["stop_reason"] = "verification_" + record["verify"]
            elif not record["hidden_common_pass"]:
                record["stop_reason"] = "hidden_common_failed"
            else:
                record["stop_reason"] = "hidden_memory_failed"
        except Exception as exc:  # noqa: BLE001 - failed cases belong in the report
            record["error"] = type(exc).__name__
            if "task" in locals():
                record["verify"] = task.verify_status
            if "controller" in locals() and controller.agent is not None:
                record["memory_hit_count"] = len(controller.agent.last_memories.hits)
            if isinstance(exc, EvalBudgetExceeded):
                record["limit_reason"] = exc.reason
                record["stop_reason"] = "budget_exhausted"
            elif isinstance(exc, AgentRoundLimitExceeded):
                record["limit_reason"] = "agent_rounds"
                record["stop_reason"] = "round_limit"
            elif isinstance(exc, RuntimeError) and str(exc).startswith("evaluation policy:"):
                record["error_reason"] = "out_of_scope_change"
                record["stop_reason"] = "policy_violation"
            else:
                record["stop_reason"] = "error"
            if "task" in locals() and "manager" in locals() and Path(task.worktree).is_dir():
                try:
                    manager.patch(task)
                    record["changed_paths"] = _git(
                        Path(task.worktree), "diff", "--cached", "--name-only", task.base_sha,
                    ).splitlines()
                except (OSError, RuntimeError):
                    pass
        finally:
            record["policy_attempts"] = sum(
                event["outcome"] == "blocked_scope" for event in record["tool_events"]
            )
            if counted is not None:
                record["model_calls"] = counted.calls
                if counted.prompt_tokens or counted.completion_tokens:
                    record["prompt_tokens"] = counted.prompt_tokens
                    record["completion_tokens"] = counted.completion_tokens
            record["seconds"] = round(time.monotonic() - started, 2)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    kind = parser.add_mutually_exclusive_group(required=True)
    kind.add_argument("--mock", action="store_true", help="self-test harness with reference writer")
    kind.add_argument("--live", action="store_true", help="use configured model; executes model-written code")
    parser.add_argument("--allow-code-execution", action="store_true", help="required acknowledgement for --live")
    parser.add_argument("--pilot", action="store_true", help="one case per fixture")
    parser.add_argument("--case", action="append", help="case id; may be repeated")
    parser.add_argument("--mode", choices=("both", "baseline", "personalized"), default="both")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--seed", type=int, default=1102)
    parser.add_argument("--max-calls", type=int, default=20)
    parser.add_argument("--agent-rounds", type=int, default=DEFAULT_AGENT_ROUNDS)
    parser.add_argument("--max-tokens", type=int, default=60000)
    parser.add_argument("--max-seconds", type=int, default=600)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.live and not args.allow_code_execution:
        parser.error("--live requires --allow-code-execution; run only in a disposable VM/container")
    if min(args.repeats, args.max_calls, args.max_tokens, args.max_seconds, args.agent_rounds) < 1:
        parser.error("repeats and limits must be positive")
    cases = load_cases()
    if args.case:
        unknown = set(args.case) - {case["id"] for case in cases}
        if unknown:
            parser.error(f"unknown case(s): {sorted(unknown)}")
        cases = [case for case in cases if case["id"] in args.case]
    elif args.pilot:
        cases = [next(case for case in cases if case["fixture"] == fixture) for fixture in MODULES]
    modes = ["baseline", "personalized"] if args.mode == "both" else [args.mode]
    jobs = [(case, mode, repeat) for case in cases for repeat in range(1, args.repeats + 1) for mode in modes]
    random.Random(args.seed).shuffle(jobs)
    config = None
    if args.live:
        from corecoder.config import Config
        config = Config.from_env()
        if not config.api_key:
            parser.error("model API key is missing")
    output = args.output or ROOT / "results" / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ("-mock" if args.mock else "-live") + ".jsonl"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    header = {"type": "run", "mock": args.mock, "model": "mock-reference" if args.mock else config.model,
              "seed": args.seed, "repeats": args.repeats, "case_count": len(cases),
              "started_utc": datetime.now(timezone.utc).isoformat(),
              "cases_sha256": hashlib.sha256((ROOT / "cases.json").read_bytes()).hexdigest(),
              "benchmark_sha256": _benchmark_digest(),
              "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "corecoder_sha256": _core_digest(),
              "provider": "mock" if args.mock else config.provider,
              "temperature": None if args.mock else config.temperature,
              "max_output_tokens": None if args.mock else config.max_tokens,
              "max_calls_per_case": args.max_calls,
              "max_total_tokens_per_case": args.max_tokens,
              "max_seconds_per_case": args.max_seconds,
              "agent_max_rounds": args.agent_rounds,
              "modes": modes, "pilot": args.pilot}
    with output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(header, ensure_ascii=False) + "\n")
        for index, (case, mode, repeat) in enumerate(jobs, 1):
            record = run_one(case, mode, repeat, mock=args.mock, config=config,
                             max_calls=args.max_calls, max_tokens=args.max_tokens,
                             max_seconds=args.max_seconds, agent_rounds=args.agent_rounds)
            stream.write(json.dumps({"type": "case", **record}, ensure_ascii=False) + "\n")
            stream.flush()
            print(f"[{index}/{len(jobs)}] {case['id']} {mode} #{repeat}: "
                  f"{'PASS' if record['success'] else 'FAIL'} ({record['verify']})")
    print(f"Results: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
