"""Interactive /task lifecycle; never used by one-shot mode."""

import json
from pathlib import Path

from .agent import Agent
from .hooks import Hooks
from .permissions import Permission
from .rules import RuleLoader
from .tasks import TaskManager
from .tools import workspace_tools
from .verify import discover, run
from .workspace import child_environment


class TaskController:
    def __init__(self, ordinary_agent: Agent, console, ask, prompt):
        self.ordinary = ordinary_agent
        self.console = console
        self.ask = ask
        self.prompt = prompt
        self.manager = TaskManager()
        self.task = None
        self.agent = None

    def _confirm(self, message: str) -> bool:
        try:
            return self.prompt(message).strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False

    def _show_results(self):
        for result in self.task.verify_results:
            self.console.print(
                f"  {result['name']}: {result['kind']} "
                f"(exit={result['exit_code']}, {result['seconds']}s)"
            )
            if result["kind"] != "passed" and result.get("summary"):
                self.console.print(result["summary"][-1500:], markup=False)

    def _activate(self, task):
        if task.status != "active" or not Path(task.worktree).is_dir():
            raise RuntimeError("Task is not active or its worktree is missing.")
        workspace = task.workspace(self.manager.tasks_dir)
        mcp = [tool for tool in self.ordinary.tools if tool.name.startswith("mcp__")]
        hooks = self.ordinary.hooks
        task_hooks = Hooks(hooks.pre, hooks.post, str(workspace.cwd), child_environment()) if hooks else None
        agent = Agent(
            llm=self.ordinary.llm, tools=[*workspace_tools(workspace), *mcp],
            max_context_tokens=self.ordinary.context.max_tokens,
            permission=Permission(
                ask=self.ask, persist_path=self.manager.tasks_dir / f"{task.id}.permissions.json"
            ),
            hooks=task_hooks, workspace=workspace,
            rules=RuleLoader(Path(task.repo), Path(task.worktree)),
        )
        agent.messages = task.messages
        self.task, self.agent = task, agent
        if mcp:
            self.console.print("[yellow]Note: external MCP tools are not isolated by Git Worktree.[/yellow]")

    def command(self, line: str) -> str | None:
        parts = line.split(maxsplit=2)
        action = parts[1] if len(parts) > 1 else ""
        value = parts[2] if len(parts) > 2 else ""
        if action == "start" and value:
            if self.task:
                raise RuntimeError("Leave the current task before starting another.")
            task, untracked = self.manager.start(Path.cwd(), value, self.ordinary.llm.model)
            self._activate(task)
            self.console.print(f"[green]Task {task.id} started in {task.worktree}[/green]")
            if untracked:
                self.console.print(f"[yellow]{len(untracked)} untracked file(s) were not copied into the worktree.[/yellow]")
            self.console.print("[yellow]Worktree isolates Git files, not Shell or external MCP side effects.[/yellow]")
            return value
        if action == "list":
            tasks = self.manager.list()
            for task in tasks:
                self.console.print(f"{task.id}  {task.status:9}  {task.prompt[:70]}")
            if not tasks:
                self.console.print("[dim]No tasks.[/dim]")
            return None
        if action == "resume" and value:
            if self.task:
                raise RuntimeError("Leave the current task first.")
            task = self.manager.load(value)
            self._activate(task)
            self.console.print(f"[green]Resumed task {task.id}[/green]")
            return None
        if action == "discard" and value:
            task = self.manager.load(value)
            if task.status == "discarded":
                raise RuntimeError("Task already discarded.")
            self.console.print(
                f"[yellow]This permanently removes task worktree {task.worktree} "
                "and its un-applied changes.[/yellow]"
            )
            if self._confirm("Discard this task? [y/N] "):
                self.manager.discard(task)
                if self.task and self.task.id == task.id:
                    self.task, self.agent = None, None
                self.console.print(f"[green]Discarded {task.id}.[/green]")
            return None
        if action == "leave" and not value:
            if self.task:
                self.task.messages = self.agent.messages
                self.task.save(self.manager.tasks_dir)
                self.console.print(f"[dim]Left task {self.task.id}; worktree retained.[/dim]")
                self.task, self.agent = None, None
            return None
        if not self.task:
            raise RuntimeError("No active task. Use /task start <requirement> or /task resume <id>.")
        if action == "status":
            current = self.manager.verification_current(self.task)
            self.console.print(
                f"Task {self.task.id}: {self.task.status}\n"
                f"Worktree: {self.task.worktree}\n"
                f"Validation: {self.task.verify_status}"
                + (" (current)" if current else " (stale or not passed)")
            )
            self._show_results()
            return None
        if action == "diff":
            patch = self.manager.patch(self.task).decode("utf-8", "replace")
            self.console.print(patch or "[dim]No changes.[/dim]", markup=False)
            return None
        if action == "verify":
            self.verify()
            return None
        if action == "apply" and value in ("", "--force"):
            patch = self.manager.patch(self.task).decode("utf-8", "replace")
            self.console.print(patch or "[dim]No changes.[/dim]", markup=False)
            self.console.print(
                f"Verification: {self.task.verify_status}; "
                f"current: {self.manager.verification_current(self.task)}"
            )
            self._show_results()
            if value == "--force":
                self.console.print("[yellow]--force bypasses verification only, not Git conflict checks.[/yellow]")
            if self._confirm("Apply this diff to the original project as uncommitted changes? [y/N] "):
                self.manager.apply(self.task, force=value == "--force")
                self.console.print("[green]Applied to original project (uncommitted).[/green]")
                self.task, self.agent = None, None
            return None
        raise ValueError(
            "Usage: /task start <requirement>|status|list|resume <id>|diff|verify|"
            "apply [--force]|leave|discard <id>"
        )

    def verify(self) -> str:
        task = self.task
        commands, command_digest = discover(Path(task.worktree), self.agent.workspace.cwd)
        if not commands:
            task.verify_status = "skipped"
            task.verify_results = []
            task.verified_digest = ""
            task.verified_command_digest = ""
            task.save(self.manager.tasks_dir)
            self.console.print("[yellow]No verification entry detected; application requires --force.[/yellow]")
            return "skipped"
        if task.approved_command_digest != command_digest:
            self.console.print("Verification commands (run without model API keys):")
            for command in commands:
                self.console.print(
                    f"  {json.dumps(command.argv, ensure_ascii=False)}  "
                    f"(cwd={command.cwd}, timeout={command.timeout}s)"
                )
            if not self._confirm("Run these commands? [y/N] "):
                task.verify_status = "skipped"
                task.verified_digest = ""
                task.verified_command_digest = ""
                task.save(self.manager.tasks_dir)
                return "skipped"
            task.approved_command_digest = command_digest
        before = self.manager.digest(task)
        results = run(commands, Path(task.worktree))
        after = self.manager.digest(task)
        task.verify_results = [result.to_dict() for result in results]
        task.verify_status = "passed" if results and all(result.kind == "passed" for result in results) else results[-1].kind
        task.verified_digest = before if task.verify_status == "passed" and before == after else ""
        task.verified_command_digest = command_digest if task.verify_status == "passed" and before == after else ""
        if before != after:
            task.verify_status = "stale"
        task.save(self.manager.tasks_dir)
        for result in results:
            self.console.print(f"{result.name}: {result.kind} (exit={result.exit_code}, {result.seconds}s)")
            if result.summary and result.kind != "passed":
                self.console.print(result.summary[-3000:], markup=False)
        return task.verify_status

    def after_chat(self, before_digest: str, chat):
        task = self.task
        task.messages = self.agent.messages
        task.save(self.manager.tasks_dir)
        if self.manager.digest(task) == before_digest:
            return
        for repair in range(3):
            status = self.verify()
            if status == "passed":
                return
            if status not in ("failed",) or repair == 2:
                return
            previous = self.manager.digest(task)
            self.console.print(f"[yellow]Verification failed; automatic repair {repair + 1}/2.[/yellow]")
            feedback = (
                "Automatic validation failed. Fix the cause in this task worktree, then summarize. "
                "Do not modify the original repository. Results:\n"
                + json.dumps(task.verify_results, ensure_ascii=False)[-6000:]
            )
            chat(self.agent, feedback)
            task.messages = self.agent.messages
            task.save(self.manager.tasks_dir)
            if self.manager.digest(task) == previous:
                self.console.print("[yellow]No new changes; stopping automatic repair.[/yellow]")
                return
