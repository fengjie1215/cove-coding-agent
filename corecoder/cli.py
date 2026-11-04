"""Interactive REPL - the user-facing terminal interface."""

import argparse
import os
import sqlite3
import sys

from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from . import __version__
from .agent import Agent
from .config import Config
from .hooks import load_hooks
from .llm import LLM, LiteLLM
from .mcp import load_mcp_tools
from .rules import project_id
from .permissions import DEFAULT_STORE, Permission
from .session import list_sessions, load_session, save_session
from .task_cli import TaskController
from .tools import ALL_TOOLS

console = Console()
err_console = Console(stderr=True)


def _parse_args():
    p = argparse.ArgumentParser(
        prog="corecoder",
        description="Minimal AI coding agent. Works with any OpenAI-compatible LLM.",
    )
    p.add_argument("-m", "--model", help="Model name (default: $CORECODER_MODEL or gpt-5.5)")
    p.add_argument("--base-url", help="API base URL (default: $OPENAI_BASE_URL)")
    p.add_argument("--api-key", help="API key (default: $OPENAI_API_KEY)")
    p.add_argument("-p", "--prompt", help="One-shot prompt (non-interactive mode)")
    p.add_argument("--yes", action="store_true", help="Auto-approve every tool call (for scripts and CI)")
    p.add_argument("--demo", action="store_true", help="Run the offline scripted demo (no API key needed)")
    p.add_argument("-r", "--resume", metavar="ID", help="Resume a saved session")
    p.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")
    return p.parse_args()


def main():
    args = _parse_args()

    if args.demo:
        from .demo import run_demo
        raise SystemExit(run_demo())

    config = Config.from_env()

    # CLI args override env vars
    if args.model:
        config.model = args.model
    if args.base_url:
        config.base_url = args.base_url
    if args.api_key:
        config.api_key = args.api_key

    if not config.api_key:
        console.print("[red bold]No API key found.[/]")
        console.print(
            "Set one of: OPENAI_API_KEY, DEEPSEEK_API_KEY, or CORECODER_API_KEY\n"
            "\nExamples:\n"
            "  # OpenAI\n"
            "  export OPENAI_API_KEY=sk-...\n"
            "\n"
            "  # DeepSeek\n"
            "  export OPENAI_API_KEY=sk-... OPENAI_BASE_URL=https://api.deepseek.com\n"
            "\n"
            "  # Ollama (local)\n"
            "  export OPENAI_API_KEY=ollama OPENAI_BASE_URL=http://localhost:11434/v1 CORECODER_MODEL=qwen2.5-coder\n"
        )
        sys.exit(1)

    llm_cls = LiteLLM if config.provider == "litellm" else LLM
    llm = llm_cls(
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
    )
    # consent layer: ask in the REPL, refuse in one-shot mode, --yes skips it
    if args.yes:
        permission = Permission(allow_all=True)
    elif args.prompt:
        permission = Permission()
    else:
        permission = Permission(ask=_ask_permission, persist_path=DEFAULT_STORE)
    agent = Agent(
        llm=llm,
        tools=[*ALL_TOOLS, *load_mcp_tools()],
        max_context_tokens=config.max_context_tokens,
        permission=permission,
        hooks=load_hooks(),
    )

    # resume saved session
    if args.resume:
        loaded = load_session(args.resume)
        if loaded:
            agent.messages, loaded_model = loaded
            # restore the model from the saved session unless overridden by CLI
            if not args.model:
                agent.llm.model = loaded_model
                config.model = loaded_model
            console.print(f"[green]Resumed session: {args.resume} (model: {agent.llm.model})[/green]")
        else:
            console.print(f"[red]Session '{args.resume}' not found.[/red]")
            sys.exit(1)

    # one-shot mode
    if args.prompt:
        _run_once(agent, args.prompt)
        return

    # interactive REPL
    _repl(agent, config)


