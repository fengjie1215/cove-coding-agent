"""Interactive /task lifecycle; never used by one-shot mode."""

import json
from pathlib import Path

from .agent import Agent
from .hooks import Hooks
from .learning import propose
from .permissions import Permission
from .rules import RuleLoader, project_id
from .tasks import TaskManager
from .trace import TaskTrace, describe, latest_issue
from .tools import workspace_tools
from .verify import discover, run
from .workspace import child_environment


class TaskController:
    def __init__(
        self, ordinary_agent: Agent, console, ask, prompt,
        manager: TaskManager | None = None, tool_factory=None,
        max_rounds: int = 50, verification_guard=None,
    ):
        self.ordinary = ordinary_agent
        self.console = console
        self.ask = ask
        self.prompt = prompt
        self.manager = manager if manager is not None else TaskManager()
        self.tool_factory = tool_factory or workspace_tools
        self.max_rounds = max_rounds
        self.verification_guard = verification_guard
        self.task = None
        self.agent = None
        self.trace = None

    def _event(self, event: str, **fields) -> None:
        trace = getattr(self, "trace", None)
        if trace is not None:
            trace.record(event, **fields)

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

    def _activate(self, task, resumed: bool = False):
        if task.status != "active" or not Path(task.worktree).is_dir():
            raise RuntimeError("Task is not active or its worktree is missing.")
        workspace = task.workspace(self.manager.tasks_dir)
        trace = TaskTrace(self.manager.tasks_dir, task.id)
        mcp = [tool for tool in self.ordinary.tools if tool.name.startswith("mcp__")]
        hooks = self.ordinary.hooks
        task_hooks = Hooks(hooks.pre, hooks.post, str(workspace.cwd), child_environment()) if hooks else None
        agent = Agent(
            llm=self.ordinary.llm, tools=[*self.tool_factory(workspace), *mcp],
            max_context_tokens=self.ordinary.context.max_tokens,
            max_rounds=self.max_rounds,
            permission=Permission(
                ask=self.ask, persist_path=self.manager.tasks_dir / f"{task.id}.permissions.json"
            ),
            hooks=task_hooks, workspace=workspace,
            rules=RuleLoader(
                Path(task.repo), Path(task.worktree),
                home=getattr(getattr(self.ordinary, "rules", None), "home", None),
            ),
            memory_store=getattr(self.ordinary, "memory_store", None),
            verification_managed=True,
            trace=trace,
        )
        agent.messages = task.messages
        self.task, self.agent = task, agent
        self.trace = trace
        self._event("task_resumed" if resumed else "task_started")
        if mcp:
            self.console.print("[yellow]Note: external MCP tools are not isolated by Git Worktree.[/yellow]")

    def learn(self, task):
        """Propose durable facts after a normally applied, verified task."""
        if getattr(self.ordinary, "plan_mode", False):
            raise RuntimeError("Task learning is disabled in plan mode.")
        store = getattr(self.ordinary, "memory_store", None)
        if store is None:
            raise RuntimeError("Memory storage is unavailable.")
        if task.status != "applied" or not task.learning_eligible:
            raise RuntimeError("Task learning requires a verified normal apply (not --force).")
        identity = project_id(Path(task.repo))
        if not store.learning_run_exists(task.id, identity, task.learning_digest):
            suggestions = propose(task, self.manager, self.ordinary.llm)
            store.save_candidates(task.id, identity, task.learning_digest, suggestions)
        candidates = store.list_candidates(identity, task_id=task.id)
        pending = [candidate for candidate in candidates if candidate.status == "pending"]
        if not pending:
            self.console.print("[dim]No pending memory candidates for this task.[/dim]")
        for candidate in pending:
            self.console.print(
                f"{candidate.id}  {candidate.content}\n  Evidence: {candidate.evidence}", markup=False,
            )
        if pending:
            self.console.print(
                "Review with /memory accept <id> [edited text] or /memory reject <id>. "
                "Nothing is saved as a memory until accepted."
            )

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
        if action == "trace":
            task = self.manager.load(value) if value else self.task
            if task is None:
                raise RuntimeError("No active task. Use /task trace <id> to inspect a saved task.")
            recorder = TaskTrace(self.manager.tasks_dir, task.id)
            rows = recorder.read()
            if not rows:
                self.console.print(f"[dim]No trace recorded for task {task.id}.[/dim]")
                return None
            self.console.print(f"Task {task.id}: {len(rows)} trace events (UTC)")
            issue = latest_issue(rows)
            if issue:
                self.console.print(f"Latest observed issue: {issue}", markup=False)
            for row in rows:
                self.console.print(describe(row), markup=False)
            return None
        if action == "learn" and value:
            self.learn(self.manager.load(value))
            return None
        if action == "resume" and value:
            if self.task:
                raise RuntimeError("Leave the current task first.")
            task = self.manager.load(value)
            self._activate(task, resumed=True)
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
                TaskTrace(self.manager.tasks_dir, task.id).record("task_discarded")
                if self.task and self.task.id == task.id:
                    self.task, self.agent = None, None
                    self.trace = None
                self.console.print(f"[green]Discarded {task.id}.[/green]")
            return None
        if action == "leave" and not value:
            if self.task:
                self.task.messages = self.agent.messages
                self.task.save(self.manager.tasks_dir)
                self._event("task_left")
                self.console.print(f"[dim]Left task {self.task.id}; worktree retained.[/dim]")
                self.task, self.agent = None, None
                self.trace = None
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
                task = self.task
                self.manager.apply(task, force=value == "--force")
                self._event("task_applied")
                self.console.print("[green]Applied to original project (uncommitted).[/green]")
                self.task, self.agent = None, None
                self.trace = None
                if task.learning_eligible and getattr(self.ordinary, "memory_store", None) is not None:
                    if self._confirm("Generate reviewable memory candidates with the model? [y/N] "):
                        try:
                            self.learn(task)
                        except Exception as exc:  # noqa: BLE001
                            self.console.print(
                                f"[yellow]Applied, but learning suggestions failed ({type(exc).__name__}). "
                                f"Retry with /task learn {task.id}.[/yellow]"
                            )
            return None
        raise ValueError(
            "Usage: /task start <requirement>|status|list|resume <id>|trace [id]|diff|verify|"
            "apply [--force]|learn <id>|leave|discard <id>"
        )

    def verify(self) -> str:
        self._event("verify_started")
        try:
            status = self._verify_inner()
        except Exception as exc:
            self._event("verify_finished", outcome="error", error_type=type(exc).__name__)
            raise
        self._event("verify_finished", outcome=status)
        return status

    def _verify_inner(self) -> str:
        task = self.task
        if self.verification_guard is not None:
            self.verification_guard(task, self.manager)
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
                self._event("verify_approval", outcome="denied")
                task.verify_status = "skipped"
                task.verified_digest = ""
                task.verified_command_digest = ""
                task.save(self.manager.tasks_dir)
                return "skipped"
            self._event("verify_approval", outcome="allowed")
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
        for index, result in enumerate(results, 1):
            self._event(
                "verify_command", round=index, outcome=result.kind,
                exit_code=result.exit_code, seconds=result.seconds,
            )
            self.console.print(f"{result.name}: {result.kind} (exit={result.exit_code}, {result.seconds}s)")
            if result.summary and result.kind != "passed":
                self.console.print(result.summary[-3000:], markup=False)
        return task.verify_status

    def after_chat(self, before_digest: str, chat):
        task = self.task
        task.messages = self.agent.messages
        task.save(self.manager.tasks_dir)
        changed_digest = self.manager.digest(task)
        if changed_digest == before_digest:
            return
        self._event("worktree_changed", diff_digest=changed_digest)
        for repair in range(3):
            status = self.verify()
            if status == "passed":
                return
            if status not in ("failed",) or repair == 2:
                self._event("repair_stopped", outcome=status)
                return
            previous = self.manager.digest(task)
            self._event("repair_started", attempt=repair + 1)
            self.console.print(f"[yellow]Verification failed; automatic repair {repair + 1}/2.[/yellow]")
            feedback = (
                "Automatic validation failed. Fix the cause in this task worktree, then summarize. "
                "Do not modify the original repository. Results:\n"
                + json.dumps(task.verify_results, ensure_ascii=False)[-6000:]
            )
            chat(self.agent, feedback)
            task.messages = self.agent.messages
            task.save(self.manager.tasks_dir)
            changed_digest = self.manager.digest(task)
            if changed_digest == previous:
                self._event("repair_stopped", outcome="no_change")
                self.console.print("[yellow]No new changes; stopping automatic repair.[/yellow]")
                return
            self._event("worktree_changed", diff_digest=changed_digest)
