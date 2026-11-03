"""Summarize one CoreCoder evaluation JSONL file as reviewable Markdown."""

import argparse
import json
from collections import defaultdict
from pathlib import Path


def _mean(values):
    return round(sum(values) / len(values), 2) if values else "N/A"


def _suite_status(record, key):
    if not record.get("applied") or key not in record:
        return "未运行"
    return "通过" if record[key] else "失败"


def _fraction_optional(group, key):
    if any(key not in row for row in group):
        return "N/A"
    return f"{sum(bool(row[key]) for row in group)}/{len(group)}"


def render(path: Path) -> str:
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    if not lines or lines[0].get("type") != "run":
        raise ValueError("missing run header")
    header, records = lines[0], lines[1:]
    if not records or any(record.get("type") != "case" for record in records):
        raise ValueError("missing or malformed case records")
    modes = defaultdict(list)
    for record in records:
        if record.get("mock") != header["mock"]:
            raise ValueError("mixed mock/live results")
        modes[record["mode"]].append(record)
    title = "模拟模型：仅评测运行器自检" if header["mock"] else "真实模型：Coding Agent 评测"
    output = [f"# {title}", "", f"- 模型：`{header['model']}`",
              f"- 用例版本 SHA-256：`{header['cases_sha256']}`",
              f"- 完整评测集 SHA-256：`{header.get('benchmark_sha256', 'N/A')}`",
              f"- CoreCoder 代码 SHA-256：`{header.get('corecoder_sha256', 'N/A')}`",
              f"- 运行器 SHA-256：`{header.get('harness_sha256', 'N/A')}`",
              f"- 随机顺序种子：`{header['seed']}`；每组每题重复：`{header['repeats']}` 次"]
    if "max_calls_per_case" in header:
        output.append(
            f"- 单题上限：模型调用 {header['max_calls_per_case']} 次；累计 token "
            f"{header['max_total_tokens_per_case']}；调用前时间 {header['max_seconds_per_case']} 秒"
        )
    if "agent_max_rounds" in header:
        output.append(f"- Agent 单轮工具调用上限：{header['agent_max_rounds']} 轮")
    output += ["",
              "| 配置 | 运行数 | 验证通过 | 正常应用 | 常规隐藏通过 | 记忆边界通过 | 隐藏全通过 | 端到端成功 | 越界尝试 | 平均耗时(s) | 平均模型调用 | 平均修复轮数 | 平均 token* | 平均记忆命中 |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for mode in ("baseline", "personalized"):
        group = modes.get(mode, [])
        if not group:
            continue
        count = len(group)
        fraction = lambda name: f"{sum(bool(row[name]) for row in group)}/{count}"
        known_tokens = [row["prompt_tokens"] + row["completion_tokens"] for row in group
                        if row.get("prompt_tokens") is not None and row.get("completion_tokens") is not None]
        token_mean = str(_mean(known_tokens)) if len(known_tokens) == count else "N/A"
        output.append(
            f"| {mode} | {count} | {sum(row['verify'] == 'passed' for row in group)}/{count} "
            f"| {fraction('applied')} | "
            f"{_fraction_optional(group, 'hidden_common_pass')} | "
            f"{_fraction_optional(group, 'hidden_memory_pass')} "
            f"| {fraction('hidden_pass')} | {fraction('success')} "
            f"| {sum(row.get('policy_attempts', 0) for row in group)} "
            f"| {_mean([row['seconds'] for row in group])} | {_mean([row['model_calls'] for row in group])} "
            f"| {_mean([row.get('repair_rounds', 0) for row in group])} "
            f"| {token_mean} | {_mean([row.get('memory_hit_count', 0) for row in group])} |"
        )
    output += ["", "*仅当该组每次运行都收到 token 用量时计算平均值；`N/A` 表示未知。"]
    paired = defaultdict(dict)
    for record in records:
        paired[(record["case"], record["repeat"])][record["mode"]] = record
    complete = [pair for pair in paired.values() if set(pair) == {"baseline", "personalized"}]
    if complete:
        base_pass = sum(pair["baseline"]["success"] for pair in complete)
        personal_pass = sum(pair["personalized"]["success"] for pair in complete)
        output += ["", f"配对比较：{len(complete)} 对；端到端成功 baseline {base_pass}/{len(complete)}，"
                   f"personalized {personal_pass}/{len(complete)}；差值 "
                   f"{(personal_pass - base_pass) / len(complete) * 100:+.1f} 个百分点。"]
    output += ["", "## 逐题诊断", "",
               "| 题目 | 配置 | 次数 | 停止原因 | 公开验证 | 常规隐藏 | 记忆边界 | 越界尝试 | 改动文件 |",
               "|---|---|---:|---|---|---|---|---:|---|"]
    for row in records:
        changed = ", ".join(row.get("changed_paths", [])) or "—"
        output.append(
            f"| `{row['case']}` | {row['mode']} | {row['repeat']} "
            f"| `{row.get('stop_reason') or 'legacy_unknown'}` | {row['verify']} "
            f"| {_suite_status(row, 'hidden_common_pass')} "
            f"| {_suite_status(row, 'hidden_memory_pass')} "
            f"| {row.get('policy_attempts', 0)} | {changed} |"
        )
    errors = [record for record in records if record.get("error")]
    if errors:
        output += ["", "## 运行异常", ""]
        output += [f"- `{row['case']}` / `{row['mode']}` / #{row['repeat']}：`{row['error']}`"
                   + (f" (`{row['error_reason']}`)" if row.get("error_reason") else "")
                   + (f" (budget: `{row['limit_reason']}`)" if row.get("limit_reason") else "")
                   for row in errors]
    output += ["", "模拟模型使用参考实现，结果只证明评测链路可运行，不能作为 Agent 能力数据。"
               if header["mock"] else
               "此结果仅代表本评测集及所记录的模型配置；小样本差异不等于通用能力提升。", ""]
    return "\n".join(output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result = render(args.input)
    if args.output:
        args.output.write_text(result, encoding="utf-8")
    else:
        print(result)


if __name__ == "__main__":
    main()
