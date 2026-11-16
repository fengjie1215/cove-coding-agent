"""Manual memory storage, retrieval, and agent context integration."""

import subprocess
from types import SimpleNamespace

from corecoder.agent import Agent
from corecoder.context import _approx_tokens
from corecoder.llm import LLMResponse, ToolCall
from corecoder.memory import MAX_INJECTED_TOKENS, MemoryStore
from corecoder.rules import RuleLoader, project_id
from corecoder.task_cli import TaskController
from corecoder.tasks import TaskManager
from corecoder.tools.agent import AgentTool
from tests.conftest import repl_with


def test_project_and_global_memory_persist_and_isolate(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    first = project_id(tmp_path / "first")
    second = project_id(tmp_path / "second")
    local = store.add("订单模块入口在 order/service.py", "project", first)
    global_entry = store.add("所有项目使用英文提交信息", "global")

    reopened = MemoryStore(store.path)
    assert {item.id for item in reopened.list_entries(first)} == {local.id, global_entry.id}
    assert {item.id for item in reopened.list_entries(second)} == {global_entry.id}
    assert reopened.get(local.id, second) is None
    assert reopened.search("订单模块入口", first).hits[0].entry.id == local.id
    assert not reopened.search("订单模块入口", second).hits
    assert reopened.search("英文提交信息", second).hits[0].entry.id == global_entry.id

    assert reopened.forget(local.id, second) is False
    assert reopened.forget(local.id, first) is True
    assert reopened.get(local.id, first) is None
    assert not reopened.search("订单模块入口", first).hits


def test_retrieval_is_relevant_bounded_and_supports_short_chinese(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    identity = project_id(tmp_path)
    wanted = store.add("订单模块入口在 order/service.py", "project", identity)
    store.add("前端按钮颜色是蓝色", "project", identity)
    assert store.search("订单", identity).hits[0].entry.id == wanted.id
    assert store.search("order service", identity).hits[0].entry.id == wanted.id
    assert not store.search("天气预报", identity).hits
    for index in range(5):
        store.add(f"订单模块入口说明 {index} " + "细节" * 700, "project", identity)
    snapshot = store.search("订单模块入口", identity)
    assert len(snapshot.hits) == 3
    assert _approx_tokens(snapshot.message()["content"]) <= MAX_INJECTED_TOKENS


def test_agent_injects_snapshot_not_session_and_reloads_next_turn(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    rules = RuleLoader(tmp_path / "repo", home=tmp_path / "home")
    rules.identity_root.mkdir()
    (rules.identity_root / "AGENTS.md").write_text("Use project rules", encoding="utf-8")
    memory = store.add("订单模块入口在 order/service.py", "project", project_id(rules.identity_root))

    class RecordingLLM:
        model = "mock"

        def __init__(self):
            self.calls = []

        def chat(self, messages, **_kwargs):
            self.calls.append([dict(item) for item in messages])
            if len(self.calls) == 1:
                store.forget(memory.id, project_id(rules.identity_root))
                return LLMResponse(tool_calls=[ToolCall("one", "missing_tool", {})])
            return LLMResponse(content="done")

    llm = RecordingLLM()
    agent = Agent(llm=llm, tools=[], rules=rules, memory_store=store)
    agent.chat("订单模块入口在哪里？")
    assert len(llm.calls) == 2
    assert all(memory.id in str(call) for call in llm.calls)
    assert memory.id in llm.calls[0][1]["content"]
    assert "Use project rules" in llm.calls[0][2]["content"]
    assert memory.id not in str(agent.messages)
    agent.chat("订单模块入口在哪里？")
    assert memory.id not in str(llm.calls[2])
    agent.reset()
    assert not agent.last_memories.hits


def test_repl_commands_do_not_call_model_and_require_delete_confirmation(tmp_path, monkeypatch, capsys):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    rules = RuleLoader(tmp_path / "repo", home=tmp_path / "home")
    agent = Agent(llm=type("NoModel", (), {"model": "mock"})(), tools=[], rules=rules, memory_store=store)
    repl_with(monkeypatch, agent, ["/memory add 订单模块入口在 order/service.py", "/memory list", "/memory search 订单模块", "quit"])
    entry = store.list_entries(project_id(rules.identity_root))[0]
    out = capsys.readouterr().out
    assert entry.id in out
    assert agent.messages == []

    repl_with(monkeypatch, agent, [f"/memory forget {entry.id}", "n", "quit"])
    assert store.get(entry.id, project_id(rules.identity_root)) is not None

    repl_with(monkeypatch, agent, ["/memory add --global 跨项目决策使用 SQLite", "n", "quit"])
    assert len(store.list_entries(project_id(rules.identity_root))) == 1
    repl_with(monkeypatch, agent, ["/memory why", f"/memory forget {entry.id}", "y", "quit"])
    assert store.get(entry.id, project_id(rules.identity_root)) is None
    assert "No memory was injected" in capsys.readouterr().out


def test_plan_mode_refuses_memory_changes(tmp_path, monkeypatch, capsys):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    rules = RuleLoader(tmp_path / "repo", home=tmp_path / "home")
    agent = Agent(llm=type("NoModel", (), {"model": "mock"})(), tools=[], rules=rules, memory_store=store)
    agent.plan_mode = True
    repl_with(monkeypatch, agent, ["/memory add should not persist", "quit"])
    assert not store.path.exists()
    assert "disabled in plan mode" in capsys.readouterr().out


def test_subagent_receives_same_memory_snapshot(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    rules = RuleLoader(tmp_path / "repo", home=tmp_path / "home")
    store.add("订单模块入口在 order/service.py", "project", project_id(rules.identity_root))
    parent = Agent(llm=type("NoModel", (), {"model": "mock"})(), tools=[AgentTool()], rules=rules, memory_store=store)
    parent._active_memories = store.search("订单模块入口", project_id(rules.identity_root))
    captured = {}

    class FakeSubAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def chat(self, _prompt):
            return "done"

    monkeypatch.setattr("corecoder.agent.Agent", FakeSubAgent)
    parent.tools[0].execute("inspect")
    assert captured["memory_store"] is store
    assert captured["inherited_memories"] is parent._active_memories


def test_task_worktree_uses_original_project_memory(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("init")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (repo / "README.md").write_text("demo", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-m", "initial")
    manager = TaskManager(tmp_path / "tasks", tmp_path / "trees")
    task, _ = manager.start(repo, "inspect memory", "mock")
    store = MemoryStore(tmp_path / "memory.sqlite3")
    entry = store.add("订单模块入口在 order/service.py", "project", project_id(repo))
    ordinary = SimpleNamespace(
        llm=SimpleNamespace(model="mock"), tools=[], context=SimpleNamespace(max_tokens=1000),
        hooks=None, memory_store=store,
    )
    controller = TaskController(ordinary, SimpleNamespace(print=lambda *args, **kwargs: None),
                                lambda *args: "once", lambda *args: "n")
    controller.manager = manager
    try:
        controller._activate(task)
        assert controller.agent.memory_store is store
        assert controller.agent.rules.identity_root == repo
        hits = controller.agent.memory_store.search(
            "订单模块入口", project_id(controller.agent.rules.identity_root)
        ).hits
        assert hits[0].entry.id == entry.id
    finally:
        manager.discard(task)


def test_broken_database_does_not_block_agent(tmp_path):
    path = tmp_path / "memory.sqlite3"
    path.write_bytes(b"not a SQLite database")
    rules = RuleLoader(tmp_path / "repo", home=tmp_path / "home")

    class NoopLLM:
        def chat(self, **_kwargs):
            return LLMResponse(content="still works")

    agent = Agent(llm=NoopLLM(), tools=[], rules=rules, memory_store=MemoryStore(path))
    assert agent.chat("订单模块入口") == "still works"
    assert "DatabaseError" in agent.memory_error
