"""安全狩猎工具集：逻辑级安全审计。

本文件只放**狩猎特有**的工具（record_threat / check_authz_surface / list_entrypoints）。
公共读写检索在 ``tools_common``，代码图查询在 ``tools_graph``。

## 这个助手为什么存在

规则引擎只能抓**模式级**问题 —— 写一条正则能识别的：硬编码密钥、``eval``、
``shell=True``、``pickle.loads``。这些东西有明确的文本特征。

**真正危险的是逻辑级问题**，它们没有可匹配的文本模式：

- 越权：``get_order(order_id)`` 不校验 ``order_id`` 属不属于当前用户
- IDOR：按 id 取数据，从不比对归属
- 竞态：读-改-写非原子，或锁的**作用域错了**（锁挂在每请求新建的对象上）
- 业务边界：只校验状态、不校验金额，1 分钱能把大额订单标记为已付

这类缺陷代码「看起来完全正常」—— 没有危险函数，没有可疑字符串。
只有先把**业务规则该是什么**想清楚，再拿代码去比对，才能发现。

实测证据：本项目 8 个靶场 34 条标注里，规则最多覆盖 5 条，其余全在这个层面。

## 一条硬约束：必须给攻击路径

``attack_path`` 是这份产出里最重要的字段。它要回答：
**谁（actor）做什么步骤，最后拿到什么（impact）。**

写不出这条链，说明只是「感觉这里不太对」，那不是发现 ——
和规则误报一样，会消耗评审对系统的信任。
"""

from __future__ import annotations

import re

from .sandbox import CommandRunner, Workspace
from .tools import Risk, Tool, ToolRegistry
from .tools_common import (
    _tool_finish,
    _tool_git_diff,
    _tool_grep,
    _tool_list_files,
    _tool_read_file,
    _tool_read_skill,
    _tool_read_symbols,
    _tool_update_todo,
)
from .types import THREAT_CATEGORIES, RunState, ToolResult


# =============================================================================
# 校验：攻击路径必须可重放
# =============================================================================

#: 攻击路径里必须出现的「可执行动作」信号。
#: 只写「可能被利用」「存在风险」但没有动作词的，一律拒。
_ACTION_SIGNALS = re.compile(
    r"(GET|POST|PUT|DELETE|PATCH|请求|调用|发送|上传|构造|遍历|传入|提交|"
    r"并发|同时|重复|修改|篡改|拦截|重放|注入|绕过|伪造|跳过|"
    r"→|->|=>)"
)

#: 含糊其辞的典型写法。整条路径只有这些，说明没想清楚。
_VAGUE_PATH = re.compile(
    r"^(?:可能|也许|或许|疑似|存在|有|该|此).{0,20}(?:风险|问题|隐患|漏洞|缺陷)[。…\.]*$"
)


def _path_is_concrete(path: str) -> bool:
    """攻击路径是否具体到能重放。

    判据（两条都要满足）：
    ① 不是纯含糊表态（「可能存在风险」）；
    ② 出现至少一个可执行动作词（请求/调用/并发/构造…）。

    为什么不在措辞上做更精细的判断：这个校验的目的是挡住「没想清楚就报」，
    而不是评判文笔。挡住含糊表态已经能过滤掉绝大多数空报。
    """
    text = (path or "").strip()
    if len(text) < 12:
        return False
    if _VAGUE_PATH.match(text) and not _ACTION_SIGNALS.search(text):
        return False
    return bool(_ACTION_SIGNALS.search(text))


# =============================================================================
# 工具：登记威胁
# =============================================================================


