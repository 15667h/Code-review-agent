"""Margin Harness 命令行入口。

用法示例：

    # 离线跑通全链路（无需 API Key）
    python -m harness -r ./your-repo --offline

    # 用真实模型审计
    python -m harness -r ./your-repo -g "审查这次限流中间件的改动，重点看并发与资源泄漏"

    # 只跑确定性规则扫描
    python -m harness -r ./your-repo --scan-only

退出码：0 正常；1 存在阻断级发现；2 运行失败（配置或模型错误）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import Config, load_config
from . import agents
from .agents import UnknownAgent, default_id, get as get_agent
from .loop import AuditResult, resolve_workspace, run_agent
from .model import DeepSeekModel, ModelError
from .offline import HeuristicModel
from .report import render_markdown, render_terminal
from .rules import cross_check, scan_tree, to_findings
from .tools_audit import build_toolbox
from .types import RunState

DEFAULT_GOAL = get_agent(default_id()).default_goal


# =============================================================================
# 输出辅助
# =============================================================================

_DIM = "\033[2m"
_BOLD = "\033[1m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_RESET = "\033[0m"


def _supports_color() -> bool:
    return sys.stdout.isatty()


def _paint(text: str, code: str) -> str:
    return f"{code}{text}{_RESET}" if _supports_color() else text


def _ensure_utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass


def _write_output(path: str, content: str) -> Path:
    """写文件并自动创建父目录。

    早期版本直接调 write_text，--out 指向不存在的目录时会抛异常，
    而且异常会被管道吞掉，表现为「命令像跑成功了但文件没生成」。
    """
    target = Path(path)
    if str(target.parent) not in {"", "."} and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def make_event_printer(verbose: bool):
    def printer(event: str, payload: dict[str, Any]) -> None:
        if not verbose:
            return

        if event == "start":
            prefix = payload.get("prefix", {})
            print(_paint("─" * 64, _DIM))
            print(_paint("  Margin Harness · 代码审计", _BOLD))
            print(_paint("─" * 64, _DIM))
            print(f"  静态前缀指纹 {prefix.get('fingerprint')}  ·  约 {prefix.get('tokens')} tokens")
            print(f"  核心工具 {prefix.get('tool_count')} 个（其余按需检索）")
            print("")

        elif event == "decision":
            decision = payload["decision"]
            iteration = payload["iteration"]
            names = ", ".join(call.name for call in decision.tool_calls) or "（无工具调用）"
            usage = payload.get("usage")
            extra = ""
            if usage and usage.cached_tokens:
                extra = _paint(f"  缓存命中 {usage.cached_tokens}", _DIM)
            print(f"  [{iteration:>2}] {_paint(names, _BOLD)}{extra}")

        elif event == "tool":
            call = payload["call"]
            result = payload["result"]
            mark = _paint("✓", _GREEN) if result.ok else _paint("✗", _RED)
            first_line = result.render().strip().splitlines()[0][:96] if result.render().strip() else ""
            print(f"        {mark} {call.name}: {first_line}")
            for note in payload.get("verification") or []:
                print(_paint(f"          · {note}", _DIM))

        elif event == "compression":
            stats = payload.get("stats", {})
            print(
                _paint(
                    f"  [压缩] 第 {payload['iteration']} 轮：压缩 {stats.get('compressed', 0)} 条，"
                    f"丢弃噪声 {stats.get('dropped_noise', 0)} 条",
                    _YELLOW,
                )
            )

        elif event == "model_retry":
            print(_paint(f"  [重试] 第 {payload['attempt']} 次：{payload['error'][:120]}", _YELLOW))

    return printer


# =============================================================================
# 参数
# =============================================================================


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="audit",
        description="Margin Harness —— 自研代码审计 Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-r", "--repo", default=".", help="被审计的仓库路径（默认当前目录）")
    parser.add_argument(
        "-a",
        "--agent",
        default="",
        help=f"用哪个 agent（{', '.join(agents.ids())}；默认 {agents.default_id()}）",
    )
    parser.add_argument("-g", "--goal", default="", help="任务目标（不填则用所选 agent 的默认目标）")
    parser.add_argument("--context", default="", help="补充给模型的背景信息")
    parser.add_argument("--out", default="", help="Markdown 报告输出路径")
    parser.add_argument("--json", default="", help="JSON 结果输出路径")
    parser.add_argument("--offline", action="store_true", help="离线规则模式，不需要 API Key")
    parser.add_argument("--scan-only", action="store_true", help="只跑确定性规则扫描，不调用模型")
    parser.add_argument("--model", default="", help="覆盖模型名")
    parser.add_argument("--max-iterations", type=int, default=0, help="覆盖最大迭代轮数")
    parser.add_argument("--allow-high-risk", action="store_true", help="允许高风险工具（如执行命令）")
    parser.add_argument("--quiet", action="store_true", help="只输出最终报告")
    parser.add_argument("--list-tools", action="store_true", help="列出工具集后退出")
    parser.add_argument("--no-cross-check", action="store_true", help="关闭规则交叉验证")
    return parser


# =============================================================================
# 主流程
# =============================================================================


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)

    repo = Path(args.repo).expanduser().resolve()
    if not repo.is_dir():
        print(f"错误：仓库路径不存在或不是目录：{repo}", file=sys.stderr)
        return 2

    # agent 在任何实质动作之前校验：跑完了才发现选错助手，成本已经付出去了
    try:
        get_agent(args.agent or default_id())
    except UnknownAgent as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    config = _build_config(args, repo)

    if args.list_tools:
        return _print_tools(repo, args, config)

    if args.scan_only:
        return _run_scan_only(repo, args)

    return _run_agent(repo, args, config)


def _build_config(args: argparse.Namespace, repo: Path) -> Config:
    config = load_config()
    if args.model:
        config.model = args.model
    if args.max_iterations:
        config.max_iterations = args.max_iterations
    if config.workspace_root is None:
        config.workspace_root = repo
    return config


def _print_tools(repo: Path, args: argparse.Namespace, config: Config) -> int:
    from .sandbox import CommandRunner
    from .tools import tool_report

    spec = get_agent(args.agent or default_id())
    workspace = resolve_workspace(repo)
    state = RunState(goal="(list tools)")
    runner = CommandRunner(workspace=workspace, allow=config.allow_commands)
    registry = spec.build_tools(workspace, state, runner)
    print(f"工具集（{spec.name} · 工作区 {repo}）：")
    print(tool_report(registry))
    print("")
    print(f"允许的命令：{', '.join(sorted(config.allow_commands))}")
    return 0


def _run_scan_only(repo: Path, args: argparse.Namespace) -> int:
    print(_paint(f"规则扫描：{repo}", _BOLD))
    report = scan_tree(repo)
    findings = to_findings(report)

    print(f"扫描 {report.scanned_files} 个文件，命中 {len(report.hits)} 条：")
    for hit in report.hits:
        color = {"blocker": _RED, "warn": _YELLOW, "note": _DIM}[hit.rule.severity]
        print(f"  {_paint('[' + hit.rule.severity + ']', color)} {hit.path}:{hit.line} {hit.rule.id} {hit.rule.title}")

    if args.out:
        result = AuditResult(goal="（确定性规则扫描）", findings=findings, summary="由规则集直接产出，未经模型推理。")
        _write_output(args.out, render_markdown(result, repo=str(repo), model="rules-only"))
        print(f"\n报告已写入 {args.out}")

    blockers = sum(1 for hit in report.hits if hit.rule.severity == "blocker")
    return 1 if blockers else 0


def _run_agent(repo: Path, args: argparse.Namespace, config: Config) -> int:
    workspace = resolve_workspace(repo)
    verbose = not args.quiet

    spec = get_agent(args.agent or default_id())
    goal = args.goal.strip() or spec.default_goal

    if args.offline:
        model = HeuristicModel(agent=spec.id)
        model_label = "heuristic-offline（规则驱动）"
    else:
        if not config.ready:
            print(
                "错误：未配置 DEEPSEEK_API_KEY。\n"
                "  · 想验证完整链路但暂时没有 Key，请加 --offline；\n"
                "  · 想只跑确定性规则，请加 --scan-only；\n"
                "  · 想配 Key，请把 backend/.env.example 复制为 backend/.env 并填写。",
                file=sys.stderr,
            )
            return 2
        try:
            model = DeepSeekModel(config)
        except ModelError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 2
        model_label = f"{config.model}（DeepSeek）"

    print(f"仓库：{repo}")
    print(f"助手：{spec.name}（{spec.id}）")
    print(f"模型：{model_label}")
    print(f"目标：{goal}")
    print("")

    try:
        result = run_agent(
            goal=goal,
            agent=spec.id,
            workspace=workspace,
            model=model,
            config=config,
            context_note=args.context,
            on_event=make_event_printer(verbose),
            allow_high_risk=args.allow_high_risk,
        )
    except ModelError as exc:
        print(f"模型调用失败：{exc.error_type} - {exc}", file=sys.stderr)
        return 2
    finally:
        close = getattr(model, "close", None)
        if callable(close):
            close()

    # ---- 规则交叉验证：规则命中但模型没报的位置，疑似漏报 ----
    # 交叉验证的前提是「这次运行产出的是发现」。架构助手不产出发现，
    # 对它做这套检查只会得出一条毫无意义的「疑似漏报」。
    if not args.no_cross_check and "finding" in result.artifacts:
        report = scan_tree(repo)
        missed = cross_check(result.findings, report)
        if missed:
            result.warnings.append(
                "确定性规则命中但本次未报告的位置（疑似漏报，需人工复核）：\n"
                + "\n".join(missed[:12])
            )

    print(render_terminal(result))

    if args.out:
        _write_output(args.out, render_markdown(result, repo=str(repo), model=model_label))
        print(f"Markdown 报告已写入：{args.out}")

    if args.json:
        payload = {
            "goal": result.goal,
            "agent": result.agent,
            "outcome": result.outcome,
            "artifacts": {
                kind: [item.to_dict() for item in items]
                for kind, items in result.artifacts.items()
            },
            "repo": str(repo),
            "model": model_label,
            "iterations": result.iterations,
            "stop_reason": result.stop_reason,
            "elapsed": round(result.elapsed, 3),
            "usage": {
                "prompt_tokens": result.usage.prompt_tokens,
                "completion_tokens": result.usage.completion_tokens,
                "cached_tokens": result.usage.cached_tokens,
            },
            "findings": [item.to_dict() for item in result.findings],
            "warnings": result.warnings,
            "compression_events": result.compression_events,
            "prefix": result.prefix_trace,
        }
        _write_output(args.json, json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"JSON 结果已写入：{args.json}")

    return 1 if result.severity_counts["blocker"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
