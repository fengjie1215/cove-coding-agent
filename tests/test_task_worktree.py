"""Task worktree regression coverage."""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from corecoder.task_cli import TaskController
from corecoder.tasks import TaskManager
from corecoder.tools import workspace_tools
from corecoder.tools.agent import AgentTool
from corecoder.verify import discover, run


def git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True,
        capture_output=True,
    ).stdout.decode("utf-8", "replace").strip()


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "project"
    path.mkdir()
    git(path, "init")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test")
    (path / "old.txt").write_text("old\n")
    git(path, "add", ".")
    git(path, "commit", "-m", "base")
    return path


def test_task_patch_apply_new_deleted_binary(repo, tmp_path):
    manager = TaskManager(tmp_path / "tasks", tmp_path / "trees")
    task, _ = manager.start(repo, "change files", "mock")
    tree = Path(task.worktree)
    (tree / "old.txt").unlink()
    (tree / "new.txt").write_text("hello\n")
    (tree / "binary.dat").write_bytes(b"\x00\xff\x01")
    assert (repo / "old.txt").exists()
    assert not (repo / "new.txt").exists()
    patch = manager.patch(task)
    assert b"new.txt" in patch and b"binary.dat" in patch
    with pytest.raises(RuntimeError):
        manager.apply(task)
    manager.apply(task, force=True)
    assert not (repo / "old.txt").exists()
    assert (repo / "new.txt").read_text() == "hello\n"
    assert (repo / "binary.dat").read_bytes() == b"\x00\xff\x01"


def test_task_refuses_dirty_original_and_head_change(repo, tmp_path):
    manager = TaskManager(tmp_path / "tasks", tmp_path / "trees")
    (repo / "old.txt").write_text("dirty\n")
    with pytest.raises(RuntimeError):
        manager.start(repo, "x", "mock")
    git(repo, "restore", "old.txt")
    task, _ = manager.start(repo, "x", "mock")
    (Path(task.worktree) / "old.txt").write_text("task\n")
    (repo / "old.txt").write_text("other\n")
    with pytest.raises(RuntimeError):
        manager.apply(task, force=True)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "changed")
    with pytest.raises(RuntimeError):
        manager.apply(task, force=True)


def test_workspace_paths_and_resume(repo, tmp_path):
    manager = TaskManager(tmp_path / "tasks", tmp_path / "trees")
    task, _ = manager.start(repo, "x", "mock")
    task.messages = [{"role": "user", "content": "x"}]
    task.save(manager.tasks_dir)
    restored = manager.load(task.id)
    workspace = restored.workspace(manager.tasks_dir)
    assert restored.messages == task.messages
    assert workspace.resolve("old.txt") == Path(task.worktree) / "old.txt"
    with pytest.raises(ValueError):
        workspace.resolve(str(repo / "old.txt"))
    link = Path(task.worktree) / "escape"
    try:
        link.symlink_to(repo / "old.txt")
    except OSError:
        pass  # Windows may not grant symlink creation to this test process.
    else:
        with pytest.raises(ValueError):
            workspace.resolve("escape")
    bound = workspace_tools(workspace)
    assert all(tool.workspace is workspace for tool in bound[:6])


def test_task_agent_and_subagent_use_same_workspace(repo, tmp_path, monkeypatch):
    manager = TaskManager(tmp_path / "tasks", tmp_path / "trees")
    task, _ = manager.start(repo, "x", "mock")
    ordinary = SimpleNamespace(
        llm=SimpleNamespace(model="mock"),
        tools=[], context=SimpleNamespace(max_tokens=1000), hooks=None,
    )
    controller = TaskController(
        ordinary, SimpleNamespace(print=lambda *args, **kwargs: None),
        lambda *args: "once", lambda *args: "n",
    )
    controller.manager = manager
    controller._activate(task)
    assert str(task.worktree) in controller.agent._system
    assert controller.agent.workspace.root == Path(task.worktree)
    captured = {}

    class FakeSubAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def chat(self, prompt):
            return "done"

    monkeypatch.setattr("corecoder.agent.Agent", FakeSubAgent)
    tool = next(tool for tool in controller.agent.tools if isinstance(tool, AgentTool))
    assert "completed" in tool.execute("inspect")
    assert captured["workspace"] is controller.agent.workspace


def test_untracked_target_conflict_is_not_overwritten(repo, tmp_path):
    manager = TaskManager(tmp_path / "tasks", tmp_path / "trees")
    task, _ = manager.start(repo, "new file", "mock")
    (Path(task.worktree) / "clash.txt").write_text("task")
    (repo / "clash.txt").write_text("original")
    with pytest.raises(RuntimeError):
        manager.apply(task, force=True)
    assert (repo / "clash.txt").read_text() == "original"