def _tool_record_threat(state: RunState) -> Tool:
    def handler(
        title: str,
        file: str,
        line: int,
        category: str,
        attack_path: str,
        impact: str,
        actor: str = "",
        preconditions: str = "",
        variants: list[str] | None = None,
        evidence: str = "",
        overlaps_rule: str = "",
    ) -> ToolResult:
        from .types import Threat

        cat = (category or "").strip().lower()
        if cat not in THREAT_CATEGORIES:
            return ToolResult.failure(
                "InvalidCategory",
                f"category 必须是 {list(THREAT_CATEGORIES)} 之一，收到 `{category}`",
                {"category": category},
                suggestion="；".join(f"{k}={v}" for k, v in THREAT_CATEGORIES.items()),
            )

        # ---- 核心校验 ①：攻击路径必须可重放 ----
        if not _path_is_concrete(attack_path):
            return ToolResult.failure(
                "VagueAttackPath",
                f"attack_path 不够具体：「{(attack_path or '')[:60]}」。"
                "它必须写出**谁做什么、系统发生什么**，多步用 → 连接。",
                {"attack_path": attack_path},
                suggestion="照着能重放的样子写。例如："
                           "「任意已登录用户遍历 order_id → GET /orders/<id> → "
                           "返回他人订单明细」。只写「可能存在越权风险」不算。",
            )

        # ---- 核心校验 ②：必须说清危害 ----
        if len((impact or "").strip()) < 6:
            return ToolResult.failure(
                "MissingImpact",
                "缺少 impact：这个漏洞让人拿到了什么？",
                {"title": title},
                suggestion="写清资产与程度。例如「可读取任意客户的订单明细、地址、手机号」。"
                           "写不出危害通常说明这个「漏洞」其实不成立。",
            )

        # ---- 一致性提示：规则已报的不该在这里重复 ----
        warning = ""
        if overlaps_rule:
            warning = (
                f"\n\n[提示] 你标注它与规则 {overlaps_rule} 重叠。"
                "如果这条完全能被规则抓到，就不该由狩猎助手报 —— 去掉它，"
                "让规则去报，避免同一问题在 PR 上出现两次。"
            )

        variants = [str(v).strip() for v in (variants or []) if str(v).strip()]
        threat = Threat(
            title=title,
            file=file,
            line=line,
            category=cat,
            actor=actor,
            attack_path=attack_path.strip(),
            impact=impact.strip(),
            preconditions=preconditions,
            variants=variants,
            evidence=evidence,
            overlaps_rule=overlaps_rule,
        )
        is_new = state.add_artifact("threat", threat)
        total = len(state.artifacts_of("threat"))
        by_cat: dict[str, int] = {}
        for item in state.artifacts_of("threat"):
            k = getattr(item, "category", "?")
            by_cat[k] = by_cat.get(k, 0) + 1
        summary = f"当前共 {total} 条威胁（" + " / ".join(
            f"{k}={v}" for k, v in sorted(by_cat.items())) + "）"
        if is_new:
            return ToolResult.success(f"已记录 {threat.id}：{title}\n{summary}{warning}")
        return ToolResult.success(f"该威胁已存在（位置+标题重复），未重复记录。\n{summary}")

    return Tool(
        name="record_threat",
        summary="登记一条逻辑级安全威胁（title/category/attack_path/impact 必填）",
        description="""登记一条**逻辑级**安全威胁：越权、竞态、业务边界、密码学误用、状态不一致。

何时用：
- 你已经读懂了「这个系统的业务规则该是什么」，并在代码里找到它没被守住的地方。
- 你能写出完整的攻击链：谁 → 做什么 → 系统发生什么 → 拿到什么。

何时不用：
- 只是看到危险函数（`eval` / `pickle` / `shell=True`）→ 那是规则引擎的活儿，
  规则已经能报，不要重复。
- 只是「感觉这里不太对」但写不出攻击步骤 → 继续读代码，不要记。
- 想报通用代码缺陷（空指针、资源泄漏）→ 用代码审查助手。

边界：
- **`attack_path` 必须可重放**：至少含一个动作词（请求/调用/并发/构造/遍历…）。
  只写「可能存在越权风险」会被拒。
- **`impact` 必须说清拿到了什么**。写不出危害通常说明这个漏洞不成立。
- `actor` 是「谁能触发」：任意已登录用户 / 未认证请求方 / 同租户的其他用户…
- `preconditions` 是「需要什么条件」：并发窗口、特定配置、特定数据状态。
  写清这个，结论才能被复核。
- `variants` 是同一缺陷的其他位置（扩散面）—— 只改一处不解决问题的关键。
- 规则已能报的问题填 `overlaps_rule`，会收到提示让你去掉。

Returns: 记录结果与当前累计条数（按 category 分组）。""",
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "一句话结论，例如「订单详情接口不校验归属」"},
                "file": {"type": "string", "description": "漏洞所在文件（相对路径）"},
                "line": {"type": "integer", "description": "关键行号", "minimum": 1},
                "category": {
                    "type": "string",
                    "enum": list(THREAT_CATEGORIES),
                    "description": "；".join(f"{k}={v}" for k, v in THREAT_CATEGORIES.items()),
                },
                "attack_path": {
                    "type": "string",
                    "description": "**必填且必须可重放**：谁做什么 → 系统发生什么。"
                                   "例如「任意已登录用户遍历 order_id → GET /orders/<id> → "
                                   "返回他人订单明细」",
                },
                "impact": {
                    "type": "string",
                    "description": "**必填**：拿到/改到什么。例如「可读取任意客户的订单明细、"
                                   "地址与手机号」",
                },
                "actor": {"type": "string", "description": "谁能触发（角色或前置身份）"},
                "preconditions": {
                    "type": "string",
                    "description": "触发前提：并发窗口 / 特定配置 / 特定数据状态",
                },
                "variants": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "**数组，每项一个**同类问题的其他位置（如 other.py:42）",
                },
                "evidence": {"type": "string", "description": "读了哪处代码得出这个结论，`file:line`"},
                "overlaps_rule": {
                    "type": "string",
                    "description": "若规则引擎已能报这个问题，填规则 id（如 SEC001），会提示你去掉",
                },
            },
            "required": ["title", "file", "line", "category", "attack_path", "impact"],
        },
        handler=handler,
        risk=Risk.LOW,
        examples=[
            {
                "title": "订单详情接口只按 order_id 取单，不校验归属",
                "file": "app/api.py",
                "line": 71,
                "category": "authz",
                "actor": "任意已登录用户",
                "attack_path": "登录后遍历 order_id → 依次 GET /orders/<id> → 每个返回他人订单明细",
                "impact": "可读取全站任意客户的订单内容、收货地址与手机号",
                "preconditions": "order_id 是自增整数，可枚举",
                "variants": ["app/repository.py:30"],
                "evidence": "app/api.py:71",
            }
        ],
        keywords=("threat", "security", "authz", "race", "威胁", "安全", "越权", "竞态"),
    )


