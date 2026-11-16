<div align="center">

# Cove

**一个强调改动可审查、自动验证与项目上下文的 CLI Coding Agent。**

[English](README.md) · [评测说明](evals/README.md) · [MIT License](LICENSE)

</div>

Cove 可以在现有代码仓库中读取文件、修改代码、调用工具并汇报结果。对于明确的编码任务，它会在独立 Git Worktree 中执行修改，验证后让用户审查完整差异，再决定是否应用回原工作区。分层规则和经确认的记忆提供项目级上下文。

**命名说明：**仓库名是 Cove；为了兼容现有安装方式，Python 包名、可执行命令、环境变量和本地数据目录目前仍使用 `corecoder`。本仓库建立在 MIT 许可的 [CoreCoder](https://github.com/he-yufeng/CoreCoder) 基础上，来源与许可见文末[致谢与许可](#致谢与许可)。

## 功能概览

| 模块 | 当前能力 |
| --- | --- |
| Agent 核心 | 流式调用 OpenAI 兼容模型、工具调用、轮次上限、请求重试、Token/费用显示和三层上下文压缩。 |
| 工具与控制 | 文件读写编辑搜索、Shell、待办清单、子 Agent、工具授权、只读计划模式、MCP 工具、Hooks、会话保存和 `/undo`。 |
| 任务工作流 | 显式创建独立 Git Worktree；任务状态和对话可恢复；查看完整 diff 与执行轨迹；受检查的补丁应用。 |
| 自动验证 | 用户确认测试/Lint 命令；从配置或 Python、Node、Maven、Gradle 项目探测入口；超时控制；失败后最多两轮自动修复重验。 |
| 个性化 | 分层 Markdown 规则、手动记忆及本地检索、需用户确认才入库的任务记忆建议。 |
| 可选执行隔离 | `/task start --sandbox` 使用 Docker 执行 Shell 和验证，并限制网络、挂载和资源。 |
| 评测 | 12 个小型编码任务、独立隐藏检查、运行与报告脚本，以及记录了条件和局限的结果。 |

## 安装与启动

需要 Python 3.10 及以上版本。建议克隆本仓库并以可编辑模式安装：

```bash
git clone https://github.com/fengjie1215/cove-coding-agent.git
cd cove-coding-agent
python -m pip install -e .
```

配置一个 OpenAI 兼容模型。例如使用 DeepSeek，可在启动命令所在目录创建 `.env`：

```dotenv
CORECODER_API_KEY=your_key_here
CORECODER_MODEL=deepseek-chat
OPENAI_BASE_URL=https://api.deepseek.com
```

Shell 中已有的环境变量优先于 `.env`，配置加载器也会向父目录查找。**不要提交 API Key**：在工作项目中创建 `.env` 时，先确认它已被 Git 忽略。任务 Worktree 不会复制原工作区的 `.env`。

```bash
corecoder                         # 交互模式
corecoder --demo                  # 不需要 API Key 的离线演示
corecoder -p "解释这个仓库的结构"   # 一次性模式
```

普通聊天可以从新文件夹启动；`/task start` 则仅在已有 Git 仓库、且已跟踪文件干净时可用，并且只支持交互模式。一次性模式默认拒绝修改类工具；只有显式添加 `--yes` 才会预授权这些调用。

## 从需求到可审查改动

在要修改的项目目录中启动 `corecoder`，然后输入：

```text
/task start 为解析器补充回归测试并修复错误
/task status
/task diff
/task trace
/task verify
/task apply
```

任务以当前 HEAD 为基点创建 detached Worktree。Agent 在任务目录中修改文件，原工作区在应用前保持不变；未跟踪文件和被忽略文件不会复制。发生改动后，Cove 会提出验证命令，展示给用户确认再运行；验证失败时，结果可反馈给同一任务 Agent，最多自动修复并重验两轮。

`/task diff` 展示完整 Git 差异，包括新增、删除和二进制文件。`/task status` 显示验证结果是否仍对应当前改动。`/task apply` 先展示差异和验证状态并再次确认，然后检查原仓库 HEAD、已跟踪文件是否干净，以及补丁能否应用。成功后，改动会留在原项目的**未提交工作区**，不会自动 commit 或 push。

验证失败、跳过或过期时，普通应用会被阻止。`/task apply --force` 只能在审查后跳过验证门槛，不能跳过补丁冲突或原仓库状态检查。`/task leave` 保留任务以便之后恢复；`/task list` 和 `/task resume <id>` 用于查看与恢复；`/task discard <id>` 经确认后清理任务 Worktree 及未应用的改动。

任务元数据和对话位于 `~/.corecoder/tasks`，任务 Worktree 位于 `~/.corecoder/worktrees`；普通聊天和 `-p` 模式保持原有行为。

### 自定义验证命令

项目可通过 `.corecoder/verify.json` 指定验证入口：

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

没有配置时，Cove 会探测已有的 Python、Node、Maven、Gradle 测试或 Lint 入口。命令按参数数组启动并设置超时；验证子进程不会继承模型 API Key，也不会自动安装缺失依赖。补丁或验证脚本改变后，旧验证结果会失效。

## 分层规则与记忆

可选的 UTF-8 Markdown 规则按以下顺序读取，越往后优先级越高：

1. `~/.corecoder/rules.md`：全局个人偏好。
2. `~/.corecoder/projects/<project-id>/rules.md`：本项目的私有偏好。
3. 仓库根目录 `AGENTS.md`：项目共享规则。

`/rules show` 可查看实际路径、规则内容和加载错误。任务模式复用原项目身份来查找私有规则，共享的 `AGENTS.md` 则从任务 Worktree 读取。规则只影响提示词上下文，**不能授予工具权限，也不能绕过验证**。

记忆用于保存事实和决策，不用于存放行为约束。`/memory add <内容>` 手动保存项目记忆；`/memory add --global <内容>` 保存跨项目记忆。`/memory list`、`/memory search <查询>` 和 `/memory why [id]` 用于查看、检索和追溯来源。确认过的记忆保存在本地 `~/.corecoder/memory.sqlite3`，每轮只检索少量相关内容。

任务验证通过并正常应用后，Cove 可询问是否额外调用一次模型，生成最多三条候选项目记忆。**候选不会自动入库。**使用 `/memory candidates` 审查，用 `/memory accept <id> [修改后内容]` 确认，或用 `/memory reject <id>` 拒绝；`/memory disable <id>` 可停用过期记忆并保留来源。候选内容的过滤是尽力而为，接受前仍应检查是否包含秘密或错误结论。

## 执行轨迹、Docker 与安全边界

`/task trace` 可查看当前任务，`/task trace <id>` 可查看已保存的任务。JSONL 轨迹记录模型轮次、Token、工具与授权结果、耗时、验证与修复状态、补丁摘要；**不记录**提示词、工具参数/输出、Shell 命令或验证日志。因此它适合定位失败阶段，不能完整重放任务。

Git Worktree 只隔离 Git 文件改动，**不是 Shell 或 MCP 的安全沙箱**。需要更严格的任务执行时，将 `CORECODER_SANDBOX_IMAGE` 指向本机已有且来源可信的 Linux 镜像，再使用 `/task start --sandbox <需求>`。Cove 会在启动任务前检查 Docker 和镜像；不可用则直接失败，不会退回宿主机执行。

在 Docker 任务中，每次 Shell 调用都要单独授权；Shell 和自动验证在独立容器中运行，仅挂载任务 Worktree、禁用网络、容器根目录只读、`/tmp` 为临时目录、以非 root 用户运行，并限制资源。任务 MCP 和宿主机 Hooks 被禁用；模型 API 调用仍在宿主机进行。镜像需预装项目所需工具。容器可降低宿主机风险，但**不能保证模型生成的代码绝对安全**。详见[安全执行验收设计](evals/任务级安全执行验收设计.md)。

## 评测与开发

[`evals/`](evals/README.md) 包含 12 个合成 Python 编码任务、独立隐藏测试、运行与报告脚本，以及[结果说明](evals/测试结果.md)。一次 72 次的 DeepSeek 对比（12 题 × 两组配置 × 3 次重复）中，常规隐藏测试两组均为 36/36；记忆特定边界要求在无相关规则/记忆时为 21/36，加入相关上下文后为 36/36。这只能说明**在当前合成题集上，对给定项目事实的遵循有所改善**；不能推断通用编码正确率提升、分别归因于规则或记忆，也没有覆盖 Java 项目。

常用本地检查：

```bash
python -m pytest tests -q
python -m pytest evals/test_evals.py -q
```

主要源码位置：

- `corecoder/agent.py`、`llm.py`、`context.py`：Agent 循环、模型调用和上下文管理。
- `corecoder/tools/`、`permissions.py`、`mcp.py`、`hooks.py`：工具及执行闸门。
- `corecoder/tasks.py`、`task_cli.py`、`workspace.py`、`verify.py`：任务生命周期、Worktree 与验证。
- `corecoder/rules.py`、`memory.py`、`learning.py`：项目上下文与确认后学习。
- `corecoder/trace.py`、`sandbox.py`：轨迹与可选容器执行。
- `tests/`、`evals/`：回归测试与模型评测。

交互模式下输入 `/help` 可查看完整命令。仓库内的[源码阅读文章](article/00-index.md)来自原 CoreCoder 项目，介绍其基础实现。

## 致谢与许可

Cove 基于 [何宇峰](https://github.com/he-yufeng) 创建的 [CoreCoder](https://github.com/he-yufeng/CoreCoder) 继续开发，遵循 MIT License。原版权声明保留在 [LICENSE](LICENSE) 中，Git 历史也保留了上游提交。本仓库新增的任务工作流、个性化、轨迹、安全执行与评测能力已在上文说明。
