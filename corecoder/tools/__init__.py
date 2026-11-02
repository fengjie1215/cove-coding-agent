"""Tool registry."""

from .agent import AgentTool
from .bash import BashTool
from .edit import EditFileTool
from .glob_tool import GlobTool
from .grep import GrepTool
from .read import ReadFileTool
from .todo import TodoWriteTool
from .write import WriteFileTool

ALL_TOOLS = [
    BashTool(),
    ReadFileTool(),
    WriteFileTool(),
    EditFileTool(),
    GlobTool(),
    GrepTool(),
    TodoWriteTool(),
    AgentTool(),
]


def workspace_tools(workspace):
    """Fresh tool instances bound to a single task worktree."""
    return [
        BashTool(workspace), ReadFileTool(workspace), WriteFileTool(workspace),
        EditFileTool(workspace), GlobTool(workspace), GrepTool(workspace),
        TodoWriteTool(), AgentTool(),
    ]

