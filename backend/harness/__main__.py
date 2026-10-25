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
from .model import OpenAIModel, ModelError
from .offline import HeuristicModel
from .report import render_markdown, render_terminal
from .rules import cross_check, scan_tree, to_findings
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
    parser.add_argument(
        "--sarif",
        default="",
        help="SARIF 2.1.0 输出路径（给 GitHub Code Scanning / 其他 CI 用）。"
             "配合 --diff 时只输出本次改动范围内的发现。",
    )
    parser.add_argument("--offline", action="store_true", help="离线规则模式，不需要 API Key")
    parser.add_argument("--scan-only", action="store_true", help="只跑确定性规则扫描，不调用模型")
    parser.add_argument("--model", default="", help="覆盖模型名")
    parser.add_argument("--max-iterations", type=int, default=0, help="覆盖最大迭代轮数")
    parser.add_argument("--allow-high-risk", action="store_true", help="允许高风险工具（如执行命令）")
    parser.add_argument("--quiet", action="store_true", help="只输出最终报告")
    parser.add_argument("--list-tools", action="store_true", help="列出工具集后退出")
    parser.add_argument("--no-cross-check", action="store_true", help="关闭规则交叉验证")
    parser.add_argument(
        "--diff",
        metavar="BASE",
        default="",
        help=(
            "增量审查：只审 BASE 到工作树的改动及其调用点（例如 --diff HEAD、"
            "--diff HEAD~1、--diff main）。不给就审全仓。"
        ),
    )
    parser.add_argument("--no-judge", action="store_true", help="关闭收尾复核（仅用于 A/B 对比）")
    return parser

# =============================================================================
# 主流程
# =============================================================================

def _compose_context(repo: Path, args: argparse.Namespace) -> str:
    """把 --context 与 --diff 生成的审查简报合成一段给模型的背景说明。

    diff 简报放在前面：它是**任务范围**，比用户补充的背景更靠前更有约束力。
    增量模式下若拿不到 diff（不是 git 仓库、base 不存在），直接报错退出 ——
    静默退化成全仓审查会让「只审改动」的承诺落空，用户还以为自己审的是增量。
    """
    parts: list[str] = []

    base = getattr(args, "diff", "") or ""
    if base:
        from .diffmode import collect_diff

        report = collect_diff(repo, base)
        if not report.ok:
            print(f"错误：{report.error}", file=sys.stderr)
            return ""
        brief = report.brief()
        if not brief:
            print("提示：基准与工作树之间没有差异，本次没有可审的改动。", file=sys.stderr)
            return ""
        parts.append(brief)

    extra = (getattr(args, "context", "") or "").strip()
    if extra:
        parts.append("## 补充背景\n\n" + extra)

    return "\n\n".join(parts)

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
        # findings 是 artifacts["finding"] 上的只读视图，不能当构造参数传
        # （dataclass __init__ 不认识它）。平台化后这里必须走 artifacts。
        result = AuditResult(
            goal="（确定性规则扫描）",
            artifacts={"finding": findings},
            outcome="findings",
            summary="由规则集直接产出，未经模型推理。",
        )
        _write_output(args.out, render_markdown(result, repo=str(repo), model="rules-only"))
        print(f"\n报告已写入 {args.out}")

    # ---- 增量过滤：--scan-only 也必须支持 --diff ----
    #
    # 之前这里漏了：传了 --diff 会被**静默忽略**，仍然输出全仓结果。
    # 那比报错更糟 —— CI 里用户以为过滤生效，实际没有，PR 上照样一片红。
    findings_for_report = findings
    note = ""
    if getattr(args, "diff", ""):
        findings_for_report, note = _filter_to_diff(findings, repo, args.diff)
        print(f"\n{note}")

    if args.sarif:
        from .sarif import rule_metadata, write_sarif

        write_sarif(findings_for_report, args.sarif, repo=str(repo), rules=rule_metadata())
        print(f"SARIF 已写入 {args.sarif}（{len(findings_for_report)} 条）")

    # 退出码按**过滤后**的结果算：CI 要拦的是「这个 PR 引入了阻断级问题」，
    # 不是「仓库里历史上有阻断级问题」—— 后者会让任何 PR 都过不去。
    blockers = sum(1 for f in findings_for_report if str(f.severity) == "blocker")
    return 1 if blockers else 0


def _filter_to_diff(findings: list, repo: Path, base: str) -> tuple[list, str]:
    """只留下「落在本次改动范围内」的发现。

    ## 为什么 CI 里必须过滤

    PR 场景下，全仓扫描会把**历史遗留问题**也报出来。开发者打开 PR 看到
    200 条与本次改动无关的红色标记，第一反应是把这个检查关掉 ——
    这正是「AI 审查变成噪音」的典型路径。

    只报改动行及其邻近（±3 行）的问题，让「这个 PR 引入了什么」成为唯一的信号。

    ## 为什么容差是 3 行

    模型给的行号常有一两行偏差（它引用的是上下文行而不是精确行）。
    容差太小会漏掉真实发现，太大又会把邻近的旧问题拉进来。
    3 行是这个权衡的中点，且与项目既有评估口径（行号 ±2）量级一致。
    """
    from .diffmode import collect_diff

    try:
        report = collect_diff(repo, base)
    except Exception as exc:  # diff 拿不到时不要吞掉，交给调用方决定
        return findings, f"无法获取 diff（{exc}），已输出全仓结果"

    if not report.ok:
        return findings, f"diff 为空或不可解析（{report.brief()}），已输出全仓结果"

    changed = {h.file for h in report.hunks}
    kept = [f for f in findings if str(f.file).replace("\\", "/") in changed]
    dropped = len(findings) - len(kept)
    return kept, (
        f"按 --diff {base} 过滤：改动 {len(changed)} 个文件，"
        f"保留 {len(kept)} 条、省略 {dropped} 条改动范围外的历史发现"
    )