# =============================================================================
# 工具：鉴权面清点（机械扫描，给推理提供靶子）
# =============================================================================


#: 常见框架的路由声明形态。
#:
#: **必须同时认「框架写法」和「自研分发层写法」**。实测教训：靶场用自研的
#: ``handle_*`` 函数 + 一个手写分发表，完全不使用框架装饰器 ——
#: 只认 @route/@app.get 的正则在这个仓库上一条入口都没找到，
#: 而它恰恰是越权漏洞最密集的样本。
#:
#: 自研分发层在真实项目里很常见（尤其是老系统和教学项目），
#: 不认识它等于对这类代码完全失明。
_ROUTE_PATTERNS = (
    # ---- 框架写法 ----
    re.compile(r"@\w*\.(?:route|get|post|put|delete|patch)\s*\(\s*['\"]([^'\"]+)['\"]"),
    re.compile(r"@\w*\.(?:route|get|post|put|delete|patch)"),
    re.compile(r"""\b(?:path|url|re_path)\s*\(\s*['"]([^'"]+)['"]"""),
    re.compile(r"""\b(?:app|router)\.(?:get|post|put|delete|patch|use)\s*\(\s*['"]([^'"]+)['"]"""),
    # ---- 自研分发层 ----
    # 命名约定：handle_xxx / api_xxx / view_xxx / endpoint_xxx
    re.compile(r"^\s*def\s+((?:handle|api|view|endpoint|do)_\w+)\s*\("),
    # 分发表里的路由键："GET /orders": handle_x
    # 必须行首锚定：不锚定的话函数文档字符串里的示例路径
    # （`"""GET /orders/<id> —— 详情页。"""`）也会被当成一条入口。
    re.compile(r"""^\s*['"]\s*(?:GET|POST|PUT|DELETE|PATCH)\s+(/[^'"]*)['"]"""),
)

