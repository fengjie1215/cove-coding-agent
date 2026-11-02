"""File creation / overwrite."""

from pathlib import Path
from typing import ClassVar

from ..checkpoints import record as _record_checkpoint
from ..workspace import Workspace
from .base import Tool
from .edit import _changed_files


class WriteFileTool(Tool):
    def __init__(self, workspace: Workspace | None = None):
        self.workspace = workspace

    name = "write_file"
    description = (
        "Create a new file or completely overwrite an existing one. "
        "For small edits to existing files, prefer edit_file instead."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path for the file",
            },
            "content": {
                "type": "string",
                "description": "Full file content to write",
            },
        },
        "required": ["file_path", "content"],
    }

    def execute(self, file_path: str, content: str) -> str:
        from .base import FILE_MUTATION_LOCK

        try:
            with FILE_MUTATION_LOCK:
                p = self.workspace.resolve(file_path) if self.workspace else Path(file_path).expanduser().resolve()
                p.parent.mkdir(parents=True, exist_ok=True)
                if self.workspace:
                    self.workspace.checkpoints.record(p)
                else:
                    _record_checkpoint(p)
                p.write_text(content, encoding="utf-8")
                (self.workspace.changed_files if self.workspace else _changed_files).add(str(p))
            n_lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
            return f"Wrote {n_lines} lines to {file_path}"
        except Exception as e:  # noqa: BLE001
            # boundary: the agent gets an error string, not a traceback
            return f"Error: {e}"
