"""Core agent loop.

This is the heart of CoreCoder.  The pattern is simple:

    user message -> LLM (with tools) -> tool calls? -> execute -> loop
                                      -> text reply? -> return to user

It keeps looping until the LLM responds with plain text (no tool calls),
which means it's done working and ready to report back.
"""

import concurrent.futures
import inspect
import sqlite3
import time
import uuid

from .context import ContextManager
from .llm import LLM
from .memory import MemorySnapshot, MemoryStore
from .permissions import Permission
from .prompt import PLAN_MODE_PROMPT, system_prompt
from .rules import RuleLoader, RuleSnapshot, project_id
from .trace import TaskTrace
from .tools import ALL_TOOLS
from .tools.agent import AgentTool
from .tools.base import Tool
from .tools.todo import TodoWriteTool


class Agent:
    def __init__(
        self,
        llm: LLM,
        tools: list[Tool] | None = None,
        max_context_tokens: int = 128_000,
        max_rounds: int = 50,
        permission=None,
        hooks=None,
        workspace=None,
        rules: RuleLoader | None = None,
        inherited_rules: RuleSnapshot | None = None,
        memory_store: MemoryStore | None = None,
        inherited_memories: MemorySnapshot | None = None,
        verification_managed: bool = False,
        trace: TaskTrace | None = None,
        trace_role: str = "main",
    ):
        self.llm = llm
        self.tools = tools if tools is not None else ALL_TOOLS
        self.permission = permission
        self.hooks = hooks
        self.workspace = workspace
        self.rules = rules if rules is not None else RuleLoader.for_cwd()
        self._inherited_rules = inherited_rules
        self._active_rules: RuleSnapshot | None = None
        self.memory_store = memory_store if memory_store is not None else MemoryStore()
        self._inherited_memories = inherited_memories
        self._active_memories: MemorySnapshot | None = None
        self.last_memories = MemorySnapshot()
        self.memory_error: str | None = None
        self._tool_by_name = {t.name: t for t in self.tools}
        self.messages: list[dict] = []
        self.context = ContextManager(max_tokens=max_context_tokens)
        self.max_rounds = max_rounds
        self._system = system_prompt(
            self.tools, cwd=str(workspace.cwd) if workspace else None,
            verification_managed=verification_managed,
        )
        self.last_stop_reason: str | None = None
        self.plan_mode = False  # toggled by /plan; while on, mutating tools are refused
        self.trace = trace
        self.trace_role = trace_role
        self._trace_turn_id: str | None = None

        # wire up sub-agent capability
        for t in self.tools:
            if isinstance(t, AgentTool):
                t._parent_agent = self

        self._todo = next((t for t in self.tools if isinstance(t, TodoWriteTool)), None)

    def _full_messages(self) -> list[dict]:
        system = self._system
        # re-injected every round, like the task list below, so a toggle made
        # between turns takes effect on the very next request
        if self.plan_mode:
            system += "\n\n" + PLAN_MODE_PROMPT
        # the task list is re-injected every round, so the model always sees the
        # current state rather than a stale copy buried in old tool results
        if self._todo is not None:
            rendered = self._todo.render()
            if rendered:
                system += "\n\n# Current task list\n" + rendered
        snapshot = self._active_rules if self._active_rules is not None else self.rules.load()
        rule_message = snapshot.message()
        memory_message = self._active_memories.message() if self._active_memories is not None else None
        return ([{"role": "system", "content": system}]
                + ([memory_message] if memory_message else [])
                + ([rule_message] if rule_message else [])
                + self.messages)

    def _tool_schemas(self) -> list[dict]:
        return [t.schema() for t in self.tools]

    def chat(self, user_input: str, on_token=None, on_tool=None, on_reasoning=None) -> str:
        """Process one user message. May involve multiple LLM/tool rounds."""
        self._trace_turn_id = uuid.uuid4().hex[:12] if self.trace else None
        self._trace_event("turn_started")
        try:
            result = self._chat_inner(user_input, on_token, on_tool, on_reasoning)
        except KeyboardInterrupt:
            self._trace_event("turn_finished", outcome="interrupted")
            raise
        except Exception as exc:
            self._trace_event("turn_finished", outcome="error", error_type=type(exc).__name__)
            raise
        else:
            self._trace_event("turn_finished", outcome=self.last_stop_reason or "completed")
            return result
        finally:
            self._trace_turn_id = None

    def _trace_event(self, event: str, **fields) -> None:
        if self.trace is not None:
            self.trace.record(event, role=self.trace_role, turn_id=self._trace_turn_id, **fields)

    def _chat_inner(self, user_input, on_token, on_tool, on_reasoning) -> str:
        self.last_stop_reason = None
        self._active_rules = self._inherited_rules if self._inherited_rules is not None else self.rules.load()
        self.memory_error = None
        if self._inherited_memories is not None:
            self._active_memories = self._inherited_memories
        else:
            try:
                self._active_memories = self.memory_store.search(user_input, project_id(self.rules.identity_root))
            except (OSError, sqlite3.Error) as exc:
                self._active_memories = MemorySnapshot()
                self.memory_error = f"{type(exc).__name__}: {exc}"
        self.last_memories = self._active_memories
        self.messages.append({"role": "user", "content": user_input})
        self.context.maybe_compress(self.messages, self.llm)

        for round_number in range(1, self.max_rounds + 1):
            self._trace_event("model_started", round=round_number)
            started = time.monotonic()
            try:
                resp = self.llm.chat(
                    messages=self._full_messages(),
                    tools=self._tool_schemas(),
                    on_token=on_token,
                    on_reasoning=on_reasoning,
                )
            except Exception as exc:
                self._trace_event(
                    "model_finished", round=round_number, outcome="error",
                    error_type=type(exc).__name__, seconds=time.monotonic() - started,
                )
                raise
            self._trace_event(
                "model_finished", round=round_number, outcome="completed",
                seconds=time.monotonic() - started, tool_count=len(resp.tool_calls),
                prompt_tokens=resp.prompt_tokens, completion_tokens=resp.completion_tokens,
            )

            # no tool calls -> LLM is done, return text
            if not resp.tool_calls:
                self.messages.append(resp.message)
                self.last_stop_reason = "completed"
                return resp.content

            # tool calls -> execute (parallel when multiple, like Claude Code's
            # StreamingToolExecutor which runs independent tools concurrently)
            self.messages.append(resp.message)

            try:
                if len(resp.tool_calls) == 1:
                    tc = resp.tool_calls[0]
                    result = self._run_tool(tc, on_tool)
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    })
                else:
                    # parallel execution for multiple tool calls
                    results = self._exec_tools_parallel(resp.tool_calls, on_tool)
                    for tc, result in zip(resp.tool_calls, results):
                        self.messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result,
                        })
            except KeyboardInterrupt:
                # Ctrl+C mid-execution would leave the assistant tool_calls
                # message without replies, poisoning the next request; backfill
                self._answer_pending_tool_calls(resp.tool_calls)
                raise

            # compress if tool outputs are big
            self.context.maybe_compress(self.messages, self.llm)

        self.last_stop_reason = "round_limit"
        return "(reached maximum tool-call rounds)"

    def _run_tool(self, tc, on_tool=None) -> str:
        if on_tool:
            on_tool(tc.name, tc.arguments)
        self._trace_event("tool_started", tool=tc.name)
        started = time.monotonic()
        try:
            blocked = self._pre_hooks(tc)
            if blocked:
                result, outcome = blocked, "blocked_hook"
                self._trace_event("tool_decision", tool=tc.name, outcome=outcome, reason="hook_blocked")
            else:
                blocked = self._permit(tc)
                if blocked is not None:
                    result = blocked
                    outcome = "blocked_plan" if self.plan_mode and tc.name not in Permission.READ_ONLY else "denied"
                    reason = "plan_mode" if outcome == "blocked_plan" else getattr(self.permission, "last_reason", None)
                    self._trace_event("tool_decision", tool=tc.name, outcome=outcome, reason=reason)
                else:
                    self._trace_event(
                        "tool_decision", tool=tc.name, outcome="allowed",
                        reason=getattr(self.permission, "last_reason", None),
                    )
                    result = self._exec_tool(tc)
                    self._post_hooks(tc, result)
                    failed = result.startswith(("Error", "⚠ Blocked")) or (tc.name == "bash" and "[exit code:" in result)
                    outcome = "failed" if failed else "completed"
        except Exception as exc:
            self._trace_event(
                "tool_finished", tool=tc.name, outcome="error",
                error_type=type(exc).__name__, seconds=time.monotonic() - started,
            )
            raise
        self._trace_event("tool_finished", tool=tc.name, outcome=outcome, seconds=time.monotonic() - started)
        return result

    def _pre_hooks(self, tc) -> str | None:
        """PreToolUse hooks, fired before consent. A string return blocks the
        call and becomes the tool result the model sees; None lets it through."""
        if self.hooks is None:
            return None
        return self.hooks.run_pre(tc.name, tc.arguments)

    def _post_hooks(self, tc, result: str):
        """PostToolUse hooks observe a finished call; they can never block."""
        if self.hooks is not None:
            self.hooks.run_post(tc.name, tc.arguments, result)

    def _permit(self, tc) -> str | None:
        """Consent check for one call. None means go ahead; a string is the
        refusal, returned as the tool result instead of executing."""
        # plan mode outranks consent, even --yes: while it's on nothing mutates
        if self.plan_mode and tc.name not in Permission.READ_ONLY:
            return (
                "Plan mode is on, so this call was refused: plan mode is "
                "read-only. Do not retry it. Keep investigating with the "
                "read-only tools, then present the plan and stop. The user can "
                'approve it by typing "approve", or exit plan mode with /plan.'
            )
        if self.permission is None:
            return None
        return self.permission.check(tc.name, tc.arguments)

    def _exec_tool(self, tc) -> str:
        """Execute a single tool call, returning the result string."""
        tool = self._tool_by_name.get(tc.name)
        if tool is None:
            return f"Error: unknown tool '{tc.name}'"
        # validate arguments first so a TypeError raised *inside* the tool isn't
        # mislabelled as a bad-arguments error from the caller
        try:
            inspect.signature(tool.execute).bind(**tc.arguments)
        except TypeError as e:
            return f"Error: bad arguments for {tc.name}: {e}"
        # a tool that blows up gets reported back as text, never kills the loop
        try:
            return tool.execute(**tc.arguments)
        except Exception as e:  # noqa: BLE001
            return f"Error executing {tc.name}: {e}"

    def _exec_tools_parallel(self, tool_calls, on_tool=None) -> list[str]:
        """Run multiple tool calls concurrently using threads.

        This is inspired by Claude Code's StreamingToolExecutor which starts
        executing tools while the model is still generating.  We simplify to:
        when the model returns N tool calls at once, run them in parallel.
        """
        from .tools.bash import get_tracked_cwd, set_tracked_cwd

        if self.workspace is not None:
            results = []
            for tc in tool_calls:
                results.append(self._run_tool(tc, on_tool))
            return results

        for tc in tool_calls:
            if on_tool:
                on_tool(tc.name, tc.arguments)

        # hooks and consent are settled up front on this thread: prompting
        # from pool workers would interleave several prompts on one terminal
        results = [self._pre_hooks(tc) or self._permit(tc) for tc in tool_calls]
        # the tracked cwd is thread-local, so pool workers would otherwise
        # start from the launch directory: hand the session cwd in, and merge
        # any cd back in call order so the batch behaves like a sequence
        session_cwd = get_tracked_cwd()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            def run(i, tc, base_cwd):
                is_bash = tc.name == "bash"
                if is_bash and base_cwd is not None:
                    set_tracked_cwd(base_cwd)
                out = self._exec_tool(tc)
                if not is_bash:
                    return i, out, None
                cwd_after = get_tracked_cwd()
                # only a worker that actually moved the dir reports back; a
                # sibling that merely inherited the base must not revert a cd
                return i, out, cwd_after if cwd_after != base_cwd else None

            futures = {
                i: pool.submit(run, i, tc, session_cwd)
                for i, tc in enumerate(tool_calls)
                if results[i] is None
            }
            for i, future in futures.items():
                i, results[i], worker_cwd = future.result()
                if worker_cwd is not None:
                    session_cwd = worker_cwd
                    set_tracked_cwd(worker_cwd)
        for i in futures:
            self._post_hooks(tool_calls[i], results[i])
        return results

    def _answer_pending_tool_calls(self, tool_calls):
        """Backfill a tool reply for every call that didn't get one.

        OpenAI-compatible APIs reject a request where an assistant message has
        tool_calls without a matching tool reply for each id, so this keeps the
        history valid when execution is interrupted partway through.
        """
        answered = {m.get("tool_call_id") for m in self.messages if m.get("role") == "tool"}
        for tc in tool_calls:
            if tc.id not in answered:
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": "[interrupted]",
                })

    def reset(self):
        """Clear conversation history."""
        self.messages.clear()
        self._active_rules = None
        self._active_memories = None
        self.last_memories = MemorySnapshot()
        self.memory_error = None
        self.last_stop_reason = None
        # a user-visible reset must not leak the dead conversation's checklist
        # into the next system prompt
        todo = self._tool_by_name.get("todo_write")
        if isinstance(todo, TodoWriteTool):
            todo._tasks = []
