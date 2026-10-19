"""判官层：在发现落盘之前，用一遍独立的复核把「撑不住证据的发现」挡下来。

## 为什么需要这一层

业界实测（CR-Bench，5,642 个真实 PR 任务）里，单次通过的精确率只有 0.38，
加上「多遍 + 过滤」能提到 0.62 —— 而**过滤**正是这里缺的那一环。
CodeRabbit 的做法是开 2–50 个 linter 并跑，再用独立 judge 模型逐条打分，
grounding 不了就丢弃；Greptile 会真的把代码跑起来，拿日志当证据。

我们的做法分两步，都是确定性能做的先做：

1. **确定性核验**（`guardrails.cross_check_line_content`）：引用的行号是否存在、
   那一行的内容是否真的在支撑描述。不花 token，先砍掉最硬的一类。
2. **独立判官**（本模块）：把「发现 + 它引用的那段真实代码」交给模型，
   只问一个问题 —— 这段代码能证明这个结论吗？**只判不改、只删不加**。

## 一条不能破的纪律

判官**不得产生新发现**。让判官既能删又能加，等于把同一个角色同时当审查者和
被审者，正是 CR-Bench 说的「审查者与修复者共享盲区」。所以它的输出被强制
约束成对每条发现的 ``keep`` / ``drop`` 判定。

## 失效策略

判官本身失败（网络、解析、超时）时**保留全部发现**并记一条警告。
理由：判官是降低误报的增强项，不是正确性前提。让它失败就把发现全丢掉，
会把一次网络抖动放大成「什么都没查到」。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .guardrails import cross_check_line_content
from .model import Model, ModelError
from .types import Message

# 每条发现随证据送的代码行数上限。给太多行既浪费 token 又稀释注意力；
# ±8 行足够覆盖一条缺陷所在的表达式与紧邻的分支。
CONTEXT_LINES = 8

# 一次最多复核多少条。判官是逐个判，条数直接乘在调用次数上，
# 而真实运行里发现数很少超过 20 条。
MAX_JUDGE_ITEMS = 24

_SYSTEM = """你是一位资深代码评审的**复核人**，任务是判断别人给出的审查发现是否站得住。

规则（必须严格遵守）：
1. 你只做判断，**绝对不要提出新的问题**，也不要改写建议。
2. 判断依据只有两样：发现里写的结论，以及下面贴出的真实代码片段。
3. 只有当这段代码**确实能支撑**这条结论时才判 keep。
4. 以下情况判 drop：
   - 代码里根本没有描述说的那个问题（结论与代码不符）；
   - 代码已经做了防护 / 已修复 / 是测试或注释里的说明；
   - 描述引用的是框架行为但代码里没有任何对应迹象；
   - 结论是纯风格偏好、命名意见、缺少注释之类的**非缺陷**。
5. 拿不准时判 keep —— 你的作用是过滤明显站不住的，不是宁可错杀。
6. **`note` 级（低置信度提示）请特别保守**：这类条目本来就是在指出「边界/前提未被约束」，
   即使代码在数学上「不算错」，只要描述指出的前提确实没有校验，就判 keep。
   只有当描述**指错了代码**时才判 drop。

输出：只输出一个 JSON 对象，形如
{"judgments": [{"id": "F1", "verdict": "keep", "reason": "一句话理由"}]}
不要输出任何其他文字、不要用 markdown 代码块包裹。"""


@dataclass
class Judgment:
    id: str
    verdict: str
    reason: str = ""

    @property
    def kept(self) -> bool:
        return self.verdict != "drop"


@dataclass
class JudgeReport:
    """一次复核的结果，供 run_agent 汇总与 UI 展示。"""

    total: int = 0
    dropped: list[Judgment] = field(default_factory=list)
    # 被判官判 drop、但被保护规则拦下保留的（note 级）
    kept_by_guard: list[Judgment] = field(default_factory=list)
    ungrounded: int = 0
    skipped_reason: str = ""

    def warnings(self) -> list[str]:
        if self.skipped_reason:
            return [self.skipped_reason]
        out: list[str] = []
        if self.dropped:
            names = "；".join(
                f"{j.id} {j.reason}"[:80] for j in self.dropped[:4]
            )
            out.append(
                f"复核阶段删除了 {len(self.dropped)}/{self.total} 条撑不住证据的发现：{names}"
            )
        if self.ungrounded:
            out.append(
                f"另有 {self.ungrounded} 条发现的位置与描述不符，已在核验阶段标注。"
            )
        return out


def _excerpt(root: Path, rel: str, line: int, span: int = CONTEXT_LINES) -> str:
    """取发现所指位置前后各 ``span`` 行，带上真实行号。

    行号必须带上：判官要能验证「你说的第 N 行」确实是它看到的那一行，
    否则它只是在给一段无坐标的文本打分。
    """
    try:
        target = (root / rel).resolve()
        target.relative_to(root.resolve())
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except (ValueError, OSError):
        return ""

    start = max(1, line - span)
    end = min(len(lines), line + span)
    out: list[str] = []
    for no in range(start, end + 1):
        marker = ">>>" if no == line else "   "
        out.append(f"{marker} {no:>4} | {lines[no - 1]}")
    return "\n".join(out)


def _parse_verdicts(raw: str) -> dict[str, Judgment]:
    """从模型输出里抽 ``{"judgments": [...]}``。

    容忍三种实际会遇到的情况：纯 JSON、```json 包裹、前后带解释性文字
    （按第一个 ``{`` 到最后一个 ``}`` 截取）。解析不出来就返回空，
    由上层按「保留全部」处理 —— 不能因为格式问题把发现全判死。
    """
    text = (raw or "").strip()
    if not text:
        return {}

    if "```" in text:
        parts = text.split("```")
        for part in parts:
            cleaned = part.strip()
            if cleaned.startswith("json"):
                cleaned = cleaned[4:].strip()
            if cleaned.startswith("{"):
                text = cleaned
                break

    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}

    items = data.get("judgments") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return {}

    out: dict[str, Judgment] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        fid = str(item.get("id") or "").strip()
        verdict = str(item.get("verdict") or "").strip().lower()
        if not fid or verdict not in ("keep", "drop"):
            continue
        out[fid] = Judgment(id=fid, verdict=verdict, reason=str(item.get("reason") or ""))
    return out