#: 鉴权信号的常见形态。
_AUTH_SIGNALS = re.compile(
    r"(?:require_|_require|check_perm|has_perm|authorize|authenticate|authenticated|"
    r"current_user|request\.user|@login_required|@permission|@auth|is_admin|"
    r"check_owner|verify_owner|forbidden|unauthorized|\b401\b|\b403\b)",
    re.IGNORECASE,
)


def _tool_check_authz_surface(workspace: Workspace) -> Tool:
    """清点「对外入口」与「有没有鉴权」，找出**同一文件里不一致**的地方。

    这是确定性的，不调用模型。它不解判越权 ——
    它只做一件模型很难自己做完的事：**把一个仓库的所有入口列出来，标注谁有校验、谁没有**。

    为什么有价值：越权漏洞最典型的样子是「同一个文件里，purge 有 require_ops，
    但批量发货没有」。逐个人口人工比对很枯燥，机械扫描一秒出结果。
    """

    def handler(path: str = ".", limit: int = 60) -> ToolResult:
        from .languages import for_path
        from .sandbox import PathEscape

        try:
            target = workspace.resolve(path)
        except PathEscape as exc:
            return ToolResult.failure("PathEscape", str(exc), {"path": path},
                                      suggestion="只能用工作区内的相对路径。")
        if not target.exists():
            return ToolResult.failure("PathNotFound", f"路径不存在：{path}", {"path": path},
                                      suggestion="先 list_files('.') 看目录结构。")

        entries: list[tuple[str, int, str, bool]] = []   # (file, line, name, guarded)
        scanned = 0
        for p in sorted(target.rglob("*")):
            if not p.is_file():
                continue
            lang = for_path(p.name)
            if lang is None or lang.level != "shallow" and lang.id != "python":
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            scanned += 1
            rel = workspace.relative(p)
            lines = text.splitlines()
            # 跳过文档字符串：里面的 `"""GET /orders/<id>"""` 是说明，不是路由。
            # 不跳的话同一个入口会出现两次（def 行 + 文档行），清单噪音翻倍。
            in_doc = False
            for idx, line in enumerate(lines):
                stripped = line.strip()
                triple = chr(34) * 3
                triple_s = chr(39) * 3
                if stripped.count(triple) >= 2 or stripped.count(triple_s) >= 2:
                    continue
                if stripped.startswith(triple) or stripped.startswith(triple_s):
                    in_doc = not in_doc
                    continue
                if in_doc:
                    continue
                if not any(pat.search(line) for pat in _ROUTE_PATTERNS):
                    continue
                # 鉴权窗口必须**限制在本函数内**，不能跨进下一个函数。
                #
                # 实测教训：固定窗口（上下各 6/8 行）会让上一个函数的窗口
                # 盖住下一个函数体里的 `require_ops`，于是「无校验」的入口
                # 被误标成「有校验」—— 越权线索直接漏掉。
                # 反过来也会误判：无校验函数被判成有校验，比漏报更糟，
                # 因为它会让人相信这里已经守住了。
                start = max(0, idx - 6)
                end = idx + 8
                # 往回找上一个 def 作为下界
                for back in range(idx - 1, max(-1, idx - 12), -1):
                    if lines[back].lstrip().startswith(("def ", "async def ", "@")):
                        start = back
                        break
                # 往下找下一个 def 作为上界
                for fwd in range(idx + 1, min(len(lines), idx + 40)):
                    if lines[fwd].lstrip().startswith(("def ", "async def ")):
                        end = fwd
                        break
                window = "\n".join(lines[start:end])
                guarded = bool(_AUTH_SIGNALS.search(window))
                name = line.strip()[:100]
                entries.append((rel, idx + 1, name, guarded))

        if not entries:
            return ToolResult.success(
                f"扫描 {scanned} 个文件，没有识别到路由声明。\n"
                "可能原因：① 这个仓库不是 Web 服务；② 用了本工具不认识的框架写法。\n"
                "这种情况下仍然可以用 grep 搜 'route' / 'handle_' / 'def get_' 找入口。"
            )

        guarded = [e for e in entries if e[3]]
        unguarded = [e for e in entries if not e[3]]

        lines = [
            f"扫描 {scanned} 个源文件，识别到 {len(entries)} 个对外入口：",
            f"  有鉴权信号：{len(guarded)} 个",
            f"  **没有鉴权信号：{len(unguarded)} 个**",
            "",
        ]

        if unguarded:
            lines.append("### 没有鉴权信号的入口（重点核对）")
            lines.append("注意：**这不是「越权漏洞」的结论**。")
            lines.append("它只表示「这个入口附近没看到权限校验代码」——")
            lines.append("校验可能在更外层（中间件、装饰器、网关），也可能根本不存在。")
            lines.append("要确认，需要读这个入口的实现，看它按什么条件决定返回什么数据。")
            lines.append("")
            for rel, ln, name, _ in unguarded[:limit]:
                lines.append(f"- `{rel}:{ln}`  {name}")
            if len(unguarded) > limit:
                lines.append(f"[... 还有 {len(unguarded) - limit} 个]")
            lines.append("")

        # ---- 同文件内不一致：最像越权漏洞的信号 ----
        by_file: dict[str, list[tuple[int, str, bool]]] = {}
        for rel, ln, name, g in entries:
            by_file.setdefault(rel, []).append((ln, name, g))
        inconsistent = {
            f: items for f, items in by_file.items()
            if any(x[2] for x in items) and any(not x[2] for x in items)
        }
        if inconsistent:
            lines.append("### 同一文件里鉴权不一致（最值得先看）")
            lines.append("一个文件里有入口做了校验、有的没做 —— 这通常不是「有意的」，而是漏写。")
            lines.append("")
            for f, items in inconsistent.items():
                lines.append(f"`{f}`：")
                for ln, name, g in items:
                    mark = "有校验" if g else "**无校验**"
                    lines.append(f"  - L{ln} [{mark}] {name[:80]}")
                lines.append("")

        if guarded:
            lines.append("### 有鉴权信号的入口（供对照）")
            for rel, ln, name, _ in guarded[:10]:
                lines.append(f"- `{rel}:{ln}`  {name[:80]}")
            if len(guarded) > 10:
                lines.append(f"[... 还有 {len(guarded) - 10} 个]")

        return ToolResult.success("\n".join(lines))

    return Tool(
        name="check_authz_surface",
        summary="清点对外入口与鉴权信号，找出同文件里不一致的地方",
        description="""列出仓库里的「对外入口」，并标注附近有没有鉴权代码。

何时用：
- 开始做越权审计时，先用它拿到一份客观的入口清单，而不是靠 grep 逐个猜。
- 想快速判断「这个项目的权限校验是不是成体系的」。

何时不用：
- 想直接得出「这里有越权」的结论 → 本工具给不出，必须读实现。
- 想找危险函数 → 用 grep 或让规则引擎跑。

边界：
- **这不是漏洞扫描器**。它只统计「入口附近有没有权限校验代码」，
  校验可能在更外层（中间件/网关），也可能写了但逻辑是错的（如恒返回 True）。
- 只认常见框架的写法（Flask/FastAPI/Django/Express 等），
  自研路由框架可能识别不到。
- 只扫源码不做类型推断，动态注册的路由会漏。

Returns: 入口总数、无鉴权信号清单、同文件内鉴权不一致清单。""",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "扫描范围，默认整个工作区 '.'"},
                "limit": {"type": "integer", "description": "每类最多列多少个，默认 60",
                          "minimum": 1, "maximum": 300},
            },
        },
        handler=handler,
        risk=Risk.LOW,
        examples=[{}, {"path": "app", "limit": 30}],
        keywords=("authz", "route", "endpoint", "鉴权", "入口", "路由", "越权"),
    )


