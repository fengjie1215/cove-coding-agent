"""Opt-in sandbox task safety and regression tests (no Docker daemon needed)."""

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from corecoder.permissions import Permission
from corecoder.agent import Agent
from corecoder.llm import LLMResponse, ToolCall
from corecoder.memory import MemoryStore
from corecoder.rules import RuleLoader
from corecoder.sandbox import SandboxBashTool, SandboxExecutor
from corecoder.task_cli import TaskController
from corecoder.tasks import TaskManager
from corecoder.tools.base import Tool
from corecoder.trace import TaskTrace
from corecoder.verify import VerifyCommand, discover, run


def _git(repo: Path, *args: str):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "file.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    return repo


def test_permission_requires_each_call_and_never_saves_always(tmp_path):
    answers = iter(["always", "once", "deny"])
    path = tmp_path / "permissions.json"
    permission = Permission(ask=lambda *_: next(answers), persist_path=path, allow_always=False)
    assert "Permission denied" in permission.check("bash", {"command": "pwd"})
    assert permission.last_reason == "persistent_not_allowed"
    assert permission.check("bash", {"command": "pwd"}) is None
    assert permission.last_reason == "approved_once"
    assert "Permission denied" in permission.check("bash", {"command": "pwd"})
    assert permission.last_reason == "approval_denied"
    assert not path.exists()


def test_docker_preflight_fails_closed_when_unavailable(monkeypatch):
    monkeypatch.setattr("corecoder.sandbox.shutil.which", lambda _: None)
    with pytest.raises(RuntimeError, match="image"):
        SandboxExecutor.preflight("")
    with pytest.raises(RuntimeError, match="valid Docker image"):
        SandboxExecutor.preflight("--privileged")
    with pytest.raises(RuntimeError, match="Docker CLI"):
        SandboxExecutor.preflight("trusted:test")


def test_remote_docker_host_rejected_before_any_container(monkeypatch):
    monkeypatch.setattr("corecoder.sandbox.shutil.which", lambda _: "docker")
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote.example:2376")
    monkeypatch.setattr("corecoder.sandbox.subprocess.run", lambda *_a, **_k: pytest.fail("docker called"))
    with pytest.raises(RuntimeError, match="Remote Docker"):
        SandboxExecutor.preflight("trusted:test")