def judge_findings(
    findings: Sequence[dict[str, Any]],
    workspace_root: Path,
    model: Model,
) -> tuple[list[dict[str, Any]], JudgeReport]:
    """复核一批发现，返回 ``(保留的发现, 报告)``。

    调用方负责把返回值写回运行结果。整个过程不改动发现的内容，
    只决定保留还是剔除。
    """
    report = JudgeReport(total=len(findings))
    if not findings:
        return list(findings), report

    # 第一步：确定性核验。位置明显错的直接不进判官 —— 省 token，也避免
    # 判官对着一段无关代码猜。
    survivors: list[dict[str, Any]] = []
    for finding in findings:
        ok, _why = cross_check_line_content(finding, workspace_root)
        if ok:
            survivors.append(finding)
        else:
            report.ungrounded += 1

    if not survivors:
        return [], report

    batch = survivors[:MAX_JUDGE_ITEMS]
    overflow = survivors[MAX_JUDGE_ITEMS:]

    blocks: list[str] = []
    for index, finding in enumerate(batch, start=1):
        fid = str(finding.get("id") or f"F{index}")
        excerpt = _excerpt(
            Path(workspace_root),
            str(finding.get("file") or ""),
            int(finding.get("line") or 0),
        )
        blocks.append(
            "\n".join(
                [
                    f"### 发现 {fid}",
                    f"文件：{finding.get('file')}:{finding.get('line')}",
                    f"严重度：{finding.get('severity')}",
                    f"结论：{finding.get('title')}",
                    f"说明：{str(finding.get('detail') or '')[:600]}",
                    f"证据：{str(finding.get('evidence') or '')[:400]}",
                    "代码片段（>>> 标记被引用的行）：",
                    excerpt or "（读不到该文件，请判 keep）",
                    "",
                ]
            )
        )

    user = "请逐条复核下列发现，判断它们是否站得住：\n\n" + "\n".join(blocks)
    try:
        decision = model.complete(
            [
                Message(role="system", content=_SYSTEM),
                Message(role="user", content=user),
            ],
            temperature=0.0,
        )
    except ModelError as exc:
        report.skipped_reason = (
            f"复核阶段跳过（模型调用失败：{exc.error_type}），发现全部保留。"
        )
        return list(findings), report

    verdicts = _parse_verdicts(decision.content or "")
    if not verdicts:
        report.skipped_reason = "复核阶段跳过（判官输出无法解析），发现全部保留。"
        return list(findings), report

    kept: list[dict[str, Any]] = []
    for index, finding in enumerate(batch, start=1):
        fid = str(finding.get("id") or f"F{index}")
        judgment = verdicts.get(fid)
        if judgment and judgment.verdict == "drop":
            # note 级是低置信度提示，判官容易把它当成「设计选择」删掉。
            # 实测 s5 的两条 note 被删后召回从 100% 掉到 50%，而那两条确实指出了
            # 「边界/前提未约束」——属于标注口径认可的缺陷。所以这里加一道闸：
            # note 级只有在判官明确指出「代码里没有所描述的东西」时才允许删。
            if _is_fragile_drop(finding, judgment):
                report.kept_by_guard.append(judgment)
                kept.append(finding)
                continue
            report.dropped.append(judgment)
            continue
        kept.append(finding)

    # 超出一批上限的照原样保留：没被复核不等于被判错
    kept.extend(overflow)
    return kept, report

# 判官理由里出现这些词，说明它是在说「代码里根本没有这回事」，可以删。
# 否则（比如「这是设计选择」「依赖调用方假设」）更像是口径分歧，不该删 note 级。
_MISSING_CUES = (
    "没有", "不存在", "未出现", "找不到", "并无", "不符", "相反",
    "已有", "已经", "实际是", "不是", "no such", "not present", "already",
)


def _is_fragile_drop(finding: dict[str, Any], judgment: Judgment) -> bool:
    """判断这次 drop 是否该被保护规则拦下。

    只保护 ``note`` 级：``blocker``/``warn`` 是明确缺陷，判官要删得给出硬理由；
    而 note 本身就是「值得提一句」的弱提示，被误删的代价（真实发现丢失）
    比多留一条噪声更高。
    """
    if str(finding.get("severity") or "").lower() != "note":
        return False
    reason = (judgment.reason or "").lower()
    return not any(cue in reason for cue in _MISSING_CUES)

