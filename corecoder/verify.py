"""Explicit, reviewable validation for task worktrees."""

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .workspace import child_environment


@dataclass
class VerifyCommand:
    name: str
    argv: list[str]
    cwd: str
    timeout: int = 180


@dataclass
class VerifyResult:
    name: str
    argv: list[str]
    exit_code: int | None
    seconds: float
    summary: str
    kind: str

    def to_dict(self) -> dict:
        return asdict(self)


def _manifest_root(worktree: Path, cwd: Path) -> Path:
    for path in (cwd, *cwd.parents):
        if any((path / name).exists() for name in (
            ".corecoder/verify.json", "pyproject.toml", "pytest.ini", "package.json",
            "pom.xml", "build.gradle", "build.gradle.kts", "gradlew", "gradlew.bat",
        )):
            return path
        if path == worktree:
            break
    return cwd


def discover(worktree: Path, cwd: Path, target_platform: str | None = None) -> tuple[list[VerifyCommand], str]:
    """Return commands and a digest including config/script contents."""
    root = _manifest_root(worktree.resolve(), cwd.resolve())
    config = root / ".corecoder" / "verify.json"
    tracked: list[Path] = []
    commands: list[VerifyCommand] = []
    if config.exists():
        tracked.append(config)
        data = json.loads(config.read_text(encoding="utf-8"))
        entries = data.get("commands")
        if not isinstance(entries, list):
            raise ValueError("verify.json must contain a commands array.")
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict) or not isinstance(entry.get("argv"), list):
                raise TypeError("Each verify command needs an argv array.")
            argv = entry["argv"]
            if not argv or not all(isinstance(v, str) and v for v in argv):
                raise ValueError("Verification argv must contain nonempty strings.")
            timeout = entry.get("timeout", 180)
            if not isinstance(timeout, int) or not 1 <= timeout <= 1800:
                raise ValueError("Verification timeout must be 1–1800 seconds.")
            commands.append(VerifyCommand(entry.get("name", f"check-{i+1}"), argv, str(root), timeout))
    elif (root / "pom.xml").exists():
        tracked.append(root / "pom.xml")
        windows = os.name == "nt" and target_platform != "posix"
        wrapper = root / ("mvnw.cmd" if windows else "mvnw")
        if not wrapper.exists():
            wrapper = root / ("mvnw.bat" if windows else "mvnw")
        argv = [str(wrapper)] if wrapper.exists() else ["mvn"]
        if wrapper.exists():
            tracked.append(wrapper)
        commands.append(VerifyCommand("Maven tests", argv + ["test"], str(root), 300))
    elif any((root / n).exists() for n in ("build.gradle", "build.gradle.kts")):
        tracked += [root / n for n in ("build.gradle", "build.gradle.kts") if (root / n).exists()]
        windows = os.name == "nt" and target_platform != "posix"
        wrapper = root / ("gradlew.bat" if windows else "gradlew")
        if not wrapper.exists():
            wrapper = root / ("gradlew.cmd" if windows else "gradlew")
        argv = [str(wrapper)] if wrapper.exists() else ["gradle"]
        if wrapper.exists():
            tracked.append(wrapper)
        commands.append(VerifyCommand("Gradle tests", argv + ["test"], str(root), 300))
    elif (root / "package.json").exists():
        package = root / "package.json"
        tracked.append(package)
        scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts", {})
        manager = "npm"
        if (root / "pnpm-lock.yaml").exists():
            manager = "pnpm"
        elif (root / "yarn.lock").exists():
            manager = "yarn"
        for script in ("test", "lint"):
            if script in scripts:
                commands.append(VerifyCommand(f"{manager} {script}", [manager, "run", script], str(root)))
                try:
                    tokens = shlex.split(scripts[script])
                except ValueError:
                    tokens = []
                for token in tokens:
                    candidate = (root / token).resolve()
                    if candidate.is_file() and candidate.is_relative_to(worktree.resolve()):
                        tracked.append(candidate)
    elif (root / "pyproject.toml").exists() or (root / "pytest.ini").exists():
        tracked += [root / n for n in ("pyproject.toml", "pytest.ini") if (root / n).exists()]
        commands.append(VerifyCommand("Python tests", ["python", "-m", "pytest"], str(root)))
        pyproject = root / "pyproject.toml"
        if pyproject.exists() and "[tool.ruff" in pyproject.read_text(encoding="utf-8", errors="ignore"):
            commands.append(VerifyCommand("Ruff", ["ruff", "check", "."], str(root)))
    digest = hashlib.sha256()
    for command in commands:
        for token in command.argv:
            candidate = (root / token).resolve()
            if candidate.is_file() and candidate.is_relative_to(worktree.resolve()):
                tracked.append(candidate)
    digest.update(json.dumps([asdict(c) for c in commands], sort_keys=True).encode())
    for path in dict.fromkeys(tracked):
        digest.update(str(path.relative_to(worktree)).encode())
        digest.update(path.read_bytes())
    return commands, digest.hexdigest()


def run(commands: list[VerifyCommand], worktree: Path, executor=None) -> list[VerifyResult]:
    results = []
    for command in commands:
        cwd = Path(command.cwd).resolve()
        if not cwd.is_relative_to(worktree.resolve()):
            raise ValueError("Verification cwd is outside worktree.")
        start = time.monotonic()
        argv = list(command.argv)
        executable = argv[0]
        local_executable = (cwd / executable).resolve()
        if executor is None and local_executable.is_file() and local_executable.is_relative_to(worktree.resolve()):
            argv[0] = str(local_executable)
        elif executor is None and not Path(executable).is_file() and not shutil.which(executable):
            results.append(VerifyResult(command.name, command.argv, None, 0, f"Executable not found: {executable}", "missing"))
            break
        try:
            if executor is None:
                proc = subprocess.run(
                    argv, cwd=cwd, env=child_environment(),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    timeout=command.timeout, shell=False, check=False,
                )
            else:
                proc = executor.run_argv(argv, cwd, command.timeout)
            output = (proc.stdout or "") + (proc.stderr or "")
            if len(output) > 6000:
                output = output[:2500] + "\n... output truncated ...\n" + output[-2500:]
            lower = output.lower()
            environment_failure = any(marker in lower for marker in (
                "no module named pytest", "no module named ruff",
                "command not found", "is not recognized as an internal or external command",
                "cannot find module", "could not find or load main class",
                "cannot connect to the docker daemon", "error response from daemon",
                "permission denied while trying to connect to the docker api",
            ))
            kind = "passed" if proc.returncode == 0 else ("environment" if environment_failure else "failed")
            results.append(VerifyResult(
                command.name, command.argv, proc.returncode,
                round(time.monotonic()-start, 2), output, kind,
            ))
            if proc.returncode:
                break
        except subprocess.TimeoutExpired as e:
            output = (e.stdout or b"")
            if isinstance(output, bytes):
                output = output.decode("utf-8", "replace")
            results.append(VerifyResult(
                command.name, command.argv, None, round(time.monotonic()-start, 2),
                output[-3000:] + "\nTimed out", "timeout",
            ))
            break
        except OSError as e:
            results.append(VerifyResult(
                command.name, command.argv, None,
                round(time.monotonic()-start, 2), str(e), "environment",
            ))
            break
        except ValueError as e:
            results.append(VerifyResult(
                command.name, command.argv, None,
                round(time.monotonic()-start, 2), str(e), "environment",
            ))
            break
    return results