# =============================================================================
# 工具：并发敏感点清点
# =============================================================================


#: 典型的共享可变状态与锁形态。
_SHARED_STATE = re.compile(
    r"(?:self\.\w*(?:store|repo|db|conn|pool|lock|cache|state|_items|_orders)\b"
    r"|\bglobal\s+\w+|\bnonlocal\s+\w+)"
)
_LOCK_USE = re.compile(
    r"(?:\bwith\s+[\w.]*lock|\block\.acquire|\bLock\(\)|\bSemaphore\(|\bRLock\("
    r"|lock_for\(|\bthreading\.Lock|\bMutex)"
)


def _tool_check_concurrency_surface(workspace: Workspace) -> Tool:
    """找出「读-改-写」形态的函数，以及它们有没有加锁。

    和 ``check_authz_surface`` 同一定位：**确定性清点，给推理提供靶子**。
    竞态漏洞的判定（这里到底有没有窗口）仍然要模型读控制流。
    """

    def handler(path: str = ".", limit: int = 50) -> ToolResult:
        try:
            target = workspace.resolve(path)
        except Exception as exc:  # noqa: BLE001 - 路径错误直接报回
            return ToolResult.failure("PathEscape", str(exc), {"path": path})

        # 读-改-写的典型形态：赋值给 self.xxx / dict[key] = ，且右侧引用了同一目标
        read_write = re.compile(
            r"(?:self\.\w+\s*[-+]?=|self\.\w+\[\w+\]\s*[-+]?=|\w+\[\w+\]\s*=\s*\w+\[)"
        )
        findings: list[tuple[str, int, str, bool]] = []
        scanned = 0
        for p in sorted(target.rglob("*.py")):
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            scanned += 1
            rel = workspace.relative(p)
            lines = text.splitlines()
            for idx, line in enumerate(lines):
                if line.lstrip().startswith("#"):
                    continue
                if not (_SHARED_STATE.search(line) and read_write.search(line)):
                    continue
                window = "\n".join(lines[max(0, idx - 8): idx + 10])
                findings.append((rel, idx + 1, line.strip()[:100], bool(_LOCK_USE.search(window))))

        if not findings:
            return ToolResult.success(
                f"扫描 {scanned} 个 Python 文件，没有找到明显的「共享状态读-改-写」形态。\n"
                "这不等于没有竞态：跨函数、跨模块的状态更新可能没被识别到。"
            )

        with_lock = [f for f in findings if f[3]]
        without = [f for f in findings if not f[3]]

        lines = [
            f"扫描 {scanned} 个 Python 文件，找到 {len(findings)} 处共享状态读-改-写：",
            f"  附近有锁：{len(with_lock)} 处",
            f"  **附近没有锁：{len(without)} 处**",
            "",
        ]
        if without:
            lines.append("### 没有锁保护的读-改-写（重点核对）")
            lines.append("注意：**这不是「有竞态」的结论**。需要确认三件事：")
            lines.append("  ① 这个对象是不是**跨请求/跨线程共享**的（每请求新建就没有竞态）")
            lines.append("  ② 锁是不是在更外层加的（调用方持有）")
            lines.append("  ③ 这两步之间有没有可能被别的线程插入")
            lines.append("")
            for rel, ln, code, _ in without[:limit]:
                lines.append(f"- `{rel}:{ln}`  {code}")
            if len(without) > limit:
                lines.append(f"[... 还有 {len(without) - limit} 处]")
        return ToolResult.success("\n".join(lines))

    return Tool(
        name="check_concurrency_surface",
        summary="找出共享状态的读-改-写位置，标注附近有没有锁",
        description="""列出 Python 代码里「读共享状态再写回去」的位置，并标注附近有没有锁。

何时用：
- 做并发审计时，先拿一份候选清单，而不是通读所有代码。
- 想确认「这个项目的锁是不是加在正确的地方」。

何时不用：
- 想直接判定「这里有竞态」→ 本工具给不出，必须确认对象是否跨线程共享。
- 找 asyncio 的 await 交错问题 → 本工具只看锁，不看协程调度。

边界：
- **只做形态匹配**：「读-改-写」+「附近有没有 `lock` 字样」。
  它不判断锁的作用域是否正确（最常见的 bug 恰恰是**锁挂在每请求新建的对象上**，
  有锁等于没锁 —— 这种它识别不出来，必须读代码）。
- 只看 Python。其他语言没有对应的赋值形态识别。

Returns: 候选总数、无锁清单。""",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "扫描范围，默认 '.'"},
                "limit": {"type": "integer", "description": "最多列多少处，默认 50",
                          "minimum": 1, "maximum": 200},
            },
        },
        handler=handler,
        risk=Risk.LOW,
        examples=[{}, {"path": "app"}],
        keywords=("race", "concurrency", "lock", "并发", "竞态", "锁"),
    )


