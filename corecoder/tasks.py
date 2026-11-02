"""Persistent, review-before-apply Git worktree tasks."""

import hashlib
import json
import re
import subprocess
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .checkpoints import CheckpointStore
from .workspace import Workspace, child_environment

TASKS_DIR = Path.home() / ".corecoder" / "tasks"
WORKTREES_DIR = Path.home() / ".corecoder" / "worktrees"


def _git(directory: Path, *args: str, input_data: bytes | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-C", str(directory), *args], input=input_data,
        capture_output=True, check=False,
        env=child_environment(),
    )
    if proc.returncode:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip() or f"git {args[0]} failed")
    return proc


def _registered_worktree(repo: Path, target: Path) -> bool:
    listing = _git(repo, "worktree", "list", "--porcelain").stdout.decode("utf-8", "replace")
    return any(
        Path(line[len("worktree "):]).resolve() == target.resolve()
        for line in listing.splitlines() if line.startswith("worktree ")
    )


@dataclass
class Task:
    id: str
    repo: str
    worktree: str
    base_sha: str
    relative_cwd: str
    prompt: str
    model: str
    status: str = "active"
    messages: list[dict] = field(default_factory=list)
    verify_status: str = "not_run"
    verified_digest: str = ""
    approved_command_digest: str = ""
    verified_command_digest: str = ""
    verify_results: list[dict] = field(default_factory=list)

    def save(self, directory: Path = TASKS_DIR):
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{self.id}.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)

    def workspace(self, directory: Path = TASKS_DIR) -> Workspace:
        root = Path(self.worktree)
        return Workspace(root, root / self.relative_cwd, CheckpointStore(directory / f"{self.id}.undo.json", root))


class TaskManager:
    def __init__(self, tasks_dir: Path = TASKS_DIR, worktrees_dir: Path = WORKTREES_DIR):
        self.tasks_dir = tasks_dir.resolve()
        self.worktrees_dir = worktrees_dir.resolve()

    def start(self, cwd: Path, prompt: str, model: str) -> tuple[Task, list[str]]:
        cwd = cwd.resolve()
        repo = Path(_git(cwd, "rev-parse", "--show-toplevel").stdout.decode().strip()).resolve()
        if _git(repo, "status", "--porcelain", "-uno").stdout:
            raise RuntimeError("Tracked files in the original repository must be clean before starting a task.")
        sha = _git(repo, "rev-parse", "HEAD").stdout.decode().strip()
        warnings = _git(repo, "ls-files", "--others", "--exclude-standard").stdout.decode("utf-8", "replace").splitlines()
        task_id = uuid.uuid4().hex[:12]
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        path = (self.worktrees_dir / task_id).resolve()
        if path.exists():
            raise RuntimeError(f"Task path already exists: {path}")
        _git(repo, "worktree", "add", "--detach", str(path), sha)
        task = Task(task_id, str(repo), str(path), sha, str(cwd.relative_to(repo)), prompt, model)
        try:
            task.save(self.tasks_dir)
        except Exception:
            _git(repo, "worktree", "remove", "--force", str(path))
            raise
        return task, warnings

    def load(self, task_id: str) -> Task:
        if not re.fullmatch(r"[0-9a-f]{12}", task_id):
            raise ValueError("Invalid task ID.")
        data = json.loads((self.tasks_dir / f"{task_id}.json").read_text(encoding="utf-8"))
        task = Task(**data)
        if task.id != task_id or Path(task.worktree).resolve() != (self.worktrees_dir / task_id).resolve():
            raise ValueError("Task metadata has an invalid worktree path.")
        if not Path(task.worktree).resolve().is_relative_to(self.worktrees_dir):
            raise ValueError("Task worktree escaped its storage directory.")
        return task

    def list(self) -> list[Task]:
        tasks = []
        for path in sorted(self.tasks_dir.glob("*.json")):
            if re.fullmatch(r"[0-9a-f]{12}\.json", path.name):
                try:
                    tasks.append(self.load(path.stem))
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    continue
        return tasks

    def patch(self, task: Task) -> bytes:
        if task.status == "discarded":
            raise RuntimeError("Task worktree has been discarded.")
        root = Path(task.worktree)
        _git(root, "add", "-A")
        return _git(root, "diff", "--cached", "--binary", task.base_sha).stdout

    def digest(self, task: Task) -> str:
        return hashlib.sha256(self.patch(task)).hexdigest()

    def verification_current(self, task: Task) -> bool:
        if task.verify_status != "passed" or task.verified_digest != self.digest(task):
            return False
        from .verify import discover
        try:
            _, command_digest = discover(Path(task.worktree), Path(task.worktree) / task.relative_cwd)
        except (OSError, ValueError, TypeError):
            return False
        return task.verified_command_digest == command_digest

    def apply(self, task: Task, force: bool = False):
        if task.status != "active":
            raise RuntimeError("Only an active task can be applied.")
        patch = self.patch(task)
        if not patch:
            raise RuntimeError("Task has no changes to apply.")
        if not force and not self.verification_current(task):
            raise RuntimeError(
                "Verification did not pass for the current diff. "
                "Run /task verify or explicitly use /task apply --force."
            )
        repo = Path(task.repo)
        if Path(_git(repo, "rev-parse", "--show-toplevel").stdout.decode().strip()).resolve() != repo.resolve():
            raise RuntimeError("Original repository path no longer matches task metadata.")
        if not _registered_worktree(repo, Path(task.worktree)):
            raise RuntimeError("Task worktree is no longer registered with the original repository.")
        if _git(repo, "rev-parse", "HEAD").stdout.decode().strip() != task.base_sha:
            raise RuntimeError("Original repository HEAD changed; task cannot be applied automatically.")
        if _git(repo, "status", "--porcelain", "-uno").stdout:
            raise RuntimeError("Original repository has tracked changes; task cannot be applied.")
        _git(repo, "apply", "--check", "--binary", "-", input_data=patch)
        _git(repo, "apply", "--binary", "-", input_data=patch)
        task.status = "applied"
        task.save(self.tasks_dir)

    def discard(self, task: Task):
        path = Path(task.worktree).resolve()
        if path != (self.worktrees_dir / task.id).resolve() or not path.is_relative_to(self.worktrees_dir) or not path.is_dir():
            raise RuntimeError("Refusing to remove an unverified task worktree.")
        if not _registered_worktree(Path(task.repo), path):
            raise RuntimeError("Task path is not a registered worktree.")
        _git(Path(task.repo), "worktree", "remove", "--force", str(path))
        task.status = "discarded"
        task.save(self.tasks_dir)
