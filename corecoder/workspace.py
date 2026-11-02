"""Per-task paths and mutable state, without changing the process cwd."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from .checkpoints import CheckpointStore

_MODEL_SECRETS = ("CORECODER_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY")


def child_environment() -> dict[str, str]:
    """Keep the model credential out of commands run on task code."""
    env = os.environ.copy()
    for name in _MODEL_SECRETS:
        env.pop(name, None)
    return env


@dataclass
class Workspace:
    root: Path
    cwd: Path
    checkpoints: CheckpointStore
    changed_files: set[str] = field(default_factory=set)
    shell_cwd: Path | None = None

    def __post_init__(self):
        self.root = self.root.resolve()
        self.cwd = self.cwd.resolve()
        if not self.cwd.is_relative_to(self.root):
            raise ValueError("Task cwd is outside its worktree")
        if self.shell_cwd is None:
            self.shell_cwd = self.cwd

    def resolve(self, raw: str) -> Path:
        """Reject absolute escapes, traversal, and symlinks leaving the tree."""
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = self.cwd / path
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError(f"Path is outside task worktree: {raw}")
        return resolved
