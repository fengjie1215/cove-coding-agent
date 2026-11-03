"""Confirmed task learning: suggestions are not memories until approved."""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from corecoder.cli import _memory_command
from corecoder.llm import LLMResponse
from corecoder.memory import MemoryStore
from corecoder.rules import RuleLoader, project_id
from corecoder.task_cli import TaskController
from corecoder.tasks import TaskManager
from corecoder.verify import discover


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _task(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "old.txt").write_text("old\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    manager = TaskManager(tmp_path / "tasks", tmp_path / "trees")
    task, _ = manager.start(repo, "Update the project entry file", "mock")
    tree = Path(task.worktree)
    (tree / "old.txt").write_text("new project entry\n", encoding="utf-8")
    (tree / ".corecoder").mkdir()
    (tree / ".corecoder" / "verify.json").write_text(json.dumps({
        "commands": [{"argv": [sys.executable, "-c", "print('ok')"]}],
    }), encoding="utf-8")
    _, task.verified_command_digest = discover(tree, tree)
    task.verify_status = "passed"
    task.verified_digest = manager.digest(task)
    task.verify_results = [{"name": "test", "kind": "passed", "exit_code": 0, "seconds": 0.1}]
    task.save(manager.tasks_dir)
    return repo, manager, task


class MockLLM:
    model = "mock"

    def __init__(self, content=None):
        self.calls = []
        self.content = content or json.dumps({"candidates": [{
            "content": "项目入口文件是 old.txt", "file": "old.txt", "reason": "文件内容标记为 project entry",
        }]}, ensure_ascii=False)

    def chat(self, messages, **_kwargs):
        self.calls.append(messages)
        return LLMResponse(content=self.content)


def _controller(manager, store, llm, answers=("y", "y")):
    responses = iter(answers)
    output = []
    ordinary = SimpleNamespace(
        llm=llm, tools=[], context=SimpleNamespace(max_tokens=1000),
        hooks=None, memory_store=store, plan_mode=False,
    )
    controller = TaskController(
        ordinary, SimpleNamespace(print=lambda *args, **kwargs: output.append(args[0])),
        lambda *args: "once", lambda *args: next(responses),
    )
    controller.manager = manager
    return controller, output


def test_verified_apply_proposes_but_does_not_store_until_accepted(tmp_path):
    repo, manager, task = _task(tmp_path)
    store = MemoryStore(tmp_path / "memory.sqlite3")
    llm = MockLLM()
    controller, _ = _controller(manager, store, llm)
    controller._activate(task)
    controller.command("/task apply")
    identity = project_id(repo)
    assert task.status == "applied" and task.learning_eligible
    assert (repo / "old.txt").read_text(encoding="utf-8") == "new project entry\n"
    assert len(llm.calls) == 1
    assert not store.search("项目入口文件", identity).hits
    candidate = store.list_candidates(identity)[0]
    assert candidate.task_id == task.id
    reopened = MemoryStore(store.path)
    assert reopened.get_candidate(candidate.id, identity) is not None
    controller.command(f"/task learn {task.id}")
    assert len(llm.calls) == 1
    entry = reopened.accept_candidate(candidate.id, identity, "项目入口文件位于 old.txt")
    assert reopened.search("项目入口文件", identity).hits[0].entry.id == entry.id
    assert entry.source_task_id == task.id
    assert entry.source_digest == task.learning_digest
    assert "old.txt" in entry.evidence
    assert reopened.disable(entry.id, identity)
    assert not reopened.search("项目入口文件", identity).hits
    assert reopened.get(entry.id, identity).status == "disabled"


def test_force_apply_and_stale_snapshot_cannot_learn(tmp_path):
    repo, manager, task = _task(tmp_path)
    store = MemoryStore(tmp_path / "memory.sqlite3")
    llm = MockLLM()
    manager.apply(task, force=True)
    controller, _ = _controller(manager, store, llm)
    with pytest.raises(RuntimeError, match="verified normal apply"):
        controller.command(f"/task learn {task.id}")
    assert not llm.calls and not store.list_candidates(project_id(repo))

    second = tmp_path / "second"
    second.mkdir()
    _, second_manager, second_task = _task(second)
    second_manager.apply(second_task)
    (Path(second_task.worktree) / "old.txt").write_text("stale\n", encoding="utf-8")
    controller.manager = second_manager
    with pytest.raises(RuntimeError, match="no longer current"):
        controller.command(f"/task learn {second_task.id}")
    assert not llm.calls


def test_secret_lines_and_rule_like_suggestions_are_filtered(tmp_path):
    _, manager, task = _task(tmp_path)
    tree = Path(task.worktree)
    (tree / "old.txt").write_text("new project entry\nAPI_KEY=sk-abcdefghijklmnop\n", encoding="utf-8")
    task.verified_digest = manager.digest(task)
    task.save(manager.tasks_dir)
    manager.apply(task)
    llm = MockLLM(json.dumps({"candidates": [
        {"content": "以后都必须写测试", "file": "old.txt", "reason": "test"},
        {"content": "API_KEY=sk-abcdefghijklmnop", "file": "old.txt", "reason": "key"},
        {"content": "项目入口文件是 old.txt", "file": "old.txt", "reason": "project entry"},
    ]}, ensure_ascii=False))
    store = MemoryStore(tmp_path / "memory.sqlite3")
    controller, _ = _controller(manager, store, llm)
    controller.command(f"/task learn {task.id}")
    assert "sk-abcdefghijklmnop" not in str(llm.calls)
    assert [item.content for item in store.list_candidates(project_id(Path(task.repo)))] == ["项目入口文件是 old.txt"]


def test_legacy_memory_db_migrates_and_rejected_candidate_is_erased(tmp_path):
    path = tmp_path / "memory.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE memories (id TEXT PRIMARY KEY, scope TEXT NOT NULL, project_id TEXT, "
        "content TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO memories VALUES ('old', 'project', 'project', 'legacy fact', 'now', 'now')"
    )
    connection.commit()
    connection.close()
    store = MemoryStore(path)
    assert store.get("old", "project").origin == "manual"
    assert store.search("legacy fact", "project").hits
    candidates = store.save_candidates("task123", "project", "digest", [("new fact", "file.txt: diff")])
    assert store.reject_candidate(candidates[0].id, "project")
    rejected = store.get_candidate(candidates[0].id, "project")
    assert rejected.content == "" and rejected.evidence == ""
    assert not store.list_candidates("project")
    assert store.save_candidates("task123", "project", "digest", [("duplicate", "file.txt: diff")]) == [rejected]