# =============================================================================
# 组装
# =============================================================================

CORE_TOOLS: tuple[str, ...] = (
    "list_files",
    "read_file",
    "grep",
    "git_diff",
    "read_symbols",
    "record_threat",
    "update_todo",
    "finish",
    "discover_tools",
)


def build_hunt_toolbox(workspace: Workspace, state: RunState,
                       runner: CommandRunner) -> ToolRegistry:
    """构建安全狩猎工具箱。

    与其他助手的差别：
    - **没有 record_finding / record_test_case / record_plan_step**（产出是 threat）
    - **没有 apply_patch**：狩猎只报告，不改代码（与架构/计划同一边界）
    - 多了两个机械清点工具：check_authz_surface / check_concurrency_surface
    - 代码图全给：``impact`` 用来判断「这个入口被谁调用」，对越权面很有用
    """
    from .tools import bind_discovery
    from .tools_graph import build_graph_toolbox

    registry = ToolRegistry()

    registry.register(bind_discovery(registry), core=True)
    registry.register(_tool_list_files(workspace), core=True)
    registry.register(_tool_read_file(workspace), core=True)
    registry.register(_tool_grep(workspace), core=True)
    registry.register(_tool_git_diff(workspace, runner), core=True)
    registry.register(_tool_read_symbols(workspace), core=True)
    registry.register(_tool_record_threat(state), core=True)
    registry.register(_tool_update_todo(state), core=True)
    registry.register(_tool_finish(state, outcome="threat_model"), core=True)

    # 按需加载：机械清点是「想起来才用」的
    registry.register(_tool_check_authz_surface(workspace))
    registry.register(_tool_check_concurrency_surface(workspace))
    registry.register(_tool_read_skill(workspace))

    # 代码图：只读查询（狩猎不建索引）
    for tool in build_graph_toolbox(workspace, readonly=True).all():
        if registry.get(tool.name) is None:
            registry.register(tool)

    return registry


__all__ = ["CORE_TOOLS", "build_hunt_toolbox"]