def _ask_permission(tool_name: str, arguments: dict) -> str:
    """REPL consent prompt. Anything but a clear yes counts as a no."""
    console.print(f"\n[bold yellow]permission requested:[/] [cyan]{tool_name}[/cyan]({_brief(arguments)})")
    try:
        answer = pt_prompt("  [y] allow once  [a] always allow this tool  [n] deny: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        console.print("[dim]denied[/dim]")
        return "deny"
    if answer in ("y", "yes"):
        return "once"
    if answer in ("a", "always"):
        return "always"
    return "deny"


def _run_once(agent: Agent, prompt: str):
    """Non-interactive: run one prompt and exit."""
    perm = agent.permission
    if perm is not None and perm.ask is None and not perm.allow_all:
        console.print("[dim]one-shot mode: mutating tools are refused unless you pass --yes[/dim]")

    def on_token(tok):
        print(tok, end="", flush=True)

    def on_tool(name, kwargs):
        console.print(f"\n[dim]> {name}({_brief(kwargs)})[/dim]")

    def on_reasoning(tok):
        # thinking goes to stderr so piped stdout stays the plain answer
        err_console.print(f"[dim]{tok}[/dim]", end="")

    try:
        agent.chat(prompt, on_token=on_token, on_tool=on_tool, on_reasoning=on_reasoning)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        sys.exit(130)
    except Exception as e:  # noqa: BLE001
        # one-shot mode: print whatever went wrong and exit non-zero
        console.print(f"\n[red]Error: {e}[/red]")
        sys.exit(1)
    if agent.memory_error:
        err_console.print(f"[yellow]Memory retrieval skipped: {agent.memory_error}[/yellow]")
    print()


def _repl(agent: Agent, config: Config):
    """Interactive read-eval-print loop."""
    perm = agent.permission
    mode = "auto-approve every tool call (--yes)" if (perm and perm.allow_all) else "ask before mutating tools"
    mcp_count = sum(1 for t in agent.tools if t.name.startswith("mcp__"))
    console.print(Panel(
        f"[bold]CoreCoder[/bold] v{__version__}\n"
        f"Model: [cyan]{config.model}[/cyan]"
        + (f"  Base: [dim]{config.base_url}[/dim]" if config.base_url else "")
        + f"\nPermissions: [cyan]{mode}[/cyan]"
        + (f"\nHooks: [cyan]{len(agent.hooks.pre)} pre, {len(agent.hooks.post)} post[/cyan]"
           " from ~/.corecoder/hooks.json" if agent.hooks else "")
        + (f"\nMCP: [cyan]{mcp_count} tools[/cyan] from ~/.corecoder/mcp.json" if mcp_count else "")
        + "\nType [bold]/help[/bold] for commands, [bold]Ctrl+C[/bold] to cancel, [bold]quit[/bold] to exit.",
        border_style="blue",
    ))

    hist_path = os.path.expanduser("~/.corecoder_history")
    history = FileHistory(hist_path)

    # Enter submits, Escape+Enter inserts a newline (for pasting code blocks etc.)
    kb = KeyBindings()
    tasks = TaskController(agent, console, _ask_permission, pt_prompt)

    @kb.add("enter")
    def _submit(event):
        event.current_buffer.validate_and_handle()

    @kb.add("escape", "enter")
    def _newline(event):
        event.current_buffer.insert_text("\n")

    while True:
        current = tasks.agent or agent
        try:
            user_input = pt_prompt(
                f"You (task {tasks.task.id}) > " if tasks.task else ("You (plan) > " if current.plan_mode else "You > "),
                history=history,
                multiline=True,
                key_bindings=kb,
                prompt_continuation="...  ",
            ).strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\nBye!")
            break

        if not user_input:
            continue

        # built-in commands
        if user_input.lower() in ("quit", "exit", "/quit", "/exit"):
            break
        if user_input == "/help":
            _show_help()
            continue
        if user_input in ("/rules", "/rules show"):
            _show_rules(current)
            continue
        if user_input.startswith("/rules "):
            console.print("[yellow]Usage: /rules show[/yellow]")
            continue
        if user_input == "/memory" or user_input.startswith("/memory "):
            try:
                _memory_command(current, user_input[7:].strip(), pt_prompt)
            except (ValueError, OSError, sqlite3.Error) as e:
                console.print(f"[red]Memory error: {e}[/red]")
            continue
        if user_input == "/task" or user_input.startswith("/task "):
            try:
                task_prompt = tasks.command(user_input)
                if task_prompt:
                    before = tasks.manager.digest(tasks.task)
                    _display_chat(tasks.agent, task_prompt)
                    tasks.after_chat(before, _display_chat)
            except KeyboardInterrupt:
                if tasks.task:
                    tasks.task.messages = tasks.agent.messages
                    tasks.task.save(tasks.manager.tasks_dir)
                console.print("\n[yellow]Interrupted.[/yellow]")
            except Exception as e:  # noqa: BLE001
                if tasks.task:
                    tasks.task.messages = tasks.agent.messages
                    tasks.task.save(tasks.manager.tasks_dir)
                console.print(f"[red]Task error: {e}[/red]")
            continue
        if user_input == "/reset":
            current.reset()
            if tasks.task:
                tasks.task.messages = current.messages
                tasks.task.save(tasks.manager.tasks_dir)
            console.print("[yellow]Conversation reset.[/yellow]")
            continue
        if user_input == "/plan":
            current.plan_mode = not current.plan_mode
            if current.plan_mode:
                console.print(
                    "[yellow]Plan mode on.[/yellow] The agent can look but not touch: it will "
                    "investigate read-only and present a plan. Type [bold]approve[/bold] to "
                    "accept the plan, or [bold]/plan[/bold] again to exit."
                )
            else:
                console.print("[yellow]Plan mode off.[/yellow]")
            continue
        if current.plan_mode and user_input.lower() in ("approve", "/approve"):
            current.plan_mode = False
            console.print("[yellow]Plan mode off.[/yellow]")
            user_input = "approve"  # the approval itself goes to the model, which then executes
        if user_input == "/tokens":
            p = agent.llm.total_prompt_tokens
            c = agent.llm.total_completion_tokens
            line = f"Tokens: [cyan]{p}[/cyan] prompt + [cyan]{c}[/cyan] completion = [bold]{p+c}[/bold] total"
            cost = agent.llm.estimated_cost
            if cost is not None:
                line += f"  (~${cost:.4f})"
            console.print(line)
            continue
        if user_input == "/model" or user_input.startswith("/model "):
            new_model = user_input[7:].strip() if user_input.startswith("/model ") else ""
            if new_model:
                agent.llm.model = new_model
                config.model = new_model
                console.print(f"Switched to [cyan]{new_model}[/cyan]")
            else:
                console.print(f"Current model: [cyan]{config.model}[/cyan]")
            continue
        if user_input == "/compact":
            from .context import estimate_tokens
            before = estimate_tokens(current.messages)
            compressed = current.context.maybe_compress(current.messages, current.llm)
            after = estimate_tokens(current.messages)
            if compressed:
                console.print(f"[green]Compressed: {before} → {after} tokens ({len(current.messages)} messages)[/green]")
            else:
                console.print(f"[dim]Nothing to compress ({before} tokens, {len(current.messages)} messages)[/dim]")
            continue
        if user_input == "/save":
            if tasks.task:
                tasks.task.messages = current.messages
                tasks.task.save(tasks.manager.tasks_dir)
                console.print(f"[green]Task {tasks.task.id} saved; resume with /task resume {tasks.task.id}[/green]")
                continue
            sid = save_session(current.messages, config.model)
            console.print(f"[green]Session saved: {sid}[/green]")
            console.print(f"Resume with: corecoder -r {sid}")
            continue
        if user_input == "/diff":
            if tasks.task:
                tasks.command("/task diff")
                continue
            from .tools.edit import _changed_files
            if not _changed_files:
                console.print("[dim]No files modified this session.[/dim]")
            else:
                console.print(f"[bold]Files modified this session ({len(_changed_files)}):[/bold]")
                for f in sorted(_changed_files):
                    console.print(f"  [cyan]{f}[/cyan]")
            continue
        if user_input == "/undo":
            if tasks.task:
                console.print(current.workspace.checkpoints.undo())
                tasks.task.verify_status = "stale"
                tasks.task.save(tasks.manager.tasks_dir)
                continue
            from .checkpoints import pending, undo
            console.print(undo())
            left = pending()
            if left:
                console.print(f"[dim]{left} more checkpoint(s) on the stack.[/dim]")
            continue
        if user_input == "/sessions":
            sessions = list_sessions()
            if not sessions:
                console.print("[dim]No saved sessions.[/dim]")
            else:
                for s in sessions:
                    console.print(f"  [cyan]{s['id']}[/cyan] ({s['model']}, {s['saved_at']}) {s['preview']}")
            continue
        if user_input == "/mcp" or user_input.startswith("/mcp "):
            _mcp(user_input[4:].strip())
            continue

        # an unknown /command shouldn't be sent to the model as a prompt
        if user_input.startswith("/"):
            console.print(f"[yellow]Unknown command: {user_input.split()[0]} (try /help)[/yellow]")
            continue

        # call the agent
        streamed: list[str] = []

        def on_token(tok, streamed=streamed):
            streamed.append(tok)
            print(tok, end="", flush=True)

        def on_tool(name, kwargs):
            console.print(f"\n[dim]> {name}({_brief(kwargs)})[/dim]")

        def on_reasoning(tok, streamed=streamed):
            # dim the thinking inline; mark the stream non-empty so the final
            # newline logic below still fires even if the reply itself is empty
            streamed.append(tok)
            console.print(f"[dim]{tok}[/dim]", end="")

        try:
            before = tasks.manager.digest(tasks.task) if tasks.task else None
            response = current.chat(user_input, on_token=on_token, on_tool=on_tool,
                                    on_reasoning=on_reasoning)
            if current.memory_error:
                console.print(f"[yellow]Memory retrieval skipped: {current.memory_error}[/yellow]")
            if streamed:
                print()  # newline after streamed tokens
            else:
                # response wasn't streamed (came after tool calls)
                console.print(Markdown(response))
            if tasks.task:
                tasks.after_chat(before, _display_chat)
        except KeyboardInterrupt:
            if tasks.task:
                tasks.task.messages = current.messages
                tasks.task.save(tasks.manager.tasks_dir)
            console.print("\n[yellow]Interrupted.[/yellow]")
        except Exception as e:  # noqa: BLE001
            if tasks.task:
                tasks.task.messages = current.messages
                tasks.task.save(tasks.manager.tasks_dir)
            # keep the REPL alive no matter what chat() throws
            console.print(f"\n[red]Error: {e}[/red]")


def _display_chat(agent: Agent, message: str):
    """Display a task-start or automatic-repair turn."""
    streamed = []

    def on_token(tok):
        streamed.append(tok)
        print(tok, end="", flush=True)

    def on_tool(name, kwargs):
        console.print(f"\n[dim]> {name}({_brief(kwargs)})[/dim]")

    result = agent.chat(message, on_token=on_token, on_tool=on_tool)
    if agent.memory_error:
        console.print(f"[yellow]Memory retrieval skipped: {agent.memory_error}[/yellow]")
    if streamed:
        print()
    else:
        console.print(Markdown(result))


def _mcp(arg: str):
    """The /mcp command: server states, or `reconnect <name>` to revive one."""
    from . import mcp
    parts = arg.split()
    if not parts:
        if not mcp._live_clients:
            console.print("[dim]No MCP servers running (configure ~/.corecoder/mcp.json).[/dim]")
        for client in mcp._live_clients:
            if client.error is None:
                console.print(f"  [cyan]{client.name}[/cyan]: alive, {len(client.tools)} tools")
            else:
                console.print(
                    f"  [cyan]{client.name}[/cyan]: [red]dead[/red] ({client.error})"
                    f", revive with /mcp reconnect {client.name}"
                )
        return
    if len(parts) == 2 and parts[0] == "reconnect":
        client = next((c for c in mcp._live_clients if c.name == parts[1]), None)
        if client is None:
            console.print(f"[red]No MCP server named '{parts[1]}'.[/red]")
            return
        try:
            client.reconnect()
        except (mcp.MCPError, OSError) as e:
            console.print(f"[red]Reconnect failed:[/red] {e}")
        else:
            console.print(f"[green]Reconnected:[/green] [cyan]{client.name}[/cyan], {len(client.tools)} tools")
        return
    console.print("[yellow]Usage: /mcp [reconnect <name>][/yellow]")


def _show_help():
    console.print(Panel(
        "[bold]Commands:[/bold]\n"
        "  /help          Show this help\n"
        "  /reset         Clear conversation history\n"
        "  /model         Show current model\n"
        "  /model <name>  Switch model mid-conversation\n"
        "  /tokens        Show token usage\n"
        "  /compact       Compress conversation context\n"
        "  /rules show    Show active rules, sources, and precedence\n"
        "  /memory add [--global] <text>  Save a manual memory (project by default)\n"
        "  /memory list [--project|--global]|show <id>|search <query>|why [id]|forget <id>\n"
        "  /memory candidates|accept <id> [edited text]|reject <id>|disable <id>\n"
        "  /diff          Show files modified this session\n"
        "  /task start <requirement>  Start an isolated coding task\n"
        "  /task status|list|resume <id>|trace [id]|diff|verify|apply [--force]|learn <id>|leave|discard <id>\n"
        "  /undo          Revert the most recent file change\n"
        "  /plan          Toggle plan mode: read-only, then a plan to approve\n"
        "  /save          Save session to disk\n"
        "  /sessions      List saved sessions\n"
        "  /mcp           MCP server status; /mcp reconnect <name> revives a dead one\n"
        "  quit           Exit CoreCoder\n"
        "\n"
        "[bold]Input:[/bold]\n"
        "  Enter          Submit message\n"
        "  Esc+Enter      Insert newline (for pasting code)",
        title="CoreCoder Help",
        border_style="dim",
    ))


def _show_rules(agent: Agent):
    console.print("Rules: current request > project shared > personal project > global")
    console.print("Built-in safety and tool permissions always apply.")
    for source in reversed(agent.rules.load().sources):
        status = source.error or ("active" if source.content else "empty or missing")
        console.print(f"{source.scope}: {source.path} [{status}]", markup=False)
        if source.content:
            console.print(source.content, markup=False)


def _memory_command(agent: Agent, arg: str, prompt):
    """Manage approved memories and task candidates; model tools cannot write this store."""
    action, _, value = arg.partition(" ")
    value = value.strip()
    store = agent.memory_store
    identity = project_id(agent.rules.identity_root)

    def confirmed(question: str) -> bool:
        try:
            return prompt(question).strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False

    if agent.plan_mode and action in ("add", "forget", "accept", "reject", "disable"):
        raise ValueError("Memory changes are disabled in plan mode.")

    if action == "add":
        scope = "project"
        if value.startswith("--global "):
            scope, value = "global", value[len("--global "):].strip()
        elif value.startswith("--project "):
            value = value[len("--project "):].strip()
        if not value or value.startswith("--"):
            raise ValueError("Usage: /memory add [--global] <text>")
        if scope == "global" and not confirmed("Save this memory for every project? [y/N] "):
            console.print("[dim]Cancelled.[/dim]")
            return
        entry = store.add(value, scope, identity)
        console.print(f"[green]Saved {entry.scope} memory {entry.id}.[/green]")
        return

    if action == "list" and value in ("", "--project", "--global"):
        scope = value.removeprefix("--") if value else None
        entries = store.list_entries(identity, scope)
        if not entries:
            console.print("[dim]No memories in this scope.[/dim]")
        for entry in entries:
            preview = entry.content.replace("\n", " ")[:100]
            console.print(f"{entry.id}  {entry.scope:7}  {entry.status:8}  {preview}", markup=False)
        return

    if action == "show" and value:
        entry = store.get(value, identity)
        if entry is None:
            console.print("[yellow]Memory not found in this project.[/yellow]")
            return
        console.print(f"{entry.id} ({entry.scope}, {entry.status}, created {entry.created_at})", markup=False)
        console.print(entry.content, markup=False)
        return

    if action == "search" and value:
        hits = store.search(value, identity, limit=10).hits
        if not hits:
            console.print("[dim]No matching memories.[/dim]")
        for hit in hits:
            preview = hit.entry.content.replace("\n", " ")[:100]
            console.print(f"{hit.entry.id}  {hit.entry.scope:7}  score={hit.score:.2f}  {preview}", markup=False)
        return

    if action == "why" and not value:
        if agent.memory_error:
            console.print(f"[yellow]Last retrieval failed: {agent.memory_error}[/yellow]")
        elif not agent.last_memories.hits:
            console.print("[dim]No memory was injected in the last turn.[/dim]")
        else:
            console.print("Memories injected in the last turn:")
            for hit in agent.last_memories.hits:
                console.print(f"{hit.entry.id}  {hit.entry.scope}  score={hit.score:.2f}", markup=False)
        return

    if action == "why" and value:
        entry = store.get(value, identity)
        if entry is None:
            console.print("[yellow]Memory not found in this project.[/yellow]")
            return
        console.print(f"{entry.id}: {entry.origin}, {entry.status}, {entry.scope}", markup=False)
        if entry.origin == "task":
            console.print(
                f"Task: {entry.source_task_id}\nVerified diff: {entry.source_digest}\n"
                f"Evidence: {entry.evidence}", markup=False,
            )
        else:
            console.print("Source: manual /memory add", markup=False)
        return

    if action == "candidates" and not value:
        candidates = store.list_candidates(identity)
        if not candidates:
            console.print("[dim]No pending memory candidates.[/dim]")
        for candidate in candidates:
            console.print(
                f"{candidate.id}  task {candidate.task_id}  {candidate.content}\n"
                f"  Evidence: {candidate.evidence}", markup=False,
            )
        return

    if action == "accept" and value:
        candidate_id, _, edited = value.partition(" ")
        candidate = store.get_candidate(candidate_id, identity)
        if candidate is None or candidate.status != "pending":
            console.print("[yellow]Pending candidate not found in this project.[/yellow]")
            return
        content = edited.strip() or candidate.content
        console.print(f"Accept project memory: {content}\nEvidence: {candidate.evidence}", markup=False)
        if not confirmed("Save this candidate as a project memory? [y/N] "):
            console.print("[dim]Cancelled.[/dim]")
            return
        entry = store.accept_candidate(candidate_id, identity, content)
        console.print(f"[green]Saved project memory {entry.id}.[/green]")
        return

    if action == "reject" and value:
        candidate = store.get_candidate(value, identity)
        if candidate is None or candidate.status != "pending":
            console.print("[yellow]Pending candidate not found in this project.[/yellow]")
            return
        console.print(f"Reject candidate: {candidate.content}", markup=False)
        if confirmed("Reject and erase this candidate text? [y/N] "):
            store.reject_candidate(value, identity)
            console.print("[green]Candidate rejected.[/green]")
        return

    if action == "disable" and value:
        entry = store.get(value, identity)
        if entry is None or entry.status != "active":
            console.print("[yellow]Active memory not found in this project.[/yellow]")
            return
        console.print(f"Disable {entry.scope} memory {entry.id}: {entry.content[:100]}", markup=False)
        if confirmed("Disable this memory without deleting its provenance? [y/N] "):
            store.disable(value, identity)
            console.print(f"[green]Disabled memory {value}.[/green]")
        return

    if action == "forget" and value:
        entry = store.get(value, identity)
        if entry is None:
            console.print("[yellow]Memory not found in this project.[/yellow]")
            return
        console.print(f"Delete {entry.scope} memory {entry.id}: {entry.content[:100]}", markup=False)
        if not confirmed("Delete this memory? [y/N] "):
            console.print("[dim]Cancelled.[/dim]")
            return
        store.forget(value, identity)
        console.print(f"[green]Deleted memory {value}.[/green]")
        return

    console.print(
        "[yellow]Usage: /memory add [--global] <text>|list [--project|--global]|"
        "show <id>|search <query>|why [id]|candidates|accept <id> [edited text]|"
        "reject <id>|disable <id>|forget <id>[/yellow]"
    )


def _brief(kwargs: dict, maxlen: int = 80) -> str:
    s = ", ".join(f"{k}={repr(v)[:40]}" for k, v in kwargs.items())
    return s[:maxlen] + ("..." if len(s) > maxlen else "")
