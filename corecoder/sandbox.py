"""Opt-in task execution in a local, pre-existing Linux Docker image.

Only the task worktree is mounted. The model API call stays in the host
process; neither its credentials nor the host environment enter the image.
"""

import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

from .tools.bash import BashTool, _check_dangerous
from .workspace import Workspace


class SandboxExecutor:
    _IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]*\Z")

    def __init__(self, worktree: Path, image: str):
        self.worktree = worktree.resolve()
        self.image = image.strip()
        if not self._IMAGE.fullmatch(self.image) or not self.worktree.is_dir():
            raise ValueError("Sandbox needs an existing worktree and an image name.")

    @staticmethod
    def preflight(image: str) -> None:
        if not image or not image.strip():
            raise RuntimeError("Set CORECODER_SANDBOX_IMAGE to a trusted local Linux image first.")
        if not SandboxExecutor._IMAGE.fullmatch(image.strip()):
            raise RuntimeError("CORECODER_SANDBOX_IMAGE is not a valid Docker image reference.")
        docker = shutil.which("docker")
        if docker is None:
            raise RuntimeError("Docker CLI is unavailable; sandbox task was not started.")
        docker_host = os.environ.get("DOCKER_HOST", "")
        if docker_host and not docker_host.startswith(("npipe://", "unix://")):
            raise RuntimeError("Remote Docker hosts are not supported for sandbox tasks.")
        try:
            context = subprocess.run(
                [docker, "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
                capture_output=True, text=True, timeout=15, check=False,
            )
            if context.returncode or not context.stdout.strip().startswith(("npipe://", "unix://")):
                raise RuntimeError("A local Docker context is required for sandbox tasks.")
            info = subprocess.run(
                [docker, "info", "--format", "{{.OSType}}"],
                capture_output=True, text=True, timeout=15, check=False,
            )
            if info.returncode or info.stdout.strip() != "linux":
                raise RuntimeError("A running Linux Docker engine is required for sandbox tasks.")
            local = subprocess.run(
                [docker, "image", "inspect", image],
                capture_output=True, timeout=15, check=False,
            )
            if local.returncode:
                raise RuntimeError("Sandbox image is not local; build or pull it explicitly first.")
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("Docker preflight failed; sandbox task was not started.") from exc

    def _container_cwd(self, cwd: Path) -> str:
        cwd = cwd.resolve()
        if not cwd.is_relative_to(self.worktree):
            raise ValueError("Sandbox cwd is outside the task worktree.")
        relative = cwd.relative_to(self.worktree).as_posix()
        return "/workspace" if relative == "." else "/workspace/" + relative

    def _argv(self, cwd: Path, name: str) -> list[str]:
        user = f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") else "65534:65534"
        return [
            "docker", "run", "--rm", "--pull=never", "--name", name,
            "--network=none", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--pids-limit=128",
            "--memory=1g", "--cpus=2", f"--user={user}",
            "--tmpfs=/tmp:rw,nosuid,nodev,size=256m",
            "--env=HOME=/tmp", "--env=TMPDIR=/tmp", "--env=PYTHONDONTWRITEBYTECODE=1",
            "--mount", f"type=bind,source={self.worktree},target=/workspace",
            "--workdir", self._container_cwd(cwd), self.image,
        ]

    def _run(self, command: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess:
        name = "corecoder-" + uuid.uuid4().hex[:20]
        argv = self._argv(cwd, name) + command
        try:
            return subprocess.run(
                argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout + 10, check=False,
            )
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            # Stopping the CLI alone can leave a container running. Remove the
            # exact container name we created before reporting the timeout.
            try:
                subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15, check=False)
            except (OSError, subprocess.TimeoutExpired):
                pass
            if isinstance(exc, KeyboardInterrupt):
                raise
            raise subprocess.TimeoutExpired(command, timeout) from exc

    def run_shell(self, command: str, cwd: Path, timeout: int) -> subprocess.CompletedProcess:
        return self._run(["sh", "-lc", command], cwd, timeout)

    def run_argv(self, argv: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess:
        mapped = []
        for token in argv:
            path = Path(token)
            if path.is_absolute():
                resolved = path.resolve()
                if not resolved.is_relative_to(self.worktree):
                    raise ValueError("Verification references a host path outside the worktree.")
                mapped.append("/workspace/" + resolved.relative_to(self.worktree).as_posix())
            else:
                mapped.append(token)
        return self._run(mapped, cwd, timeout)


class SandboxBashTool(BashTool):
    """Same tool schema, but no host shell or persistent host cwd."""

    def __init__(self, workspace: Workspace, executor: SandboxExecutor):
        super().__init__(workspace)
        self.executor = executor

    def execute(self, command: str, timeout: int = 120) -> str:
        warning = _check_dangerous(command)
        if warning:
            return f"⚠ Blocked: {warning}"
        try:
            proc = self.executor.run_shell(command, self.workspace.cwd, timeout)
            output = proc.stdout or ""
            if proc.stderr:
                output += "\n[stderr]\n" + proc.stderr
            if proc.returncode:
                output += f"\n[exit code: {proc.returncode}]"
            if len(output) > 15_000:
                output = output[:6000] + f"\n... truncated ({len(output)} chars total) ...\n" + output[-3000:]
            return output.strip() or "(no output)"
        except subprocess.TimeoutExpired:
            return f"Error: sandbox command timed out after {timeout}s"
        except (OSError, ValueError) as exc:
            return f"Error: sandbox execution failed ({type(exc).__name__})"
