<div align="center">

# CoreCoder

**The nanoGPT of coding agents. A readable Python core with task-level worktree isolation and reviewable verification.**

*learn from it · fork it · ship something better*

[中文](README_CN.md) | English | [Source-reading series · 8 bilingual essays](article/00-index_EN.md)

[![PyPI](https://img.shields.io/pypi/v/corecoder)](https://pypi.org/project/corecoder/)
[![Python](https://img.shields.io/badge/python-3.10+-blue)](https://python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Tests](https://github.com/he-yufeng/CoreCoder/actions/workflows/ci.yml/badge.svg)](https://github.com/he-yufeng/CoreCoder/actions)
[![engine](https://img.shields.io/badge/engine-1503_LoC-blue)](article/00-index_EN.md)
[![essays](https://img.shields.io/badge/source--reading-8_bilingual-orange)](article/00-index_EN.md)

</div>

- **Readable end to end.** Read the whole engine in an afternoon, with no magic hidden anywhere you can't follow it.
- **Hackable.** Set a breakpoint on any line, change it, rerun, all on your own machine. It genuinely works, which makes this a living reference rather than a diagram.
- **The gaps are the point.** It deliberately keeps only the minimal core; what's missing isn't half-finished, it's where you branch off and make it your own.

## How it compares

| | CoreCoder | Claude Code | aider | nanoGPT |
|---|---|---|---|---|
| Lines of code | 1,503 engine / 5,108 total | hundreds of thousands (closed) | tens of thousands of Python | ~600 (two files) |
| Time to read it all | one afternoon | can't (closed) | a few days of slogging | one afternoon |
| Breakpoint, change, rerun? | yes, every line | no | yes, but there's a lot | yes |
| What it's for | understand one, then fork your own | production coding assistant | terminal pair-programming | minimal GPT for teaching |

The nanoGPT column is there as a reference point: minimal, readable, but it teaches you to train a GPT. CoreCoder is after the same thing, only the subject is an agent that actually edits code. Sitting it next to Claude Code and aider isn't about competing for their users. CoreCoder is the foundation you stand on while you learn from them and get going; it isn't in the same race.

## What this is

I've always felt coding agents get talked about as if they were arcane. Strip a tool like Claude Code or Cursor all the way down and the core is a `while` loop wrapped around a large model, plus seven or eight tools that let it actually do things. The hard part was never the loop; it's everything the loop has to cope with once it meets the real world. CoreCoder is the minimal version that writes that core out honestly.

The engine (loop, model interface, context, tools, sessions) is 1,503 lines once you drop blank lines and comments. With task isolation, validation, layered rules, confirmed learning, task traces and opt-in sandbox tasks, the package is 34 files: 5,108 physical lines, 4,363 net. The extra workflows are documented below.

And it really runs: reads and writes files, executes shell, spawns sub-agents, compacts context in three tiers, and tells you the tokens and dollars a run burned whenever you ask. Anything that would mutate your disk or run a command stops for your consent first. The point of it running is to keep the walkthrough grounded in working code.

The code came out of a public teardown: open analyses have already exposed a lot of the load-bearing architecture inside production agents like Claude Code. I took the most essential layer and rewrote it honestly, in as little code as I could. So reading CoreCoder is roughly like reading a runnable, annotated take on how that kind of agent works, except it's only a minimal reimplementation, sitting right there on your machine for you to take apart and change.

<p align="center">
  <img src="https://raw.githubusercontent.com/he-yufeng/CoreCoder/main/assets/demo-plan-hooks.gif" width="760"
       alt="Plan mode in action: the agent reads fib.py, gets its edit refused while plan mode is on, presents a plan, and only after approval edits, tests, and reports — with Pre/PostToolUse hooks firing around every call.">
</p>

<p align="center"><sub><i>These thousand lines really do run a full loop end to end: ask it to fix buggy.py and it reads the file, edits the code, runs it once to confirm, then reports back on its own. Watch it, then come back and read the code.</i></sub></p>

This README follows the same arc: the first half helps you **read it** (the code map, the main loop, eight essays), the second half helps you **fork it** and points at a few directions worth pushing further.

## Run it once first (five minutes before you read)

Before you read the source, get it running on your machine once to build some intuition. It's a foundation meant for forking, so the recommended path is to clone it and install editable, reading and changing as you go:

```bash
git clone https://github.com/he-yufeng/CoreCoder
cd CoreCoder
pip install -e .
```

If you just want to get it running first, `pip install corecoder` works too.

Give it a model and a key and it goes. It speaks the OpenAI-compatible API by default, and switching providers is usually just two environment variables:

| Provider | Example env vars |
|---|---|
| OpenAI (default `gpt-5.5`) | `OPENAI_API_KEY=sk-...` |
| DeepSeek | `OPENAI_API_KEY=sk-... OPENAI_BASE_URL=https://api.deepseek.com CORECODER_MODEL=deepseek-chat` |
| OmniRoute | `OPENAI_API_KEY=your-key OPENAI_BASE_URL=http://localhost:20128/v1 CORECODER_MODEL=auto` |
| Local Ollama | `OPENAI_API_KEY=ollama OPENAI_BASE_URL=http://localhost:11434/v1 CORECODER_MODEL=qwen2.5-coder` |

Kimi, Qwen and the like are the same two variables; for providers that don't even offer an OpenAI-compatible endpoint, the optional LiteLLM backend (`pip install "corecoder[litellm]"`) routes to a hundred-plus of them. The third essay goes into this in detail. Thinking models are first-class too: deepseek-reasoner, kimi-k3 and friends stream their chain-of-thought, and CoreCoder shows it dimmed as it works, kept out of the conversation history so providers never see it come back. The key can be `export`ed directly or dropped into a `.env` at the project root, which is loaded on startup. Then:

Smoke-tested end to end (read the file, edit it, run it, report back) against DeepSeek, Qwen3 and Kimi K2 via a single OpenRouter-compatible endpoint; each completed the full loop. One note for one-shot scripts: `-p` refuses mutating tools unless you pass `--yes`, by design.

```bash
corecoder                                             # interactive REPL
corecoder -p "add error handling to parse_config()"   # one-shot mode, exits when done
```

## Read it: the code map

Laid out flat, the whole project is this big. Skim it before you clone and you'll know where everything is. This is the most concrete difference from Claude Code's hundreds of thousands of lines: you can read it like the table of contents of a book. Start from the main loop in `agent.py`; that's the heart of the whole agent.

```
corecoder/
├── agent.py        agent loop + parallel tool exec       366 lines   ← start here
├── llm.py          streaming client + retry + cost        349 lines
├── context.py      three-tier context compaction          220 lines
├── session.py      save / resume + path-traversal guard    97 lines
├── permissions.py  consent for mutating tools              89 lines
├── hooks.py        Pre/PostToolUse shell hooks             91 lines
├── shell.py        POSIX shell routing (Git Bash on Windows) 61 lines
├── mcp.py          MCP stdio client for external tools    237 lines
├── prompt.py       system prompt                           49 lines
├── rules.py        layered user and project rules          96 lines
├── memory.py       approved memory + local retrieval      468 lines
├── learning.py     verified-task memory suggestions       133 lines
├── cli.py          REPL + slash commands + one-shot       657 lines
├── config.py       env-var config                          55 lines
├── checkpoints.py  /undo snapshot and restore               93 lines
├── sandbox.py      opt-in task Docker execution           141 lines
├── demo.py         offline end-to-end demo                 101 lines
└── tools/
    ├── bash.py       shell + dangerous-command gate + cd  221 lines
    ├── edit.py       unique-match search/replace + diff    99 lines
    ├── grep.py       content search                        93 lines
    ├── glob_tool.py  filename matching                     52 lines
    ├── read.py       file read                             56 lines
    ├── write.py      file write                            46 lines
    ├── todo.py       agent-maintained task checklist       79 lines
    ├── agent.py      sub-agent spawning                    79 lines
    └── base.py       tool base class                       32 lines
examples/
└── plan_hooks_demo.py  offline plan mode + hooks demo (no API key)
```

Eight tools: `bash`, `read_file`, `write_file`, `edit_file`, `glob`, `grep`, `todo_write` (a task checklist the agent maintains for itself), and `agent` (which spawns a sub-agent). Everything else is the CLI shell, config, and packaging wrapped around that engine core. If `~/.corecoder/mcp.json` exists, its MCP servers join the eight as extra `mcp__*` tools; the MCP section below covers it.

## A `while` loop is the whole agent

The whole of an agent fits in one sentence: hand the user's words to the model, run whatever tools it asks for, stuff the results back into the context, ask again, and keep going until it stops asking for tools and gives an answer. In code, that's about a dozen lines:

```python
# corecoder/agent.py · the main loop (trimmed skeleton)
def chat(self, user_input):
    self.messages.append(user_input)

    for _ in range(self.max_rounds):                   # bounded, so it can't run away
        reply = self.llm.chat(self.messages, self.tools)   # ask the model what to do next
        if not reply.tool_calls:                       # model wants no more tools
            return reply.text                          #   -> done, hand the answer back
        results = run_parallel(reply.tool_calls)       # tools requested -> run in parallel
        self.messages += results                       # feed results back, loop again

    return "(hit the round limit)"
```

That's the whole thing. The core skeleton is about twenty lines; counting parallel execution and the bookkeeping after a Ctrl+C interrupt, maybe forty. Almost everything else in CoreCoder's thousand-odd lines is there to clean up the mess the loop runs into once it meets the real world. `llm.py` ends up the biggest file in the engine, not because calling a model is hard, but because a streamed response splinters each tool call's arguments into fragments you have to restitch in order, a provider will hand you half a JSON object or a null `usage` field, and 429s, timeouts, dropped connections and 5xx all need backoff-and-retry while the other 4xx should just raise. A stream can also die after it has already started, so the retry wraps the whole request, not just the connect. That unglamorous grunt work, not the loop, is where the real engineering of taking an agent from demo to delivery actually lives; the third essay follows it down to the line.

Three decisions are worth a closer look, because they're the kind of call you can only make after you've understood how others did it, and they're judgments you can lift straight into your own fork.

**`edit_file` does search-and-replace on a unique match, not line numbers.** Line numbers are a trap: the model only has to miscount by one and it quietly edits the wrong place. Anchor on a unique snippet of the original instead. If there's no match, it hands the start of the file back so the model can re-anchor; if there are several matches, it makes the model bring more surrounding context rather than gamble on one. On a successful edit it returns a diff. Recoverable on failure, verifiable on success: the whole loop stays inside the tool.

**Context isn't cut all at once when it's full; it gives ground in three tiers, cheapest first.** At half full (50%) it trims over-long tool outputs in place, a tier that's purely mechanical and costs no model call. If 70% still isn't enough, it has the model summarize the older turns into a single paragraph while keeping the most recent ones verbatim. Only at 90% does it hit the emergency tier and pull everything, summary and recent turns alike, down to its tightest form. Blunt truncation tends to throw away exactly the early decision a long task leans on most; tiering lets it surrender the least important things first instead of lopping off the oldest decisions wholesale from the start.

**You constrain a sub-agent by withholding the tool, not by writing rules and hoping it obeys.** A spawned sub-agent gets an isolated context and its own separate history, with a toolset exactly one item shorter than the parent's: the `agent` tool itself, so it can't recursively spawn more sub-agents. Handing it one fewer tool is cleaner than legislating a rule after the fact. It also reuses the parent's model connection (its spend folded into the same running total), truncates its output once it runs past 5,000 characters down to just the opening, and runs on a shorter round limit than the parent. The same restraint, end to end.

Every one of these *whys* is traced down to the actual lines of code in the series below.

## The source-reading series · 8 bilingual essays

I also wrote a bilingual source-reading series, one intro plus eight parts, each in Chinese with an English mirror. Against CoreCoder's actual code, it walks through how agents like Claude Code work under the hood. One hard rule I set myself: every line count and every snippet is re-read and re-checked from the repo, never written from memory. The first six get you reading, the seventh gets you forking, and the eighth is about extending it without touching the loop; read them in any order.

- **[Intro · Read Claude Code through CoreCoder, then build your own](article/00-index_EN.md)**
- **[01 · An agent, at its core, is a `while` loop](article/01-the-loop_EN.md)** — the main loop in `agent.py`, interrupts, and the round limit
- **[02 · The tool system: letting the model act, safely](article/02-tools_EN.md)** — the eight tools in `tools/` and the bash safety gate
- **[03 · Plug in any LLM, and keep the bill honest](article/03-llm-and-cost_EN.md)** — `llm.py`'s provider wrapper, retries, and cost accounting
- **[04 · Surviving a long task on a finite window](article/04-context_EN.md)** — `context.py`'s three-tier compaction and orphaned tool messages
- **[05 · Parallel execution and sub-agents](article/05-parallel-and-subagents_EN.md)** — thread-pool concurrency and sub-agent isolation
- **[06 · Turning it into a real command-line tool](article/06-session-and-cli_EN.md)** — `session.py` and path-traversal defense
- **[07 · Fork CoreCoder into your own coding agent](article/07-build-your-own_EN.md)** — from fork to custom tools to swapping models
- **[08 · Three ways to extend without touching the loop: MCP, hooks, and plan mode](article/08-extensibility_EN.md)** — the v0.6.0 extensibility trio and the contract that makes them safe

## Fork it, build something better

Once you understand it, the natural next step is to fork. Getting started doesn't take much:

- **Swap in a model you actually use.** It's the two env vars from above; `llm.py` (349 lines) is the entry point for all provider adaptation.
- **Add a tool of your own.** Write a new file against the tool base class in `tools/base.py` (32 lines): run tests, fetch a page, call an LSP, whatever. The end of the second essay walks you through your first one by hand.
- **Rewrite the system prompt.** `prompt.py` is all of 49 lines; change one line and you'll watch the agent's temperament shift. It's the cheapest "change one thing, see a result" in the whole project.
- **Import it as a library.** The top level exports `Agent`, `LLM`, and `Config`, ready to embed in your own program:

```python
from corecoder import Agent, LLM

llm = LLM(model="deepseek-chat", api_key="sk-...", base_url="https://api.deepseek.com")
print(Agent(llm=llm).chat("find every TODO comment in this project and list them"))
```

Going deeper, several directions remain open. The optional task container improves execution isolation, but it does not make the agent a hardened security product. Each item below is an entry point for a stronger tool of your own:

- **The dangerous-command blocking in bash is just a regex blacklist.** It guards against slips, not adversarial commands. The opt-in Docker mode narrows exposure, but stronger isolation, policy enforcement, and security evaluation are still open work.
- **Retry is only exponential backoff.** No fallback model, no hard dollar budget. Follow `llm.py` down and add a fallback model chain plus a stop-on-over-budget gate; the change stays mostly inside that one file.
- **Sub-agents only run the plainest synchronous execution.** Make it async or a streaming executor and you close the exact gap the fifth essay identifies between this and how production agents stream execution.
- **No RAG, and the MCP client speaks tools only.** Retrieval-based code location for big repos is still open, and `mcp.py` leaves resources and prompts unimplemented on purpose. Either one is a real way to grow from a minimal core into your own stronger agent.

The README only points; the seventh essay picks up the code details for each. Pick one and start; that's the whole reason the core is kept this small.

## Commands

Inside the REPL, `/help` lists everything; these are the ones you'll reach for:

```
/model <name>    switch model
/compact         compact the context by hand
/tokens          token usage and cost estimate
/diff            files changed this session
/rules show      show active rules and their source files
/memory add [--global] <text>  save a project or global memory
/memory list [--project|--global]|show <id>|search <query>|why [id]|forget <id>
/memory candidates|accept <id> [edited text]|reject <id>|disable <id>
/task start [--sandbox] <request>  start an isolated coding task
/task status|list|resume <id>|trace [id]|diff|verify|apply [--force]|learn <id>|leave|discard <id>
/undo            revert the most recent file change
/plan            toggle plan mode (read-only, then a plan to approve)
/save  /sessions save / list sessions
/mcp             MCP server status; /mcp reconnect <name> revives a dead one
quit / exit      exit (Ctrl+C cancels the current round)
```

## Isolated coding tasks

In interactive mode, `/task start <request>` requires a Git repository with no tracked changes and creates a detached worktree under `~/.corecoder/worktrees`. Untracked and ignored files, including `.env`, are not copied. Task conversations, permissions, undo checkpoints and validation state live separately under `~/.corecoder/tasks`; use `/task leave` and `/task resume <id>` across restarts. `/task diff` (or `/diff` while in a task) shows the complete Git patch, including added, deleted and binary files.

After a changed turn, CoreCoder proposes validation commands from `.corecoder/verify.json` or detects Python, Node, Maven or Gradle entry points. It shows the exact argument arrays and asks before execution. A config file can contain `{"commands":[{"name":"tests","argv":["python","-m","pytest"],"timeout":180}]}`. Commands run without model API keys; failures can trigger at most two agent repair rounds. Missing tools, timeouts and skipped checks stop the repair loop. Changing the diff or validation scripts makes the prior result stale.

`/task trace` shows the current task's execution trace; `/task trace <id>` works after leaving or restarting. JSONL events under `~/.corecoder/tasks/<id>.trace.jsonl` capture model rounds and token counts, tool categories, approval decisions, durations, verification and repair outcomes, and patch digests. They **do not capture** prompts, tool arguments or outputs, shell commands, or verification logs. The trace helps locate a failure stage but cannot replay a task or replace diff review.

`/task apply` shows the diff and validation state, then asks before applying it as **uncommitted** changes to the original repository. It refuses changed original HEAD, dirty tracked files and patch conflicts. Failed, skipped or stale checks also block normal apply; `/task apply --force` bypasses only that validation gate, not conflict checks. `/task discard <id>` confirms before removing the created worktree and its unapplied changes.

Worktrees isolate Git changes, **not execution**. Shell commands and external MCP tools can still affect files and services outside the worktree.

For an opt-in stricter task, configure `CORECODER_SANDBOX_IMAGE` to a **trusted, already-local Linux image**, then use `/task start --sandbox <request>`. CoreCoder checks for a local Docker engine and image before creating the worktree and never pulls automatically. In this mode, each Shell call requires fresh approval; Shell and automatic verification run in a container with only the task worktree mounted, no network, a read-only container root, a temporary `/tmp`, a non-root user, and CPU/memory/process limits. Task MCP tools and host hooks are disabled. An unavailable Docker engine or image fails closed—there is no host fallback. Resume checks Docker again. The model API call itself stays on the host; the container is not given the host environment or model API keys. Only the command's output and exit status return to the agent.

The image must contain the tools your project needs. Verification uses Linux commands (including POSIX Maven/Gradle wrappers); a Windows `.bat` command or host-absolute executable path will not work in this mode. Each Shell call starts in the task directory, and `git` inside the container may not resolve the Git Worktree's host `.git` pointer. Worktree files and image-baked environment variables remain visible to code inside the container. Docker reduces exposure but is not a guarantee against malicious images, daemon compromise, or every side effect. See [sandbox acceptance design](evals/任务级安全执行验收设计.md).

## Layered rules

CoreCoder loads optional plain-text Markdown rules from three places, in increasing priority: `~/.corecoder/rules.md` (global preferences), `~/.corecoder/projects/<project-id>/rules.md` (private preferences for this project), and the repository-root `AGENTS.md` (shared project rules). Run `/rules show` to see the exact paths, contents and any loading errors; it works inside a task too. The private project ID is derived from the original repository path, so all its task worktrees use the same private rules. The shared `AGENTS.md` is read from the task worktree, not the original checkout; commit it to Git if tasks should use it.

Rules reload at the start of each user turn and stay fixed through that turn's tool calls. Each file must be UTF-8, a regular non-symlink file, and at most 8 KiB. A current explicit user request takes precedence over these rules; more specific rules take precedence over broader ones. Rules are advisory prompt context, **not security policy**: they cannot grant tool permissions, disable plan mode or bypass the task validation and apply checks. Rule text is not saved into conversation history.

## Memory, retrieval and confirmed learning

Use `/memory add <note>` to save a fact or decision for the current project, or `/memory add --global <note>` for all projects (with confirmation). `/memory list`, `/memory show <id>`, and `/memory search <query>` inspect the local store; `/memory forget <id>` confirms before deleting a note. `/memory why` shows which notes were selected for the last turn; `/memory why <id>` shows a note's source. Keep behavior instructions in layered rules and facts in memory; do not store API keys or other secrets.

After a task passes validation and `/task apply` succeeds normally, CoreCoder asks whether to spend one model call proposing up to three project facts. Declining does not call the model; `/task learn <id>` can propose later. Forced apply, stale validation and discarded tasks cannot generate suggestions. The model sees a bounded, filtered diff and brief task context, not raw shell output or the full chat. Filtering is best-effort, so review suggestions before accepting them. `/memory candidates` lists pending proposals; `/memory accept <id> [edited text]` confirms one as a project memory, while `/memory reject <id>` erases a rejected proposal. No proposal enters retrieval without acceptance. `/memory disable <id>` keeps provenance but stops retrieval for a stale note. Suggestions that prescribe future behavior belong in rules, not memories.

Notes live in `~/.corecoder/memory.sqlite3`, outside Git worktrees and saved chats. At each turn, CoreCoder searches global and current-project notes locally and injects up to three relevant results within a fixed context budget. The same original-project identity is used inside a task worktree, and a sub-agent receives the parent turn's snapshot. Retrieval also works in one-shot mode, but writing memories requires an explicit interactive command. Memory text is advisory, never a tool permission or a substitute for the current request and rules; a model may still misinterpret it, so inspect `/memory why` when an answer is surprising.

## Local evaluations

[`evals/`](evals/README.md) contains 12 small coding tasks, out-of-worktree hidden checks, runner/report scripts, and the test plan, method, expectations, and results. Mock mode checks the harness; live model runs should be performed in a disposable container or VM. Mock pass rates do not measure agent quality.

Session IDs are sanitized to safe characters before they become filenames, every archive lands under `~/.corecoder/sessions`, and a malicious session name can't traverse out. Undo history persists the same way: checkpoints land in `~/.corecoder/checkpoints.json`, so `/undo` still reaches back after a restart. The cost estimate behind `/tokens` reads a built-in price table, and `~/.corecoder/pricing.json` overrides any entry in it, so a new model or a moved price lands without waiting for a release.

## Permissions

Read-only tools (`read_file`, `glob`, `grep`, `todo_write`) run the moment the model asks. The mutating ones (`edit_file`, `write_file`, `bash`, and spawning a sub-agent) stop for consent first, and the REPL banner shows which mode you're in:

- In the REPL you get one prompt per call: allow once, always allow this tool, or deny. "Always" is remembered per tool and persists in `~/.corecoder/permissions.json` across restarts; a sub-agent inherits the same layer, so consent follows the work wherever it happens.
- In one-shot mode (`-p`) there is nobody to ask, so a mutating call is refused on the spot and the refusal goes back to the model as an ordinary tool result: the loop never hangs on input that can't arrive. Pass `--yes` to approve everything up front (scripts, CI).
- The decision itself is pure logic in `permissions.py`, with the terminal only supplying the prompt callback. You can unit-test consent without a TTY, or reuse the layer in your own embedding.

## Plan mode

`/plan` toggles plan mode in the REPL. While it's on, the prompt shows `(plan)` and every mutating call (writes, edits, bash, MCP tools, sub-agents) is refused on the spot: the refusal goes back to the model as an ordinary tool result, telling it to keep investigating read-only and present a numbered plan instead. When the plan looks right, `approve` (or `/plan` again) hands control back and the agent executes. Mechanically it is one flag on the `Agent` plus one refusal branch ahead of the consent gate, which itself stays untouched; there is no plan file and nothing is remembered between sessions.

## Hooks

Drop a `hooks.json` under `~/.corecoder` and your own shell commands run around every tool call, the same idea as Claude Code's hooks:

```json
{
  "PreToolUse":  [{"matcher": "bash", "command": "cat >> ~/.corecoder/audit.jsonl"}],
  "PostToolUse": [{"matcher": "*",    "command": "cat >> ~/.corecoder/trace.jsonl"}]
}
```

Each hook gets the call as JSON on stdin (`tool_name`, `tool_input`; post hooks also get `tool_response`). The matcher is an exact tool name; empty or `*` fires on every tool. A pre hook can veto the call with exit code 2, and its stderr travels back to the model as the reason so it can route around the block. Post hooks only observe and can never block. A hook that errors or runs past ten seconds is skipped with a warning: hooks assist the loop, they never get to kill it. The whole mechanism is `hooks.py`, and the REPL banner shows how many hooks loaded.

Two worth stealing (the commands lean on `jq`, the usual suspect):

```bash
# 1. lint gate: after every edit/write, run the project's fast linter on the
#    touched file. The model sees the output and fixes its own mistakes in
#    the same turn instead of waiting for CI.
{
  "PostToolUse": [{
    "matcher": "edit_file",
    "command": "f=$(jq -r .tool_input.file_path); ruff check \"$f\" 2>&1 | head -20"
  }]
}

# 2. write protect: refuse edits under paths you never want an agent to
#    touch. Exit code 2 vetoes the call and the message reaches the model.
{
  "PreToolUse": [{
    "matcher": "edit_file",
    "command": "case \"$(jq -r .tool_input.file_path)\" in .env*|*/secrets/*|*.pem) echo 'that path is off-limits' >&2; exit 2;; esac"
  }]
}
```

Both are plain shell; nothing here is CoreCoder-specific syntax beyond the JSON shape and the exit-code-2 veto.

## MCP servers

Drop a `mcp.json` under `~/.corecoder` and tools from any MCP server join the agent over stdio, the same config shape as Claude Code's:

```json
{
  "mcpServers": {
    "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]}
  }
}
```

Each configured server starts as a subprocess at launch, handshakes, and lists its tools; every one is registered as `mcp__<server>__<tool>`, so hook matchers and the consent gate treat it exactly like a built-in. MCP tools stay out of the read-only set, meaning the agent asks before running one. The handshake gets fifteen seconds, a call gets sixty, and a server that dies or never answers fails that one call as an ordinary tool result instead of killing the loop. The REPL's `/mcp` lists every server with its state, and `/mcp reconnect <name>` starts a dead one fresh: new process, new handshake, and the tools the agent already holds work again with no restart. The client speaks the tools slice of the protocol (initialize, tools/list, tools/call) and nothing else, which keeps the whole thing inside `mcp.py` at about 240 lines. With no `mcp.json` there is no MCP and nothing changes. A runnable starter config lives in [`examples/mcp_filesystem.md`](examples/mcp_filesystem.md): the reference filesystem server, scoped to one directory, behind the same consent gate.

## Related Projects

If working through CoreCoder was useful, here are a few other tools I've built around agents and LLM systems:

- **[RepoWiki](https://github.com/he-yufeng/RepoWiki)** — dropped into an unfamiliar codebase? It gives you a guided wiki and a where-to-start reading path, a self-hostable DeepWiki alternative.
- **[FindJobs-Agent](https://github.com/he-yufeng/FindJobs-Agent)** — stop sifting job boards by hand: it ranks postings against your resume and runs mock interviews.
- **[ContractGuard](https://github.com/he-yufeng/ContractGuard)** — catch the risky clauses before you sign: it reads contracts and flags the dangerous bits.
- **[GitSense](https://github.com/he-yufeng/GitSense)** — want to contribute to open source? It finds issues worth your time and gauges whether your PR will get merged.
- **[CodeABC](https://github.com/he-yufeng/CodeABC)** — understand any codebase even if you don't code, built for non-programmers.

## Contributing / License

Before you send anything, run `pytest tests/ -q` (227 tests), `ruff check`, and `compileall`, and make sure they're green. MIT licensed: fork it, learn from it, ship something better. A mention of this project is appreciated.

---

By [Yufeng He](https://github.com/he-yufeng), formerly at Moonshot AI (Kimi). I earlier wrote a fairly complete [Claude Code source analysis](article/claude-code-source-analysis.md) (written in Chinese); this project is its hands-on counterpart: that one walks you through reading it, this one through rebuilding it.

> CoreCoder was formerly named NanoCoder; it was renamed to avoid confusion with [Nano-Collective/nanocoder](https://github.com/Nano-Collective/nanocoder), and old links redirect here automatically.