def test_memory_review_commands_accept_reject_why_and_disable(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    identity = project_id(repo)
    store = MemoryStore(tmp_path / "memory.sqlite3")
    first = store.save_candidates("firsttask", identity, "digest1", [("old wording", "old.txt: changed")])[0]
    second = store.save_candidates("secondtask", identity, "digest2", [("reject me", "old.txt: changed")])[0]
    agent = SimpleNamespace(memory_store=store, rules=RuleLoader(repo), plan_mode=False)
    _memory_command(agent, "candidates", lambda _: "y")
    _memory_command(agent, f"accept {first.id} corrected project fact", lambda _: "y")
    entry = store.list_entries(identity)[0]
    assert entry.content == "corrected project fact"
    _memory_command(agent, f"why {entry.id}", lambda _: "y")
    assert "firsttask" in capsys.readouterr().out
    _memory_command(agent, f"reject {second.id}", lambda _: "y")
    assert store.get_candidate(second.id, identity).content == ""
    _memory_command(agent, f"disable {entry.id}", lambda _: "y")
    assert not store.search("corrected project fact", identity).hits
    agent.plan_mode = True
    with pytest.raises(ValueError, match="disabled in plan mode"):
        _memory_command(agent, "accept nonexistent", lambda _: "y")


def test_declining_generation_avoids_extra_api_call_and_retry_works(tmp_path):
    repo, manager, task = _task(tmp_path)
    store = MemoryStore(tmp_path / "memory.sqlite3")
    llm = MockLLM()
    controller, _ = _controller(manager, store, llm, answers=("y", "n"))
    controller._activate(task)
    controller.command("/task apply")
    assert not llm.calls and not store.list_candidates(project_id(repo))
    controller.command(f"/task learn {task.id}")
    assert len(llm.calls) == 1


def test_learning_api_error_does_not_undo_successful_apply(tmp_path):
    repo, manager, task = _task(tmp_path)
    store = MemoryStore(tmp_path / "memory.sqlite3")

    class FailingLLM(MockLLM):
        def chat(self, messages, **_kwargs):
            raise RuntimeError("simulated provider failure")

    controller, output = _controller(manager, store, FailingLLM())
    controller._activate(task)
    controller.command("/task apply")
    assert task.status == "applied"
    assert (repo / "old.txt").read_text(encoding="utf-8") == "new project entry\n"
    assert "simulated provider failure" not in str(output)
    assert "Applied, but learning suggestions failed" in str(output)


def test_invalid_model_json_can_be_retried_without_saving(tmp_path):
    repo, manager, task = _task(tmp_path)
    manager.apply(task)
    store = MemoryStore(tmp_path / "memory.sqlite3")
    llm = MockLLM("not json")
    controller, _ = _controller(manager, store, llm)
    with pytest.raises(ValueError, match="invalid JSON"):
        controller.command(f"/task learn {task.id}")
    assert not store.learning_run_exists(task.id, project_id(repo), task.learning_digest)
    llm.content = json.dumps({"candidates": [{
        "content": "项目入口文件是 old.txt", "file": "old.txt", "reason": "verified diff",
    }]}, ensure_ascii=False)
    controller.command(f"/task learn {task.id}")
    assert len(llm.calls) == 2
    assert len(store.list_candidates(project_id(repo))) == 1