def test_auto_repair_stops_after_two_attempts(repo, tmp_path):
    manager = TaskManager(tmp_path / "tasks", tmp_path / "trees")
    task, _ = manager.start(repo, "x", "mock")
    tree = Path(task.worktree)
    before = manager.digest(task)
    (tree / "old.txt").write_text("first")
    controller = TaskController.__new__(TaskController)
    controller.task = task
    controller.manager = manager
    controller.agent = SimpleNamespace(messages=[])
    controller.console = SimpleNamespace(print=lambda *args, **kwargs: None)
    controller.verify = lambda: "failed"
    attempts = []

    def repair(agent, feedback):
        attempts.append(feedback)
        (tree / "old.txt").write_text(f"attempt-{len(attempts)}")

    controller.after_chat(before, repair)
    assert len(attempts) == 2


def test_maven_and_gradle_wrapper_detection(repo, tmp_path):
    manager = TaskManager(tmp_path / "tasks", tmp_path / "trees")
    task, _ = manager.start(repo, "x", "mock")
    tree = Path(task.worktree)
    (tree / "pom.xml").write_text("<project/>")
    (tree / "mvnw").write_text("")
    (tree / "mvnw.cmd").write_text("")
    commands, _ = discover(tree, tree)
    assert commands[0].argv[0].endswith("mvnw.cmd" if sys.platform == "win32" else "mvnw")
    (tree / "pom.xml").unlink()
    (tree / "build.gradle").write_text("")
    (tree / "gradlew").write_text("")
    (tree / "gradlew.bat").write_text("")
    commands, _ = discover(tree, tree)
    assert commands[0].argv[0].endswith("gradlew.bat" if sys.platform == "win32" else "gradlew")


def test_verify_key_is_absent(repo, tmp_path, monkeypatch):
    manager = TaskManager(tmp_path / "tasks", tmp_path / "trees")
    task, _ = manager.start(repo, "x", "mock")
    tree = Path(task.worktree)
    (tree / ".corecoder").mkdir()
    (tree / ".corecoder" / "verify.json").write_text(json.dumps({
        "commands": [{"argv": [sys.executable, "-c", "import os; assert not os.getenv('CORECODER_API_KEY')"]}]
    }))
    monkeypatch.setenv("CORECODER_API_KEY", "dummy-secret")
    commands, _ = discover(tree, tree)
    assert run(commands, tree)[0].kind == "passed"


def test_verify_timeout_and_missing_program(repo, tmp_path):
    manager = TaskManager(tmp_path / "tasks", tmp_path / "trees")
    task, _ = manager.start(repo, "x", "mock")
    tree = Path(task.worktree)
    (tree / ".corecoder").mkdir()
    config = tree / ".corecoder" / "verify.json"
    config.write_text(json.dumps({"commands": [
        {"argv": [sys.executable, "-c", "import time; time.sleep(3)"], "timeout": 1}
    ]}))
    commands, _ = discover(tree, tree)
    assert run(commands, tree)[0].kind == "timeout"
    config.write_text(json.dumps({"commands": [{"argv": ["corecoder-no-such-command-123"]}]}))
    commands, _ = discover(tree, tree)
    assert run(commands, tree)[0].kind == "missing"


def test_verify_relative_wrapper(repo, tmp_path):
    manager = TaskManager(tmp_path / "tasks", tmp_path / "trees")
    task, _ = manager.start(repo, "x", "mock")
    tree = Path(task.worktree)
    if sys.platform == "win32":
        script = tree / "check.cmd"
        script.write_text("@echo off\necho ok\n")
    else:
        script = tree / "check.sh"
        script.write_text("#!/bin/sh\necho ok\n")
        script.chmod(0o755)
    (tree / ".corecoder").mkdir()
    (tree / ".corecoder" / "verify.json").write_text(json.dumps({
        "commands": [{"argv": [f"./{script.name}"]}]
    }))
    commands, _ = discover(tree, tree)
    assert run(commands, tree)[0].kind == "passed"


def test_verify_command_reapproval_and_snapshot(repo, tmp_path):
    manager = TaskManager(tmp_path / "tasks", tmp_path / "trees")
    task, _ = manager.start(repo, "x", "mock")
    tree = Path(task.worktree)
    (tree / ".corecoder").mkdir()
    config = tree / ".corecoder" / "verify.json"
    config.write_text(json.dumps({"commands": [{"argv": [sys.executable, "-c", "print('ok')"]}]}))
    approvals = []
    ordinary = SimpleNamespace(
        llm=SimpleNamespace(model="mock"), tools=[],
        context=SimpleNamespace(max_tokens=1000), hooks=None,
    )
    controller = TaskController(
        ordinary, SimpleNamespace(print=lambda *args, **kwargs: None),
        lambda *args: "once", lambda message: approvals.append(message) or "y",
    )
    controller.manager = manager
    controller._activate(task)
    assert controller.verify() == "passed"
    assert manager.verification_current(task)
    assert controller.verify() == "passed"
    assert len(approvals) == 1
    (tree / "old.txt").write_text("changed")
    assert not manager.verification_current(task)
    config.write_text(json.dumps({"commands": [{"argv": [sys.executable, "-c", "print('new')"]}]}))
    assert controller.verify() == "passed"
    assert len(approvals) == 2
