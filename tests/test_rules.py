"""Layered rules: scope, prompt priority, turn snapshots, and task reuse."""

import subprocess
from pathlib import Path
from types import SimpleNamespace

from corecoder.agent import Agent
from corecoder.demo import ScriptedLLM
from corecoder.llm import LLMResponse, ToolCall
from corecoder.rules import RuleLoader, project_root
from corecoder.task_cli import TaskController
from corecoder.tasks import TaskManager
from corecoder.tools.agent import AgentTool
from tests.conftest import repl_with


def test_three_scopes_and_priority(tmp_path):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    nested = repo / "src"
    nested.mkdir()
    assert project_root(nested) == repo

    loader = RuleLoader(repo, home=home)
    global_path, private_path, shared_path = (path for _, path in loader.paths)
    for path, content in (
        (global_path, "Prefer concise answers."),
        (private_path, "Prefer pytest in this project."),
        (shared_path, "Follow the repository style."),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    snapshot = loader.load()
    assert [source.scope for source in snapshot.sources] == ["global", "personal project", "project shared"]
    message = snapshot.message()
    assert message["role"] == "user"
    text = message["content"]
    assert text.index("Prefer concise") < text.index("Prefer pytest") < text.index("repository style")
    assert "current explicit user request takes precedence" in text
    assert "not permission grants" in text

    agent = Agent(llm=ScriptedLLM([]), tools=[], rules=loader)
    agent.messages = [{"role": "user", "content": "Answer in detail this time."}]
    full = agent._full_messages()
    assert [item["role"] for item in full] == ["system", "user", "user"]
    assert full[-1]["content"] == "Answer in detail this time."


def test_rules_reload_next_turn_but_not_during_tool_rounds(tmp_path):
    home = tmp_path / "home"
    loader = RuleLoader(tmp_path, home=home)
    global_path = loader.paths[0][1]
    global_path.parent.mkdir(parents=True)
    global_path.write_text("Old rule", encoding="utf-8")

    class RecordingLLM:
        model = "mock"

        def __init__(self):
            self.calls = []

        def chat(self, messages, **_kwargs):
            self.calls.append([dict(item) for item in messages])
            if len(self.calls) == 1:
                global_path.write_text("New rule", encoding="utf-8")
                return LLMResponse(tool_calls=[ToolCall("one", "missing_tool", {})])
            return LLMResponse(content="done")

    llm = RecordingLLM()
    agent = Agent(llm=llm, tools=[], rules=loader)
    agent.chat("first")
    agent.chat("second")
    assert len(llm.calls) == 3
    assert all("Old rule" in llm.calls[index][1]["content"] for index in (0, 1))
    assert "New rule" in llm.calls[2][1]["content"]
    assert all("Old rule" not in str(item) for item in agent.messages)
    global_path.write_text("After reset", encoding="utf-8")
    agent.reset()
    assert "After reset" in agent._full_messages()[1]["content"]


def test_worktree_keeps_original_project_identity(tmp_path):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    tree = tmp_path / "tree"
    repo.mkdir()
    tree.mkdir()
    (repo / "AGENTS.md").write_text("Original", encoding="utf-8")
    (tree / "AGENTS.md").write_text("Worktree", encoding="utf-8")
    ordinary = RuleLoader(repo, home=home)
    task = RuleLoader(repo, tree, home=home)
    assert ordinary.paths[1][1] == task.paths[1][1]
    assert task.paths[2][1] == tree / "AGENTS.md"
    assert "Worktree" in task.load().message()["content"]
    assert "Original" not in task.load().message()["content"]


def test_bad_rule_files_do_not_break_agent(tmp_path):
    loader = RuleLoader(tmp_path, home=tmp_path / "home")
    global_path = loader.paths[0][1]
    global_path.parent.mkdir(parents=True)
    global_path.write_bytes(b"x" * 8193)
    assert loader.load().message() is None
    assert "larger than" in loader.load().sources[0].error
    global_path.write_bytes(b"\xff")
    assert "UnicodeDecodeError" in loader.load().sources[0].error


def test_rules_show_reports_sources_without_model_call(tmp_path, monkeypatch, capsys):
    loader = RuleLoader(tmp_path, home=tmp_path / "home")
    shared = loader.paths[2][1]
    shared.write_text("Use unittest", encoding="utf-8")
    agent = Agent(llm=ScriptedLLM([]), tools=[], rules=loader)
    repl_with(monkeypatch, agent, ["/rules show", "quit"])
    output = capsys.readouterr().out
    assert "Use unittest" in output
    assert "project shared" in output
    assert "AGENTS.md" in output
    assert agent.messages == []


def test_task_and_subagent_share_rule_context(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("init")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (repo / "AGENTS.md").write_text("Use repository conventions", encoding="utf-8")
    git("add", "AGENTS.md")
    git("commit", "-m", "rules")
    manager = TaskManager(tmp_path / "tasks", tmp_path / "trees")
    task, _ = manager.start(repo, "test rules", "mock")
    ordinary = SimpleNamespace(
        llm=SimpleNamespace(model="mock"), tools=[], context=SimpleNamespace(max_tokens=1000), hooks=None,
    )
    controller = TaskController(
        ordinary, SimpleNamespace(print=lambda *args, **kwargs: None),
        lambda *args: "once", lambda *args: "n",
    )
    controller.manager = manager
    controller._activate(task)
    assert controller.agent.rules.identity_root == repo
    assert controller.agent.rules.content_root == Path(task.worktree)
    snapshot = controller.agent.rules.load()
    assert "repository conventions" in snapshot.message()["content"]

    captured = {}

    class FakeSubAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def chat(self, _prompt):
            return "done"

    monkeypatch.setattr("corecoder.agent.Agent", FakeSubAgent)
    controller.agent._active_rules = snapshot
    tool = next(tool for tool in controller.agent.tools if isinstance(tool, AgentTool))
    tool.execute("inspect")
    assert captured["rules"] is controller.agent.rules
    assert captured["inherited_rules"] is snapshot