def test_start_command_checks_docker_before_creating_worktree(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CORECODER_SANDBOX_IMAGE", "trusted:test")
    manager = TaskManager(tmp_path / "tasks", tmp_path / "trees")
    ordinary = SimpleNamespace(llm=SimpleNamespace(model="mock"), tools=[],
                               context=SimpleNamespace(max_tokens=1000), hooks=None)
    controller = TaskController(ordinary, SimpleNamespace(print=lambda *_a, **_k: None),
                                lambda *_: "once", lambda _: "n", manager=manager)

    def unavailable(_image):
        raise RuntimeError("Docker unavailable")

    monkeypatch.setattr(SandboxExecutor, "preflight", unavailable)
    with pytest.raises(RuntimeError, match="Docker unavailable"):
        controller.command("/task start --sandbox update status")
    assert manager.list() == []
    assert not (tmp_path / "trees").exists()


def test_executor_flags_paths_and_no_host_env(tmp_path, monkeypatch):
    root = tmp_path / "worktree"
    child = root / "src"
    child.mkdir(parents=True)
    script = child / "test.sh"
    script.write_text("echo ok\n", encoding="utf-8")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr("corecoder.sandbox.subprocess.run", fake_run)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret-do-not-pass")
    executor = SandboxExecutor(root, "trusted:test")
    assert executor.run_shell("pwd", child, 10).stdout == "ok"
    argv, kwargs = calls[-1]
    assert "--network=none" in argv and "--read-only" in argv
    assert "--cap-drop=ALL" in argv and "--pull=never" in argv
    assert "--user=65534:65534" in argv
    assert "--workdir" in argv and "/workspace/src" in argv
    assert "secret-do-not-pass" not in repr(argv) and "env" not in kwargs
    assert argv[-3:] == ["sh", "-lc", "pwd"]
    executor.run_argv([str(script), "arg"], child, 10)
    assert calls[-1][0][-2:] == ["/workspace/src/test.sh", "arg"]
    with pytest.raises(ValueError, match="host path"):
        executor.run_argv([str(tmp_path / "outside")], child, 10)
    with pytest.raises(ValueError, match="outside"):
        executor.run_shell("pwd", tmp_path, 10)


def test_timeout_forces_removal_of_exact_container(tmp_path, monkeypatch):
    root = tmp_path / "worktree"
    root.mkdir()
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "run":
            raise subprocess.TimeoutExpired(argv, 11)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("corecoder.sandbox.subprocess.run", fake_run)
    with pytest.raises(subprocess.TimeoutExpired):
        SandboxExecutor(root, "trusted:test").run_shell("sleep 99", root, 1)
    created_name = calls[0][calls[0].index("--name") + 1]
    assert calls[1] == ["docker", "rm", "-f", created_name]


def test_interruption_forces_removal_of_exact_container(tmp_path, monkeypatch):
    root = tmp_path / "worktree"
    root.mkdir()
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "run":
            raise KeyboardInterrupt
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("corecoder.sandbox.subprocess.run", fake_run)
    with pytest.raises(KeyboardInterrupt):
        SandboxExecutor(root, "trusted:test").run_shell("sleep 99", root, 1)
    created_name = calls[0][calls[0].index("--name") + 1]
    assert calls[1] == ["docker", "rm", "-f", created_name]


def test_sandbox_task_persists_mode_and_disables_host_extensions(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    manager = TaskManager(tmp_path / "tasks", tmp_path / "trees")
    task, _ = manager.start(repo, "inspect", "mock", sandbox_image="trusted:test")
    assert manager.load(task.id).sandbox_image == "trusted:test"
    monkeypatch.setattr(SandboxExecutor, "preflight", lambda _: None)

    class MCPTool(Tool):
        name = "mcp__external"
        description = "external"
        parameters = {"type": "object", "properties": {}}

        def execute(self):
            return "external"

    ordinary = SimpleNamespace(
        llm=SimpleNamespace(model="mock"), tools=[MCPTool()],
        context=SimpleNamespace(max_tokens=1000), hooks=SimpleNamespace(pre=["host"], post=[]),
    )
    output = []
    controller = TaskController(
        ordinary, SimpleNamespace(print=lambda *args, **_kwargs: output.extend(args)),
        ask=lambda *_: "always", strict_ask=lambda *_: "once", prompt=lambda _: "n", manager=manager,
    )
    controller._activate(task)
    assert controller.sandbox is not None
    assert controller.agent.hooks is None
    assert not any(tool.name.startswith("mcp__") for tool in controller.agent.tools)
    assert isinstance(next(tool for tool in controller.agent.tools if tool.name == "bash"), SandboxBashTool)
    assert controller.agent.permission.allow_always is False
    controller.command("/task leave")
    assert controller.sandbox is None
    resumed = TaskController(ordinary, SimpleNamespace(print=lambda *_a, **_k: None),
                             lambda *_: "once", lambda _: "n", manager=manager)
    resumed.command(f"/task resume {task.id}")
    assert resumed.sandbox is not None


def test_verification_uses_executor_not_host(tmp_path, monkeypatch):
    root = tmp_path / "tree"
    root.mkdir()
    command = VerifyCommand("check", ["python", "-m", "pytest"], str(root), 10)
    seen = []

    class FakeExecutor:
        def run_argv(self, argv, cwd, timeout):
            seen.append((argv, cwd, timeout))
            return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr("corecoder.verify.subprocess.run", lambda *_a, **_k: pytest.fail("host execution"))
    assert run([command], root, executor=FakeExecutor())[0].kind == "passed"
    assert seen == [(["python", "-m", "pytest"], root, 10)]


def test_docker_failure_is_environment_not_code_failure(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()

    class FailedDocker:
        def run_argv(self, argv, cwd, timeout):
            return subprocess.CompletedProcess(argv, 125, "", "docker: Error response from daemon")

    result = run([VerifyCommand("check", ["python", "-m", "pytest"], str(root))],
                 root, executor=FailedDocker())[0]
    assert result.kind == "environment"


def test_posix_wrapper_selected_for_sandbox_digest(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    (repo / "pom.xml").write_text("<project/>", encoding="utf-8")
    (repo / "mvnw").write_text("#!/bin/sh\n", encoding="utf-8")
    (repo / "mvnw.cmd").write_text("echo windows\n", encoding="utf-8")
    commands, digest = discover(repo, repo, target_platform="posix")
    assert Path(commands[0].argv[0]).name == "mvnw"
    assert len(digest) == 64


def test_sandbox_bash_does_not_call_host_shell(tmp_path, monkeypatch):
    from corecoder.workspace import Workspace
    from corecoder.checkpoints import CheckpointStore

    root = tmp_path / "tree"
    root.mkdir()
    workspace = Workspace(root, root, CheckpointStore(tmp_path / "undo.json", root))
    calls = []

    class FakeExecutor:
        def run_shell(self, command, cwd, timeout):
            calls.append((command, cwd, timeout))
            return subprocess.CompletedProcess([], 0, "safe", "")

    monkeypatch.setattr("corecoder.tools.bash.run_shell", lambda *_a, **_k: pytest.fail("host shell"))
    tool = SandboxBashTool(workspace, FakeExecutor())
    assert tool.execute("pwd") == "safe"
    assert calls == [("pwd", root, 120)]


@pytest.mark.skipif(not os.getenv("CORECODER_SANDBOX_IMAGE"), reason="set a trusted local image for Docker acceptance")
def test_real_docker_shell_and_verification(tmp_path, monkeypatch):
    image = os.environ["CORECODER_SANDBOX_IMAGE"]
    SandboxExecutor.preflight(image)
    root = tmp_path / "tree"
    root.mkdir()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "not-a-real-key-sentinel")
    executor = SandboxExecutor(root, image)
    shell = executor.run_shell(
        "test -z \"$(printenv DEEPSEEK_API_KEY)\" && printf ok > result.txt", root, 15,
    )
    assert shell.returncode == 0, shell.stderr
    assert (root / "result.txt").read_text(encoding="utf-8") == "ok"
    blocked = executor.run_shell("touch /host_marker", root, 15)
    assert blocked.returncode != 0  # read-only container root
    command = VerifyCommand(
        "fixture", ["python", "-c", "from pathlib import Path; assert Path('result.txt').read_text() == 'ok'"],
        str(root), 15,
    )
    assert run([command], root, executor=executor)[0].kind == "passed"


@pytest.mark.skipif(not os.getenv("CORECODER_SANDBOX_IMAGE"), reason="set a trusted local image for Docker acceptance")
def test_real_docker_task_validation_flow(tmp_path):
    image = os.environ["CORECODER_SANDBOX_IMAGE"]
    SandboxExecutor.preflight(image)
    repo = _repo(tmp_path)
    (repo / "data.txt").write_text("status=pending\n", encoding="utf-8")
    config = repo / ".corecoder" / "verify.json"
    config.parent.mkdir()
    check = "from pathlib import Path; assert Path('data.txt').read_text().strip() == 'status=ready'"
    config.write_text(json.dumps({"commands": [{"name": "check", "argv": ["python", "-c", check]}]}), encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")

    class ScriptedLLM:
        model = "mock"

        def __init__(self):
            self.responses = iter([
                LLMResponse(tool_calls=[ToolCall("read", "read_file", {"file_path": "data.txt"})]),
                LLMResponse(tool_calls=[ToolCall("edit", "edit_file", {
                    "file_path": "data.txt", "old_string": "status=pending", "new_string": "status=ready",
                })]),
                LLMResponse(content="done"),
            ])

        def chat(self, **_kwargs):
            return next(self.responses)

    llm = ScriptedLLM()
    ordinary = Agent(llm, tools=[], memory_store=MemoryStore(tmp_path / "memory.sqlite3"),
                     rules=RuleLoader(repo, home=tmp_path / "home"))
    manager = TaskManager(tmp_path / "tasks", tmp_path / "trees")
    task, _ = manager.start(repo, "update status", llm.model, sandbox_image=image)
    controller = TaskController(
        ordinary, SimpleNamespace(print=lambda *_args, **_kwargs: None),
        ask=lambda *_: "deny", strict_ask=lambda *_: "once", prompt=lambda _: "y", manager=manager,
    )
    controller._activate(task)
    before = manager.digest(task)
    assert controller.agent.chat("update status") == "done"
    controller.after_chat(before, lambda *_: pytest.fail("repair was not expected"))
    assert task.verify_status == "passed" and manager.verification_current(task)
    assert (repo / "data.txt").read_text(encoding="utf-8") == "status=pending\n"
    rows = TaskTrace(manager.tasks_dir, task.id).read()
    assert any(row["event"] == "tool_decision" and row.get("reason") == "approved_once" for row in rows)
    assert any(row["event"] == "verify_finished" and row.get("outcome") == "passed" for row in rows)
    controller.command(f"/task discard {task.id}")
    assert not Path(task.worktree).exists()
