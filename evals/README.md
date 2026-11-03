# CoreCoder 本地评测

本目录集中保存评测脚本、12 个任务、夹具、独立隐藏测试，以及测试计划、原理、预期和结果。`tests/` 仍是 CoreCoder 常规回归测试目录。

在项目根目录运行：

```powershell
python -m pytest evals/test_evals.py -q
python -m evals.run --mock --pilot
python -m evals.report evals/results/<运行文件>.jsonl
```

`--agent-rounds` 控制一次 Agent 对话允许的工具调用轮数（默认 20）；
`--max-calls` 控制每题的模型调用总数（默认 20，包含自动修复）。两者是不同限制，
触及任一限制都记为未完成，不计为正常编码结果。需要提高预算时请同时审核费用，
并在 A/B 两组使用相同参数。评测任务不提供 Shell；Agent 完成编辑后由任务控制器
执行已确认的公开验证。对非目标文件的写入会即时拒绝并计入越界尝试。

真实 API 运行会执行模型修改过的 Python 代码。**Git Worktree 不是安全沙箱；请只在一次性容器或虚拟机中运行**，并先阅读[测试计划](测试计划.md)与[评测原理](评测原理.md)。模拟模型使用参考实现，仅验证运行器，不代表 Agent 的编码能力。

Windows PowerShell / Docker Desktop 的六题真实试运行（在项目根目录；先核对 `.env` 只包含本次评测需要的环境变量）：

```powershell
docker build -f evals/Dockerfile -t corecoder-eval:local .
$evalResults = Join-Path (Get-Location) 'evals\results'
docker run --rm --read-only --tmpfs /tmp:rw,nosuid,nodev,size=512m --cap-drop ALL --security-opt no-new-privileges --pids-limit 128 --memory 1g --cpus 2 --env-file .env -v "${evalResults}:/app/evals/results" corecoder-eval:local python -u -m evals.run --live --allow-code-execution --pilot
```

`.dockerignore` 让密钥文件不进入镜像；但运行中的容器仍需持有 API Key 并联网，不能视为对密钥的绝对隔离。只挂载结果目录，不挂载日常项目代码。若 Docker Hub 的 Python 镜像不可达，本评测 Dockerfile 复用本机常见的 Redis/Debian 基础镜像并安装 Python；它**不会启动 Redis 服务**。

`results/` 下的机器输出默认不纳入 Git；人工核对后的摘要写入[测试结果](测试结果.md)。
