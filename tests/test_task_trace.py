"""Content-free task traces and their CLI lifecycle."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from corecoder.agent import Agent
from corecoder.llm import LLMResponse, ToolCall
from corecoder.memory import MemoryStore
from corecoder.permissions import Permission
from corecoder.rules import RuleLoader
from corecoder.task_cli import TaskController
from corecoder.tasks import TaskManager
from corecoder.tools.agent import AgentTool
from corecoder.tools.base import Tool
from corecoder.trace import TaskTrace, latest_issue


TASK_ID = "a1b2c3d4e5f6"
SECRET = "sk-private-test-value"


def test_recorder_allowlist_drops_prompts_commands_and_outputs(tmp_path):
    trace = TaskTrace(tmp_path, TASK_ID)
    trace.record(
        "tool_finished", tool="mcp__" + SECRET, outcome="failed",
        prompt=SECRET, command=SECRET, arguments={"token": SECRET},
        output=SECRET, error_type="RuntimeError", seconds=0.12,
    )
    text = trace.path.read_text(encoding="utf-8")
    assert SECRET not in text
    assert "arguments" not in text and "command" not in text and "output" not in text
    row = trace.read()[0]
    assert row["tool"] == "external"
    assert row["outcome"] == "failed"
    assert latest_issue([row]) == "tool external failed"
    trace.record("verify_finished", outcome="passed")
    assert latest_issue(trace.read()) is None


def test_trace_write_failure_does_not_interrupt_agent(tmp_path):
    trace = TaskTrace(tmp_path, TASK_ID)
    trace.path = tmp_path  # opening a directory as an append-only file fails
    agent = Agent(
        llm=ScriptedLLM([LLMResponse(content="done")]), tools=[],
        rules=RuleLoader(tmp_path, home=tmp_path / "home"),
        memory_store=MemoryStore(tmp_path / "memory.sqlite3"), trace=trace,
    )
    assert agent.chat("test") == "done"
    assert trace.last_error == "PermissionError" or trace.last_error == "IsADirectoryError"


def test_trace_recovers_after_partial_last_line(tmp_path):
    trace = TaskTrace(tmp_path, TASK_ID)
    trace.path.write_bytes(b'{"partial":')
    trace.record("task_resumed")
    assert [row["event"] for row in trace.read()] == ["task_resumed"]


class WriteTool(Tool):
    name = "write_file"
    description = "write"
    parameters = {"type": "object", "properties": {"content": {"type": "string"}}}

    def execute(self, content):
        return content


class ScriptedLLM:
    model = "mock"

    def __init__(self, responses):
        self.responses = iter(responses)

    def chat(self, **_kwargs):
        return next(self.responses)


def test_agent_trace_records_denial_without_tool_payload(tmp_path):
    trace = TaskTrace(tmp_path, TASK_ID)
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[ToolCall("call-1", "write_file", {"content": SECRET})], prompt_tokens=12),
        LLMResponse(content="done", completion_tokens=3),
    ])
    agent = Agent(
        llm=llm, tools=[WriteTool()], permission=Permission(ask=lambda *_: "deny"),
        rules=RuleLoader(tmp_path, home=tmp_path / "home"),
        memory_store=MemoryStore(tmp_path / "memory.sqlite3"), trace=trace,
    )
    assert agent.chat("do not log " + SECRET) == "done"
    rows = trace.read()
    assert [row["event"] for row in rows] == [
        "turn_started", "model_started", "model_finished", "tool_started",
        "tool_decision", "tool_finished", "model_started", "model_finished", "turn_finished",
    ]
    assert next(row for row in rows if row["event"] == "tool_decision")["outcome"] == "denied"
    assert next(row for row in rows if row["event"] == "tool_finished")["outcome"] == "denied"
    assert next(row for row in rows if row["event"] == "model_finished")["prompt_tokens"] == 12
    assert SECRET not in trace.path.read_text(encoding="utf-8")


def test_agent_trace_records_model_error(tmp_path):
    class FailingLLM:
        model = "mock"

        def chat(self, **_kwargs):
            raise TimeoutError(SECRET)

    trace = TaskTrace(tmp_path, TASK_ID)
    agent = Agent(
        llm=FailingLLM(), tools=[], rules=RuleLoader(tmp_path, home=tmp_path / "home"),
        memory_store=MemoryStore(tmp_path / "memory.sqlite3"), trace=trace,
    )
    try:
        agent.chat("test")
    except TimeoutError:
        pass
    else:
        raise AssertionError("model failure must propagate")
    rows = trace.read()
    assert rows[-2]["event"] == "model_finished" and rows[-2]["outcome"] == "error"
    assert rows[-1]["event"] == "turn_finished" and rows[-1]["error_type"] == "TimeoutError"
    assert SECRET not in trace.path.read_text(encoding="utf-8")


def test_subagent_uses_same_trace_with_separate_role(tmp_path):
    trace = TaskTrace(tmp_path, TASK_ID)
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[ToolCall("delegate", "agent", {"task": "inspect"})]),
        LLMResponse(content="subagent done"),
        LLMResponse(content="parent done"),
    ])
    agent = Agent(
        llm=llm, tools=[AgentTool()], permission=Permission(allow_all=True),
        rules=RuleLoader(tmp_path, home=tmp_path / "home"),
        memory_store=MemoryStore(tmp_path / "memory.sqlite3"), trace=trace,
    )
    assert agent.chat("delegate") == "parent done"
    rows = trace.read()
    assert sum(row["event"] == "turn_started" and row["role"] == "main" for row in rows) == 1
    assert sum(row["event"] == "turn_started" and row["role"] == "subagent" for row in rows) == 1
    assert all("inspect" not in json.dumps(row) for row in rows)


def _git(repo: Path, *args: str):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_trace_can_be_read_by_id_after_leave_and_restart(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "file.txt").write_text("base", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    manager = TaskManager(tmp_path / "tasks", tmp_path / "trees")
    task, _ = manager.start(repo, "inspect", "mock")
    output = []
    console = SimpleNamespace(print=lambda value, **_kwargs: output.append(str(value)))
    ordinary = SimpleNamespace(
        llm=ScriptedLLM([LLMResponse(content="done")]), tools=[],
        context=SimpleNamespace(max_tokens=1000), hooks=None,
        memory_store=MemoryStore(tmp_path / "memory.sqlite3"),
        rules=RuleLoader(repo, home=tmp_path / "home"),
    )
    controller = TaskController(ordinary, console, lambda *_: "once", lambda *_: "n", manager=manager)
    controller.command(f"/task trace {task.id}")  # old tasks have no trace file
    assert any("No trace recorded" in line for line in output)
    output.clear()
    controller._activate(task)
    assert controller.agent.chat("inspect") == "done"
    controller.command("/task leave")
    restarted = TaskController(ordinary, console, lambda *_: "once", lambda *_: "n", manager=manager)
    restarted.command(f"/task trace {task.id}")
    assert any("turn_finished" in line for line in output)
    assert any("task_left" in line for line in output)
    assert not any("inspect" in line for line in output)


def test_verify_trace_contains_outcome_not_output(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "file.txt").write_text("base", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    manager = TaskManager(tmp_path / "tasks", tmp_path / "trees")
    task, _ = manager.start(repo, "verify", "mock")
    config = Path(task.worktree) / ".corecoder" / "verify.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"commands": [{"argv": ["no-such-command-123", SECRET]}]}), encoding="utf-8")
    ordinary = SimpleNamespace(
        llm=SimpleNamespace(model="mock"), tools=[], context=SimpleNamespace(max_tokens=1000), hooks=None,
    )
    controller = TaskController(
        ordinary, SimpleNamespace(print=lambda *_, **__: None),
        lambda *_: "once", lambda *_: "y", manager=manager,
    )
    controller._activate(task)
    assert controller.verify() == "missing"
    rows = TaskTrace(manager.tasks_dir, task.id).read()
    assert [row["event"] for row in rows[-4:]] == [
        "verify_started", "verify_approval", "verify_command", "verify_finished",
    ]
    assert rows[-1]["outcome"] == "missing"
    assert SECRET not in TaskTrace(manager.tasks_dir, task.id).path.read_text(encoding="utf-8")