def _line_is_in_files(warning_line: str, files: set[str]) -> bool:
    """判断一条「疑似漏报」的文案是否指向本次改动的文件。

    ``cross_check`` 产出的每行形如 ``app/api.py:71 SEC001 硬编码密钥``。
    这里按**路径前缀**精确比对，不用裸子串包含：

    - 子串 ``"app/api.py" in "x/app/api.py.bak"`` 为真 → 误判
    - 反过来 ``"api.py" in "app/api.py"`` 也为真 → 缩窄成文件名时同样误判

    所以取行首的 ``file:line`` 段，与改动集合做集合比较。
    """
    head = warning_line.split()[0] if warning_line.split() else ""
    # 去掉行号后缀：`app/api.py:71` → `app/api.py`
    path = head.rsplit(":", 1)[0].replace("\\", "/") if ":" in head else head
    if path in files:
        return True
    # 某些规则可能输出绝对路径或带 ./ 前缀，做一次归一后再比
    norm = path.lstrip("./")
    return norm in files


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
                "错误：未配置 LLM_API_KEY（模型接口 / Key / 模型名三项不齐）。\n"
                "  · 想验证完整链路但暂时没有 Key，请加 --offline；\n"
                "  · 想只跑确定性规则，请加 --scan-only；\n"
                "  · 想配 Key，请把 backend/.env.example 复制为 backend/.env 并填写。",
                file=sys.stderr,
            )
            return 2
        try:
            model = OpenAIModel(config)
        except ModelError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 2
        model_label = f"{config.model} @ {config.base_url}（OpenAI 兼容）"

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
            context_note=_compose_context(repo, args),
            judge=not getattr(args, "no_judge", False),
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
        # 增量模式下必须把「疑似漏报」也限制在改动范围内。
        #
        # 实测后果（真实 PR 审查）：--diff 只审一个文件，但这里的 scan_tree
        # 扫的是**全仓**，于是把 12 条与本次改动毫无关系的规则命中
        # 全列成「疑似漏报」。评审看到的第一反应是「这工具在胡说」——
        # 而它其实什么都没错，只是比错了范围。
        diff_base = getattr(args, "diff", "")
        if diff_base and missed:
            # 过滤失败时**必须退回「不过滤」而不是清空**。
            #
            # 这是模型在我第一版代码里抓到的真 bug：
            #   changed = {...} 失败 → 空集 → 所有 missed 被过滤掉 → 静默无输出
            # 用户看到「没有疑似漏报」，实际是「比较失败了」——
            # 静默失败比明确失败危险得多。
            filtered: list[str] | None = None
            try:
                from .diffmode import collect_diff

                diff_report = collect_diff(repo, diff_base)
                if diff_report.ok and diff_report.hunks:
                    changed = {h.file for h in diff_report.hunks}
                    # 精确比对路径，不用子串：`app/api.py` 是 `x/app/api.py.bak`
                    # 的子串，会误判。
                    filtered = [
                        line for line in missed
                        if any(_line_is_in_files(line, changed))
                    ]
                else:
                    result.warnings.append(
                        f"交叉验证未能按 `--diff {diff_base}` 收窄范围"
                        f"（{diff_report.error or 'diff 为空'}），以下为全仓比对结果"
                    )
            except Exception as exc:  # noqa: BLE001 - 但要说出来，不能静默
                result.warnings.append(
                    f"交叉验证收窄范围失败（{exc}），以下为全仓比对结果"
                )
            if filtered is not None:
                missed = filtered
        if missed:
            scope = f"（仅限本次 `--diff {diff_base}` 范围）" if diff_base else ""
            result.warnings.append(
                f"确定性规则命中但本次未报告的位置{scope}（疑似漏报，需人工复核）：\n"
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
        # 架构助手的证据核验报告：评估脚本与前端都要读它做「可精确定位率」
        if getattr(result, "evidence_report", None):
            payload["evidence_report"] = result.evidence_report
        _write_output(args.json, json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"JSON 结果已写入：{args.json}")

    # ---- SARIF：给 GitHub Code Scanning 用 ----
    if args.sarif:
        from .sarif import rule_metadata, write_sarif

        findings = list(result.findings)
        note = ""
        # CI 里必须只报改动范围，否则历史遗留会把 PR 刷红（见 _filter_to_diff 注释）
        if getattr(args, "diff", ""):
            findings, note = _filter_to_diff(findings, repo, args.diff)
        write_sarif(findings, args.sarif, repo=str(repo), rules=rule_metadata())
        print(f"SARIF 已写入：{args.sarif}（{len(findings)} 条）")
        if note:
            print(f"  {note}")

    return 1 if result.severity_counts["blocker"] else 0

if __name__ == "__main__":
    raise SystemExit(main())
