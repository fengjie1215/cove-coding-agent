<div align="center">

# Cove

**A CLI coding agent for reviewable changes, automatic verification, and project-aware context.**

[简体中文](README_CN.md) · [Evaluation](evals/README.md) · [MIT License](LICENSE)

</div>

Cove works in an existing codebase: it reads files, edits code, runs tools, and reports results through a model-driven agent loop. For coding tasks, it can create a separate Git Worktree, verify the change, and let you review the complete diff before applying it to your original checkout. Layered rules and approved memories supply project-specific context.

**Naming note:** this repository is called Cove, but the Python package, executable, environment variables, and local data directory still use `corecoder` for compatibility. The repository is an extension of the MIT-licensed [CoreCoder](https://github.com/he-yufeng/CoreCoder); see [Attribution](#attribution-and-license).

## What it can do

| Area | Current capability |
| --- | --- |
| Agent runtime | Streaming OpenAI-compatible model calls, tool calling, bounded rounds, retries, token/cost display, and three-tier context compaction. |
| Tools and control | File read/write/edit/search, Shell, task checklists, sub-agents, permission prompts, read-only plan mode, MCP tools, hooks, saved sessions, and `/undo`. |
| Task workflow | A detached Git Worktree per explicit interactive task, separate task state, resumable conversations, full diff, trace, and guarded application back to the original checkout. |
| Verification | Confirmed test/lint commands, project config or Python/Node/Maven/Gradle discovery, timeouts, and at most two automatic fix-and-reverify rounds. |
| Personalization | Layered Markdown rules, manually saved and locally retrieved memories, plus memory suggestions that require user approval before storage. |
| Optional execution isolation | Docker-backed Shell and verification for `/task start --sandbox`, with no network and restricted mounts/resources. |
| Evaluation | A local harness with 12 small coding tasks, hidden checks, reproducible run settings, and documented results. |

## Install and run

Requires Python 3.10 or newer. Clone this repository and install it in editable mode:

```bash
git clone https://github.com/fengjie1215/cove-coding-agent.git
cd cove-coding-agent
python -m pip install -e .
```

Configure an OpenAI-compatible model. For DeepSeek, an example `.env` in the directory where you launch the CLI is:

```dotenv
CORECODER_API_KEY=your_key_here
CORECODER_MODEL=deepseek-chat
OPENAI_BASE_URL=https://api.deepseek.com
```

Environment variables already set in the Shell take precedence over `.env`. The loader also searches parent directories. **Keep API keys out of Git**: make sure your working project's `.env` is ignored before committing. The source repository's `.env` is not copied into task Worktrees.

```bash
corecoder                    # interactive CLI
corecoder --demo             # offline demo; no API key required
corecoder -p "explain this repository"  # one-shot mode
```

Task Worktrees are available only in the interactive CLI. In one-shot mode, mutating tools are denied unless you explicitly pass `--yes`. The CLI can still be launched from a new folder for ordinary chat, but `/task start` requires an existing Git repository with clean tracked files.

## A reviewable coding task

Run `corecoder` from the project you want to change, then use:

```text
/task start Add a regression test for the parser and fix the bug
/task status
/task diff
/task trace
/task verify
/task apply
```

The task starts from the project's current HEAD in a detached Worktree. The original checkout stays unchanged while the agent edits the task copy. Untracked and ignored files are not copied. If the agent changes files, Cove proposes validation commands and asks before running them. A failed validation can be sent back to the same agent for up to two repair rounds.

`/task diff` shows the full Git change, including added, deleted, and binary files. `/task status` shows whether validation is current for that exact diff. `/task apply` displays the diff and validation state, asks for confirmation, then checks the original HEAD, tracked-file cleanliness, and patch applicability. On success it leaves the result **uncommitted** in the original checkout; it does not commit or push.

Failed, skipped, or stale validation blocks normal application. `/task apply --force` can override only that validation gate after review; it cannot override patch conflicts or a changed/dirty original checkout. Use `/task leave` to keep a task for later, `/task list` and `/task resume <id>` to restore it, or `/task discard <id>` to explicitly remove its Worktree and unapplied changes.

Task state and conversations live under `~/.corecoder/tasks`; task Worktrees live under `~/.corecoder/worktrees`. Ordinary chat and one-shot mode keep their existing behavior.

### Verification configuration

Cove reads `.corecoder/verify.json` from the project when present. For example:

```json
{
  "commands": [
    {
      "name": "tests",
      "argv": ["python", "-m", "pytest"],
      "timeout": 180
    }
  ]
}
```

Without a config, it looks for existing Python, Node, Maven, or Gradle test/lint entry points. Commands are passed as argument arrays, run with a timeout, and do not receive the model API key in their environment. Cove does not install missing dependencies. Changes to the diff or verification scripts make previous results stale.

## Project rules and memory

Rules are optional UTF-8 Markdown files, loaded from low to high priority:

1. `~/.corecoder/rules.md` — global personal preferences.
2. `~/.corecoder/projects/<project-id>/rules.md` — private preferences for this project.
3. `AGENTS.md` at the repository root — shared project rules.

Use `/rules show` to inspect the effective paths, contents, and loading errors. A task uses the original project's identity for private rules and reads shared `AGENTS.md` from its Worktree. Rules are prompt context, **not** tool permissions or a way around verification.

Memories are different: they store facts or decisions, not instructions. Add one manually with `/memory add <text>` (or `--global` for a cross-project note); inspect them with `/memory list`, `/memory search <query>`, and `/memory why [id]`. Approved notes are stored locally in `~/.corecoder/memory.sqlite3` and a small number of relevant notes are retrieved each turn.

After a verified task is applied normally, Cove can ask whether to spend one model call proposing up to three candidate project memories. **Nothing is stored automatically.** Review with `/memory candidates`, then use `/memory accept <id> [edited text]` or `/memory reject <id>`. `/memory disable <id>` stops retrieval of an outdated note while retaining its provenance. Candidate filtering is best-effort; never accept secrets or unreviewed output.

## Trace, sandbox, and safety boundaries

`/task trace` (or `/task trace <id>` for a saved task) summarizes model rounds, token usage, tool and permission outcomes, timing, validation, repair, and patch changes. Its JSONL record does **not** store prompts, tool arguments/results, Shell commands, or verification logs; it is for diagnosis, not full replay.

A Git Worktree isolates Git file changes, **not** Shell or external MCP side effects. For an opt-in stricter task, set `CORECODER_SANDBOX_IMAGE` to a trusted Linux image already present locally, then run `/task start --sandbox <request>`. Cove checks Docker before starting and fails closed if Docker or the image is unavailable. In this mode, every Shell call needs fresh approval; Shell and automatic verification run in separate containers with only the task Worktree mounted, networking disabled, a read-only container root, temporary `/tmp`, a non-root user, and resource limits. Task MCP tools and host hooks are disabled. The model API call remains on the host.

The image must already contain the required tools. Docker reduces host-side risk but is **not** a guarantee that generated code is harmless. See the [sandbox acceptance design](evals/任务级安全执行验收设计.md).

## Evaluation and development

The [evaluation harness](evals/README.md) contains 12 synthetic Python coding tasks, hidden checks, runner/report scripts, and a [documented result report](evals/测试结果.md). In a 72-run DeepSeek comparison on this task set (12 tasks × two configurations × three repeats), ordinary hidden checks passed 36/36 in both groups; memory-specific boundary checks passed 21/36 without relevant rules/memories and 36/36 with them. This supports better adherence to those supplied project facts **on this synthetic set only**. It does not show a general coding-correctness improvement, isolate rules from memory, or evaluate Java projects.

Useful local checks:

```bash
python -m pytest tests -q
python -m pytest evals/test_evals.py -q
```

The code map for deeper reading:

- `corecoder/agent.py`, `llm.py`, `context.py` — agent loop, model client, and context management.
- `corecoder/tools/`, `permissions.py`, `mcp.py`, `hooks.py` — tools and execution gates.
- `corecoder/tasks.py`, `task_cli.py`, `workspace.py`, `verify.py` — task lifecycle, Worktree, and validation.
- `corecoder/rules.py`, `memory.py`, `learning.py` — project context and confirmed learning.
- `corecoder/trace.py`, `sandbox.py` — diagnostics and opt-in container execution.
- `tests/` and `evals/` — regression tests and model evaluation.

Run `/help` inside the interactive CLI for the complete command list. The bundled [source-reading essays](article/00-index_EN.md) come from the original CoreCoder project and describe its foundation.

## Attribution and license

Cove extends [CoreCoder](https://github.com/he-yufeng/CoreCoder), originally created by [Yufeng He](https://github.com/he-yufeng), under the MIT License. The original copyright notice is retained in [LICENSE](LICENSE), and the Git history preserves the upstream commits. This repository's added task, personalization, trace, sandbox, and evaluation work is documented above.
