"""Margin Harness 的 HTTP 服务层（零第三方依赖）。

设计取舍：

1. **只用标准库。** 项目其它部分只依赖 httpx，这里不引入 web 框架 ——
   服务本身也是要被审计的代码，依赖越少越好审，也省掉一套版本兼容问题。
2. **同时托管前端静态文件。** 前端与 API 同源，于是不需要 CORS，
   也绕开了 Chrome 对 `file://` → localhost 的私有网络访问（PNA）拦截。
   前端仍然可以双击 `index.html` 打开，那时走演示数据（见 README/联调说明）。
3. **只绑定 127.0.0.1。** 这是本机开发工具，不做对外监听。

接口一览：

    GET  /api/health              服务与模型可用性
    GET  /api/models              向 provider 拉真实模型列表（带缓存）
    GET  /api/config              当前模型与运行配置（密钥只回掩码）
    POST /api/config              更新配置（白名单字段，落盘到 .env）
    POST /api/config/test         用给定配置发起一次真实探测
    GET  /api/repos               可审计的仓库列表
    POST /api/audit               启动一次审计 → {run_id}
    GET  /api/audit/<id>          运行快照（终态或进行中）
    GET  /api/audit/<id>/stream   事件流（SSE）
    POST /api/audit/<id>/stop     请求中断
    GET  /api/audit/<id>/report   审计报告（Markdown / JSON）
    GET  /api/runs                历史运行列表

SSE 事件类型（`data` 均为 JSON）：

    run          运行元信息
    prefix       静态前缀指纹与 token 估算
    step         一轮决策（前端渲染为计划步骤）
    tool         一次工具调用与结果
    finding      一条审计发现（边录边推）
    retry        模型重试
    compression  上下文压缩
    done         收尾（含计数、用量、告警、报告地址）
    error        运行失败
"""

from __future__ import annotations

import json
import mimetypes
import os
import queue
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .agents import (
    UnknownAgent,
    default_id as default_agent_id,
    get as get_agent,
    manifests,
)
from .config import Config, load_config, parse_env_file
from .loop import resolve_workspace, run_agent
from .model import DeepSeekModel, ModelError
from .offline import HeuristicModel
from .report import render_markdown
from .sandbox import DEFAULT_SKIP_DIRS, PathEscape, Workspace, is_secret_path
from .types import Message

# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #

BACKEND_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BACKEND_ROOT.parent
RUNS_DIR = BACKEND_ROOT / ".runs"

# 「已加入工作区」的目录记录。放在 .runs 下（已 gitignore）——
# 这是本机的使用状态，不是项目配置。
WORKSPACE_FILE = RUNS_DIR / "workspace.json"

# 前端静态文件的扩展名白名单。宁可白名单收窄，也不要让服务顺手把 .env 发出去 ——
# 静态目录包含 backend/，而 backend/.env 里放着真实密钥。
SERVE_SUFFIXES = {
    ".html", ".css", ".js", ".mjs", ".json", ".svg", ".png", ".jpg", ".jpeg",
    ".gif", ".webp", ".ico", ".woff", ".woff2", ".ttf", ".map", ".txt", ".md",
}
# 明确禁止访问的路径片段（命中即 403，不区分大小写）
DENY_PARTS = {
    ".env", ".git", ".workbuddy", ".runs", "__pycache__", "node_modules",
    # 工具缓存：既不该被列进仓库文件数，也不该出现在审计范围里
    ".ruff_cache", ".pytest_cache", ".mypy_cache", ".venv", "venv",
    # 评估标注：虽然它在审计根之外、Agent 的沙箱读不到，
    # 但静态路由也没有理由把它发给浏览器 —— 多一道防线不花钱。
    "labels.json",
}

MAX_STATIC_BYTES = 8 * 1024 * 1024


# --------------------------------------------------------------------------- #
# 运行记录
# --------------------------------------------------------------------------- #


@dataclass
class RunRecord:
    """一次审计运行的状态与事件日志。

    事件同时**追加到 events 列表**并**推给所有订阅者**：
    列表用于「晚连上的客户端补历史」，队列用于「已连上的客户端实时收」。
    两条路都保留，前端才能在切换标签页后不丢内容。
    """

    id: str
    repo: str
    repo_name: str
    goal: str
    model: str
    offline: bool
    allow_high_risk: bool
    # 用哪个 agent 跑。记进记录是因为产出形态由它决定 ——
    # 回放历史时要知道这份结果是发现列表还是架构方案。
    agent: str = field(default_factory=default_agent_id)
    # 本次审查限定的路径（仓库内相对路径）。
    # 记进记录是为了可审计：事后能看出这次审查到底覆盖了哪些文件。
    scope: list[str] = field(default_factory=list)
    # 实际注入给模型的上下文附注（范围提示也拼在这里）。
    context_note: str = ""
    created_at: float = field(default_factory=time.time)
    status: str = "queued"  # queued | running | finished | error | stopped
    stop_requested: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)
    subscribers: list[queue.Queue] = field(default_factory=list)
    final: dict[str, Any] | None = None
    error: str = ""
    finished_at: float | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- 事件 -- #

    def emit(self, kind: str, data: dict[str, Any]) -> None:
        event = {"seq": len(self.events), "type": kind, "at": time.time(), "data": data}
        with self._lock:
            self.events.append(event)
            targets = list(self.subscribers)
        for q in targets:
            try:
                q.put_nowait(event)
            except queue.Full:  # 订阅者太慢：丢事件而不是拖死生产者
                pass

    def subscribe(self) -> tuple[queue.Queue, int]:
        q: queue.Queue = queue.Queue(maxsize=512)
        with self._lock:
            self.subscribers.append(q)
            start = len(self.events)
        return q, start

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def snapshot(self, *, since: int = 0, events: bool = False) -> dict[str, Any]:
        with self._lock:
            payload: dict[str, Any] = {
                "run_id": self.id,
                "repo": self.repo,
                "repo_name": self.repo_name,
                "goal": self.goal,
                "model": self.model,
                "offline": self.offline,
                "allow_high_risk": self.allow_high_risk,
                "agent": self.agent,
                "scope": list(self.scope),
                "context_note": self.context_note,
                "status": self.status,
                "created_at": self.created_at,
                "finished_at": self.finished_at,
                "elapsed": (self.finished_at or time.time()) - self.created_at,
                "event_count": len(self.events),
                "error": self.error,
            }
            if events:
                payload["events"] = [e for e in self.events if e["seq"] >= since]
            if self.final:
                payload["result"] = self.final
        return payload


class RunRegistry:
    """运行记录的内存注册表 + 磁盘归档。"""

    def __init__(self, runs_dir: Path = RUNS_DIR) -> None:
        self._runs: dict[str, RunRecord] = {}
        self._lock = threading.Lock()
        self.runs_dir = runs_dir

    def add(self, record: RunRecord) -> None:
        with self._lock:
            self._runs[record.id] = record

    def get(self, run_id: str) -> RunRecord | None:
        with self._lock:
            return self._runs.get(run_id)

    def list(
        self,
        *,
        repo: str | None = None,
        agent: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """列出运行摘要。

        ``agent`` 过滤放在这里而不是调用方：同一条「某个助手有哪些运行」的规则
        要被会话列表和仓库的 runs 计数共用 —— 分开写必然有一天对不上
        （列表显示 3 条、旁边计数写 5）。
        """
        with self._lock:
            records = list(self._runs.values())
        if repo:
            records = [r for r in records if r.repo == repo]
        if agent:
            records = [r for r in records if r.agent == agent]
        records.sort(key=lambda r: r.created_at, reverse=True)
        return [r.snapshot() for r in records[:limit]]

    def persist(self, record: RunRecord) -> None:
        """把终态写盘，让重启后仍能看到历史。

        持久化失败不能影响运行本身 —— 归档是附加价值，不是核心路径。
        """
        try:
            self.runs_dir.mkdir(parents=True, exist_ok=True)
            target = self.runs_dir / f"{record.id}.json"
            target.write_text(
                json.dumps(record.snapshot(events=True), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def load_archived(self) -> int:
        """启动时把历史归档读回内存，只保留摘要不给 events 占内存。"""
        if not self.runs_dir.is_dir():
            return 0
        loaded = 0
        for path in sorted(self.runs_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue

            # 归档目录里不只有运行：工作区清单 workspace.json 也放在这里。
            # 按「长得像不像一条运行」来筛，而不是按文件名 —— 名字会变，
            # 形状不会。否则侧栏会多出一条空的「未命名任务」。
            if not isinstance(payload, dict):
                continue
            if not payload.get("run_id") and not payload.get("created_at"):
                continue
            record = RunRecord(
                id=str(payload.get("run_id") or path.stem),
                repo=str(payload.get("repo", "")),
                repo_name=str(payload.get("repo_name", "")),
                goal=str(payload.get("goal", "")),
                agent=str(payload.get("agent") or default_agent_id()),
                model=str(payload.get("model", "")),
                offline=bool(payload.get("offline")),
                allow_high_risk=bool(payload.get("allow_high_risk")),
                scope=[str(s) for s in (payload.get("scope") or [])],
                context_note=str(payload.get("context_note") or ""),
                created_at=float(payload.get("created_at") or 0.0),
                status=str(payload.get("status", "finished")),
                finished_at=payload.get("finished_at"),
                final=payload.get("result"),
            )
            record.events = list(payload.get("events") or [])
            with self._lock:
                self._runs.setdefault(record.id, record)
            loaded += 1
        return loaded


REGISTRY = RunRegistry()


# --------------------------------------------------------------------------- #
# 事件序列化（harness 事件 → 前端契约）
# --------------------------------------------------------------------------- #


def _tool_arg(call: Any) -> str:
    """挑一个最像「这次调用在做什么」的参数，给工具卡片当副标题。"""
    args = getattr(call, "arguments", {}) or {}
    for key in ("path", "file", "pattern", "query", "glob", "name", "summary"):
        if key in args and args[key] not in ("", None):
            value = args[key]
            return " ".join(str(v) for v in value) if isinstance(value, list) else str(value)
    if "argv" in args:
        value = args["argv"]
        return " ".join(str(v) for v in value) if isinstance(value, list) else str(value)
    if not args:
        return ""
    return json.dumps(args, ensure_ascii=False)[:72]


def _first_line(text: str, limit: int = 76) -> str:
    for line in text.strip().splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return "（无输出）"


class EventBridge:
    """把 ``run_agent`` 的事件钩子翻译成前端契约。

    之所以在这里翻译而不是让前端解析 harness 内部结构：
    前端不该知道 `ToolResult.render()` 长什么样，接口要挡在中间。
    """

    def __init__(self, record: RunRecord) -> None:
        self.record = record
        self._last_at = time.time()
        self._step_durations: list[float] = []

    def __call__(self, name: str, payload: dict[str, Any]) -> None:
        now = time.time()
        handler = getattr(self, f"_on_{name}", None)
        if handler is not None:
            try:
                handler(payload, now)
            except Exception:  # noqa: BLE001 - 观察者绝不能影响主流程
                pass
        self._last_at = now

    # -- 各事件 -- #

    def _on_start(self, payload, now) -> None:
        self.record.emit("prefix", dict(payload.get("prefix") or {}))

    def _on_decision(self, payload, now) -> None:
        decision = payload["decision"]
        usage = payload.get("usage")
        names = [call.name for call in decision.tool_calls]
        label = "、".join(names) if names else "无工具调用"
        meta = f"{len(names)} 个调用" if names else ""
        if usage and usage.cached_tokens:
            meta += f" · 缓存 {usage.cached_tokens}"
        self.record.emit(
            "step",
            {
                "iteration": payload.get("iteration"),
                "label": label,
                "meta": meta,
                "tools": names,
            },
        )

    def _on_tool(self, payload, now) -> None:
        call = payload["call"]
        result = payload["result"]
        rendered = result.render()
        self.record.emit(
            "tool",
            {
                "name": call.name,
                "arg": _tool_arg(call),
                "ok": bool(result.ok),
                "meta": _first_line(rendered),
                "output": rendered,
                "error_type": result.error_type,
                "suggestion": result.suggestion,
                "truncated": bool(result.truncated),
                "saved_to": result.saved_to,
                "verification": list(payload.get("verification") or []),
                "duration": round(max(now - self._last_at, 0.0), 2),
            },
        )

    def _on_finding(self, payload, now) -> None:
        """审查产出的旧契约。新前端请用 ``artifact`` 事件。"""
        finding = payload["finding"]
        self.record.emit("finding", finding.to_dict())

    def _on_artifact(self, payload, now) -> None:
        """通用产出事件。``kind`` 决定前端渲染成发现卡片还是图上的节点。"""
        item = payload["artifact"]
        data = item.to_dict() if hasattr(item, "to_dict") else dict(item)
        self.record.emit(
            "artifact",
            {"kind": payload.get("kind", ""), "iteration": payload.get("iteration"), **data},
        )

    def _on_retry(self, payload, now) -> None:
        self.record.emit("retry", dict(payload))

    def _on_compression(self, payload, now) -> None:
        self.record.emit(
            "compression",
            {
                "iteration": payload.get("iteration"),
                "stats": payload.get("stats"),
                "approx_tokens": payload.get("approx_tokens"),
            },
        )


# --------------------------------------------------------------------------- #
# 执行线程
# --------------------------------------------------------------------------- #


def _finish(record: RunRecord, *, status: str, final: dict[str, Any] | None, error: str = "") -> None:
    record.status = status
    record.error = error

    if final is not None:
        # 报告地址属于终态的一部分，放进 final 而不是只塞进 done 事件 ——
        # 否则只通过 GET /api/audit/<id> 取结果的客户端（例如历史回放）
        # 就拿不到报告链接，同一份数据出现了两种形状。
        final = {
            **final,
            "report_url": f"/api/audit/{record.id}/report",
            "json_url": f"/api/audit/{record.id}/report?format=json",
        }

    record.final = final
    record.finished_at = time.time()

    if final is not None:
        # 事件里不带产出明细：产出已经逐条推过了，重复推一份纯属浪费带宽
        record.emit(
            "done",
            {k: v for k, v in final.items() if k not in ("findings", "artifacts")},
        )
    elif error:
        record.emit("error", {"message": error})
    REGISTRY.persist(record)


def _worker(record: RunRecord, options: dict[str, Any]) -> None:
    record.status = "running"
    record.emit(
        "run",
        {
            "run_id": record.id,
            "repo": record.repo,
            "repo_name": record.repo_name,
            "goal": record.goal,
            "model": record.model,
            "offline": record.offline,
            "allow_high_risk": record.allow_high_risk,
            "agent": record.agent,
            "outcome": options.get("outcome") or get_agent(record.agent).outcome,
            "scope": list(record.scope),
        },
    )

    config: Config = options["config"]
    try:
        workspace = resolve_workspace(record.repo)
        model = HeuristicModel(agent=record.agent) if record.offline else DeepSeekModel(config)
    except (ModelError, PathEscape, OSError) as exc:
        _finish(record, status="error", final=None, error=str(exc))
        return

    bridge = EventBridge(record)
    started = time.time()
    try:
        result = run_agent(
            goal=record.goal,
            agent=record.agent,
            workspace=workspace,
            model=model,
            config=config,
            context_note=str(options.get("context_note") or ""),
            on_event=bridge,
            allow_high_risk=record.allow_high_risk,
            should_stop=lambda: record.stop_requested,
        )
    except Exception as exc:  # noqa: BLE001 - 任何异常都要变成可读事件，不能让线程静默死掉
        detail = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
        _finish(record, status="error", final=None, error=detail)
        return
    finally:
        close = getattr(model, "close", None)
        if callable(close):
            close()

    counts = result.severity_counts
    final = {
        "agent": result.agent,
        "outcome": result.outcome,
        "artifacts": {
            kind: [item.to_dict() for item in items]
            for kind, items in result.artifacts.items()
        },
        # counts 保持严重级别语义：既有前端按 counts.blocker 渲染。
        # 架构助手没有严重级别，此时三项都是 0 —— 通用计数看 artifact_counts。
        "counts": counts,
        "artifact_counts": result.artifact_counts,
        "stop_reason": result.stop_reason,
        "iterations": result.iterations,
        "elapsed": round(time.time() - started, 2),
        "usage": {
            "prompt_tokens": result.usage.prompt_tokens,
            "completion_tokens": result.usage.completion_tokens,
            "cached_tokens": result.usage.cached_tokens,
            "total_tokens": result.usage.total_tokens,
        },
        "cache_hit_ratio": (
            result.usage.cached_tokens / result.usage.prompt_tokens
            if result.usage.prompt_tokens
            else 0.0
        ),
        # findings 保留：既有前端与评估脚本按这个字段取审查结果
        "findings": [item.to_dict() for item in result.findings],
        "warnings": list(result.warnings),
        "summary": result.summary,
        "prefix": result.prefix_trace,
        "compression_events": list(result.compression_events),
    }
    status = "stopped" if result.stop_reason == "stopped" else "finished"
    _finish(record, status=status, final=final)


def start_run(options: dict[str, Any]) -> RunRecord:
    """建记录、起线程，立刻返回 —— 调用方拿 run_id 去订阅事件流。

    ``agent`` 在这里校验而不是等到线程里：未知 id 若静默回退成审查助手，
    用户会拿到一份跑完了但答非所问的结果，这种错误越早暴露越好。
    """
    repo = str(options["repo"])
    agent = str(options.get("agent") or default_agent_id())
    get_agent(agent)  # 未知 id 抛 UnknownAgent，由 HTTP 层转 400
    record = RunRecord(
        id=uuid.uuid4().hex[:12],
        repo=repo,
        repo_name=Path(repo).name,
        goal=str(options.get("goal") or ""),
        model=str(options.get("model") or options["config"].model),
        offline=bool(options.get("offline")),
        allow_high_risk=bool(options.get("allow_high_risk")),
        agent=agent,
        scope=[str(s) for s in (options.get("scope") or [])],
        context_note=str(options.get("context_note") or ""),
    )
    REGISTRY.add(record)
    thread = threading.Thread(target=_worker, args=(record, options), daemon=True)
    thread.start()
    return record


# --------------------------------------------------------------------------- #
# 仓库与模型探测
# --------------------------------------------------------------------------- #


def _looks_like_repo(path: Path) -> bool:
    if not path.is_dir():
        return False
    if any(part in DENY_PARTS for part in path.parts):
        return False
    markers = (".git", "pyproject.toml", "package.json", "setup.py", "requirements.txt")
    if any((path / marker).exists() for marker in markers):
        return True
    # 没有工程标记时，只要有源码文件也认。
    #
    # 这里必须**递归**找：源码放在子目录（src/、app/、lib/）是常规布局，
    # 只在顶层 glob("*.py") 会把这类仓库误判成「不是仓库」而整个漏掉。
    # 用 rglob 是生成器，next() 命中第一个就停，不会为大仓库做全量遍历。
    for suffix in (".py", ".js", ".ts", ".go", ".java", ".rs"):
        if next(
            (
                p
                for p in path.rglob(f"*{suffix}")
                if not any(part in DENY_PARTS for part in p.parts)
            ),
            None,
        ) is not None:
            return True
    return False


def _load_registered() -> list[Path]:
    """读「已加入工作区」的目录清单。

    存在 ``.runs/workspace.json``（该目录已被 gitignore）—— 这是本机的使用状态，
    不是项目配置，不该入库。
    """
    if not WORKSPACE_FILE.is_file():
        return []
    try:
        data = json.loads(WORKSPACE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []

    roots: list[Path] = []
    for raw in data.get("roots", []):
        try:
            candidate = _resolve_user_path(str(raw))
        except ValueError:
            continue
        if candidate.is_dir():
            roots.append(candidate)
    return roots


def _save_registered(roots: list[Path]) -> None:
    WORKSPACE_FILE.parent.mkdir(parents=True, exist_ok=True)
    WORKSPACE_FILE.write_text(
        json.dumps({"roots": [str(p) for p in roots]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _resolve_user_path(raw: str) -> Path:
    """把用户给出的路径归一化：相对路径按**项目根**解析，然后 resolve。"""
    text = (raw or "").strip().strip('"').strip("'")
    if not text:
        raise ValueError("路径不能为空")
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    return candidate.resolve()


def add_repo(raw_path: str) -> tuple[dict[str, Any] | None, str]:
    """把一个目录加入工作区。返回 ``(条目, 错误信息)``，二者必有其一为空。"""
    try:
        target = _resolve_user_path(raw_path)
    except ValueError as exc:
        return None, str(exc)

    if not target.is_dir():
        return None, f"不是一个已存在的目录：{target}"
    if any(part in DENY_PARTS for part in target.parts):
        return None, "该路径位于被禁止访问的目录内。"
    if target == target.anchor or len(target.parts) <= 1:
        return None, "不能把磁盘根目录加入工作区。"

    if not any(
        p.is_file() and not is_secret_path(p.name)
        for p in target.rglob("*")
        if p.is_file()
    ):
        return None, f"该目录下没有任何可审的文件：{target}"

    roots = _load_registered()
    if target not in roots:
        roots.append(target)
        _save_registered(roots)
    return _repo_entry(target.name, target, source="added"), ""


def remove_repo(raw_path: str) -> tuple[bool, str]:
    """把目录移出工作区。只能移出「手动加入」的，``examples/`` 下的样本不能移。"""
    try:
        target = _resolve_user_path(raw_path)
    except ValueError as exc:
        return False, str(exc)

    roots = _load_registered()
    if target not in roots:
        return False, "该目录不在手动加入的清单里（examples/ 下的样本不能移除）。"
    roots.remove(target)
    _save_registered(roots)
    return True, ""


def list_repo_files(repo_path: str, *, limit: int = 300) -> tuple[list[str], str]:
    """列出某仓库内的可审文件（相对路径），供「审查范围」勾选。

    刻意复用 ``Workspace`` 而不是自己 rglob：密钥文件与点目录的排除规则
    只应该有一处实现，否则筛选器里能看到 `.env`、而 Agent 读不到，两边对不上。
    """
    try:
        target = _resolve_user_path(repo_path)
    except ValueError as exc:
        return [], str(exc)
    if not target.is_dir():
        return [], f"目录不存在：{target}"

    workspace = Workspace(target, max_read_bytes=64)
    files, _truncated = workspace.iter_files(limit=limit)
    return [workspace.relative(p) for p in files], ""


def workspace_candidates() -> list[dict[str, Any]]:
    """列出「可以加入工作区」的候选目录，供前端选择器使用。

    候选来源：项目根下的一级目录（排除被禁目录）。每个都带上是否已在工作区，
    这样前端不必自己合并两份数据。
    """
    added = {str(p) for p in _load_registered()}
    existing = {item["path"] for item in list_repos()}
    candidates: list[dict[str, Any]] = []

    if PROJECT_ROOT.is_dir():
        for child in sorted(PROJECT_ROOT.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if any(part in DENY_PARTS for part in child.parts):
                continue
            resolved = child.resolve()
            candidates.append(
                {
                    "name": child.name,
                    "path": str(resolved),
                    "files": _count_files(resolved),
                    "added": str(resolved) in added,
                    "in_workspace": str(resolved) in existing,
                }
            )
    return candidates


def list_repos(root: Path | None = None, agent: str | None = None) -> list[dict[str, Any]]:
    """列出可审计的仓库。

    两个来源合并：
    1. ``examples/`` 下扫描出来的样本（默认来源，最安全 —— 它绝不会把用户的
       私人目录莫名其妙地列出来）；
    2. 用户手动「加入工作区」的目录，记录在 ``.runs/workspace.json``。

    样本目录里可能有一层 ``repo/`` 作为真正的审计根（标注放在它的外面），
    有就用它 —— 否则会把 ``labels.json`` 一起列进被审范围，等于把答案交给模型。
    """
    base = root or (BACKEND_ROOT / "examples")
    repos: list[dict[str, Any]] = []
    seen: set[str] = set()

    if base.is_dir():
        for child in sorted(base.iterdir()):
            if child.name.startswith((".", "_")):
                continue
            audit_root = child / "repo" if (child / "repo").is_dir() else child
            if not _looks_like_repo(audit_root):
                continue
            seen.add(str(audit_root.resolve()))
            repos.append(_repo_entry(child.name, audit_root, source="examples", agent=agent))

    for extra in _load_registered():
        if str(extra) in seen:
            continue
        seen.add(str(extra))
        repos.append(_repo_entry(extra.name, extra, source="added", agent=agent))

    return repos


def _count_files(root: Path, cap: int = 20000) -> int:
    """数可审文件。规则必须与 ``Workspace.iter_files`` 一致。

    之前这里和「选择审查范围」各写了一套过滤逻辑，结果同一个仓库在候选列表里
    显示 61 个文件、在范围清单里却列出 71 个 —— 两个数字对不上，用户没法信任。
    现在两处共用这一份实现（范围清单直接调 ``Workspace.iter_files``）。
    """
    total = 0
    for item in root.rglob("*"):
        try:
            relative = item.relative_to(root)
        except ValueError:
            continue
        # 只看相对根的部分：根目录自身路径含点目录时不该把整棵树判掉
        if any(
            part.startswith(".") or part in DENY_PARTS or part in DEFAULT_SKIP_DIRS
            for part in relative.parts[:-1]
        ):
            continue
        if not item.is_file():
            continue
        if is_secret_path(item.name):
            continue
        total += 1
        if total >= cap:
            break
    return total


def _repo_entry(
    name: str,
    audit_root: Path,
    *,
    source: str,
    agent: str | None = None,
) -> dict[str, Any]:
    return {
        "id": name,
        "name": name,
        "path": str(audit_root),
        "files": _count_files(audit_root),
        # 计数与会话列表同口径：都按同一个助手过滤，否则两边数字会打架
        "runs": len(REGISTRY.list(repo=str(audit_root), agent=agent, limit=500)),
        "source": source,
        "removable": source == "added",
    }


def _probe_models(config: Config) -> tuple[bool, list[dict[str, Any]], str]:
    """向 provider 要一次模型列表。返回 (ok, models, error)。"""
    import httpx

    try:
        with httpx.Client(
            base_url=config.base_url.rstrip("/"),
            timeout=20.0,
            headers={"Authorization": f"Bearer {config.api_key}"},
        ) as client:
            response = client.get("/models")
            if response.status_code != 200:
                return False, [], f"HTTP {response.status_code}：{response.text[:200]}"
            data = response.json().get("data") or []
    except Exception as exc:  # noqa: BLE001 - 网络问题都要变成可读错误
        return False, [], f"{type(exc).__name__}: {exc}"

    models = []
    for item in data:
        effort = (item.get("effort") or {})
        models.append(
            {
                "id": item.get("id"),
                "name": item.get("name") or item.get("id"),
                "context_window": item.get("context_window"),
                "max_output_tokens": item.get("max_output_tokens"),
                "input_modalities": item.get("input_modalities") or [],
                "effort_levels": effort.get("supported_levels") or [],
                "effort_default": effort.get("default_level"),
            }
        )
    return True, models, ""


_MODELS_CACHE: dict[str, Any] = {"at": 0.0, "payload": None, "key": ""}


def models_cached(config: Config, *, ttl: float = 300.0) -> dict[str, Any]:
    key = f"{config.base_url}|{config.api_key[:6]}"
    now = time.time()
    if (
        _MODELS_CACHE["payload"] is not None
        and _MODELS_CACHE["key"] == key
        and now - _MODELS_CACHE["at"] < ttl
    ):
        return _MODELS_CACHE["payload"]
    ok, models, error = _probe_models(config)
    payload = {"ok": ok, "models": models, "error": error, "at": now}
    if ok:
        _MODELS_CACHE.update(at=now, payload=payload, key=key)
    return payload


# --------------------------------------------------------------------------- #
# 配置读写
# --------------------------------------------------------------------------- #

# 允许通过接口改写的键。白名单是刻意的：配置文件是可执行的部署面，
# 不能让一个 HTTP 请求把任意键写进去。
WRITABLE_KEYS = {
    "DEEPSEEK_MODEL": str,
    "DEEPSEEK_BASE_URL": str,
    "AUDIT_MAX_ITERATIONS": int,
    "AUDIT_CONTEXT_WINDOW": int,
    "AUDIT_REPEAT_LIMIT": int,
    "AUDIT_TEMPERATURE": float,
    "AUDIT_TIMEOUT": float,
    "DEEPSEEK_API_KEY": str,
}

ENV_PATH = BACKEND_ROOT / ".env"


def format_env_value(value: Any) -> str:
    """把配置值格式化成适合写进 ``.env`` 的字符串。

    整数取值的浮点不要写成 ``120.0`` —— ``.env`` 不只是给程序读的，
    用户会打开它直接改；``AUDIT_TIMEOUT=120.0`` 这种写法只会让人怀疑是不是写错了。
    """
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _write_env(updates: dict[str, str]) -> None:
    """就地更新 ``.env``，保留注释与未涉及的键。"""
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.is_file() else []
    remaining = dict(updates)
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                output.append(f"{key}={remaining.pop(key)}")
                continue
        output.append(line)
    for key, value in remaining.items():
        output.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(output) + "\n", encoding="utf-8")


def config_payload(config: Config) -> dict[str, Any]:
    key = config.api_key
    masked = f"{key[:6]}…{key[-4:]}" if len(key) > 12 else ("(未设置)" if not key else "…")
    return {
        "model": config.model,
        "base_url": config.base_url,
        "temperature": config.temperature,
        "timeout": config.timeout,
        "max_iterations": config.max_iterations,
        "context_window": config.context_window,
        "repeat_limit": config.repeat_limit,
        "allow_commands": sorted(config.allow_commands),
        "workspace_root": str(config.workspace_root) if config.workspace_root else "",
        "api_key_masked": masked,
        "api_key_set": bool(key),
        "ready": config.ready,
        "provider": _detect_provider(config.base_url),
    }


def _detect_provider(base_url: str) -> str:
    host = urlparse(base_url).hostname or ""
    if "deepseek" in host:
        return "DeepSeek"
    if "anthropic" in host:
        return "Anthropic"
    if "localhost" in host or "127.0.0.1" in host:
        return "本地"
    return host or "自定义"


# --------------------------------------------------------------------------- #
# HTTP 处理器
# --------------------------------------------------------------------------- #


class Handler(BaseHTTPRequestHandler):
    server_version = "MarginHarness"
    protocol_version = "HTTP/1.1"

    # ---- 基础 ---- #

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        # 静音访问日志：审计运行会产生大量事件轮询，刷屏会淹没真正的诊断信息
        pass

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, status: int = 200, ctype: str = "text/plain; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > 1_000_000:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    # ---- 路由 ---- #

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        try:
            if path.startswith("/api/"):
                self._route_get(path, query)
            else:
                self._serve_static(path)
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            try:
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            except OSError:
                pass

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            self._route_post(parsed.path, self._read_json())
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            try:
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            except OSError:
                pass

    # ---- API: GET ---- #

    def _route_get(self, path: str, query: dict[str, list[str]]) -> None:
        config = load_config()

        if path == "/api/health":
            self._send_json(
                {
                    "ok": True,
                    "service": "margin-harness",
                    "ready": config.ready,
                    "model": config.model,
                    "provider": _detect_provider(config.base_url),
                    "project_root": str(PROJECT_ROOT),
                    "backend_root": str(BACKEND_ROOT),
                }
            )
            return

        if path == "/api/config":
            self._send_json(config_payload(config))
            return

        # agent 清单：前端的助手轨道直接由它渲染，新增 agent 不需要改前端代码
        if path == "/api/agents":
            self._send_json(
                {"agents": manifests(), "default": default_agent_id()}
            )
            return

        if path == "/api/models":
            force = query.get("force", ["0"])[0] not in ("0", "false", "")
            payload = models_cached(config, ttl=0.0 if force else 300.0)
            self._send_json(payload)
            return

        if path == "/api/repos":
            root = query.get("root", [""])[0]
            agent, problem = self._agent_filter(query)
            if problem:
                self._send_json(problem, 400)
                return
            self._send_json(
                {"repos": list_repos(Path(root) if root else None, agent=agent), "root": root}
            )
            return

        if path == "/api/workspace/candidates":
            self._send_json({"candidates": workspace_candidates()})
            return

        if path == "/api/repo-files":
            repo = query.get("repo", [""])[0]
            files, error = list_repo_files(repo)
            if error:
                self._send_json({"error": error}, 400)
                return
            self._send_json({"repo": repo, "files": files})
            return

        if path == "/api/runs":
            repo = query.get("repo", [""])[0]
            agent, problem = self._agent_filter(query)
            if problem:
                self._send_json(problem, 400)
                return
            self._send_json({"runs": REGISTRY.list(repo=repo or None, agent=agent)})
            return

        if path.startswith("/api/audit/"):
            self._route_audit_get(path, query, config)
            return

        self._send_json({"error": "未知接口", "path": path}, 404)

    def _agent_filter(
        self, query: dict[str, list[str]]
    ) -> tuple[str | None, dict[str, Any] | None]:
        """解析 ``?agent=``。返回 ``(过滤值, 错误载荷)``，二者必有其一为 None。

        缺省表示「不过滤」（列出全部助手的运行），但**一旦显式传了就必须有效** ——
        传了一个不存在的 id 却返回全部或空列表，都属于静默回退，
        前端会以为「这个助手确实没有历史」，用户无从分辨。
        """
        raw = str(query.get("agent", [""])[0] or "").strip()
        if not raw:
            return None, None
        try:
            return get_agent(raw).id, None
        except UnknownAgent as exc:
            return None, {
                "error": str(exc),
                "code": "unknown_agent",
                "agents": [item["id"] for item in manifests()],
            }

    def _route_audit_get(self, path: str, query: dict[str, list[str]], config: Config) -> None:
        parts = path.strip("/").split("/")  # api / audit / <id> [/ stream|report]
        if len(parts) < 3:
            self._send_json({"error": "缺少 run_id"}, 400)
            return
        run_id = parts[2]
        action = parts[3] if len(parts) > 3 else ""
        record = REGISTRY.get(run_id)
        if record is None:
            self._send_json({"error": f"没有这次运行：{run_id}"}, 404)
            return

        if action == "":
            since = int(query.get("since", ["0"])[0] or 0)
            want_events = query.get("events", ["0"])[0] not in ("0", "false", "")
            self._send_json(record.snapshot(since=since, events=want_events))
            return

        if action == "stream":
            self._stream(record)
            return

        if action == "report":
            self._serve_report(record, config, query)
            return

        self._send_json({"error": f"未知子路径：{action}"}, 404)

    def _serve_report(self, record: RunRecord, config: Config, query: dict[str, list[str]]) -> None:
        if not record.final:
            self._send_json({"error": "这次运行还没有结果"}, 409)
            return
        fmt = query.get("format", ["md"])[0]
        if fmt == "json":
            self._send_json(record.final)
            return
        if fmt == "download":
            body = json.dumps(record.final, ensure_ascii=False, indent=2)
            self._send_text(body, ctype="application/json; charset=utf-8")
            return
        text = _render_report(record)
        self._send_text(text, ctype="text/markdown; charset=utf-8")

    # ---- API: POST ---- #

    def _route_post(self, path: str, body: dict[str, Any]) -> None:
        if path == "/api/config":
            self._update_config(body)
            return

        if path == "/api/config/test":
            self._test_config(body)
            return

        if path == "/api/audit":
            self._start_audit(body)
            return

        if path == "/api/repos":
            entry, error = add_repo(str(body.get("path") or ""))
            if error:
                self._send_json({"error": error}, 400)
                return
            self._send_json({"repo": entry, "repos": list_repos()}, 201)
            return

        if path == "/api/repos/remove":
            ok, error = remove_repo(str(body.get("path") or ""))
            if not ok:
                self._send_json({"error": error}, 400)
                return
            self._send_json({"removed": True, "repos": list_repos()})
            return

        if path.startswith("/api/audit/") and path.endswith("/stop"):
            run_id = path.strip("/").split("/")[2]
            record = REGISTRY.get(run_id)
            if record is None:
                self._send_json({"error": f"没有这次运行：{run_id}"}, 404)
                return
            record.stop_requested = True
            record.emit("stopping", {})
            self._send_json({"run_id": run_id, "stop_requested": True})
            return

        self._send_json({"error": "未知接口", "path": path}, 404)

    def _update_config(self, body: dict[str, Any]) -> None:
        updates: dict[str, str] = {}
        for key, caster in WRITABLE_KEYS.items():
            if key not in body:
                continue
            raw = body[key]
            if raw in (None, ""):
                if key == "DEEPSEEK_API_KEY":
                    continue  # 空值不覆盖已存的密钥
                continue
            try:
                value = caster(raw)
            except (TypeError, ValueError):
                self._send_json({"error": f"{key} 的值不合法：{raw!r}"}, 400)
                return
            if key == "DEEPSEEK_API_KEY":
                value = str(raw).strip()
                if not value.startswith("sk-"):
                    self._send_json({"error": "API Key 应以 sk- 开头"}, 400)
                    return
                updates[key] = value
            else:
                updates[key] = format_env_value(value)

        if updates:
            _write_env(updates)
        self._send_json({"ok": True, "updated": sorted(updates), "config": config_payload(load_config())})

    def _test_config(self, body: dict[str, Any]) -> None:
        """用「提交上来的值」而不是「已落盘的值」探测 —— 否则用户改完 Key
        点测试连接，测的还是旧 Key，结论会误导人。"""
        config = load_config()
        if body.get("api_key"):
            config.api_key = str(body["api_key"]).strip()
        if body.get("base_url"):
            config.base_url = str(body["base_url"]).strip()
        if body.get("model"):
            config.model = str(body["model"]).strip()

        started = time.time()
        ok, models, error = _probe_models(config)
        self._send_json(
            {
                "ok": ok,
                "latency_ms": round((time.time() - started) * 1000),
                "models": models,
                "model_ids": [m["id"] for m in models],
                "error": error,
                "provider": _detect_provider(config.base_url),
            }
        )

    def _start_audit(self, body: dict[str, Any]) -> None:
        repo_raw = str(body.get("repo") or "").strip()
        if not repo_raw:
            self._send_json({"error": "缺少 repo"}, 400)
            return

        # agent 在这里校验，不静默回退。选了一个不存在的助手却跑出一份
        # 审查结果，比直接报错难发现得多。
        agent_raw = str(body.get("agent") or "").strip() or default_agent_id()
        try:
            spec = get_agent(agent_raw)
        except UnknownAgent as exc:
            self._send_json(
                {
                    "error": str(exc),
                    "code": "unknown_agent",
                    "agents": [item["id"] for item in manifests()],
                },
                400,
            )
            return
        # 相对路径按**项目根**解析，而不是服务进程的 CWD。
        # 服务的 CWD 是实现细节（取决于从哪启动），不该泄漏进接口契约 ——
        # 否则同一个 "backend/examples/x" 在不同启动方式下时灵时不灵。
        repo = Path(repo_raw).expanduser()
        if not repo.is_absolute():
            repo = PROJECT_ROOT / repo
        if not repo.is_dir():
            self._send_json(
                {
                    "error": f"仓库路径不存在或不是目录：{repo}",
                    "hint": "相对路径按项目根解析；也可以用绝对路径，或从 GET /api/repos 取 path 字段。",
                },
                400,
            )
            return

        config = load_config()
        if body.get("model"):
            config.model = str(body["model"]).strip()
        try:
            max_iterations = int(body.get("max_iterations") or 0)
        except (TypeError, ValueError):
            max_iterations = 0
        if max_iterations > 0:
            config.max_iterations = min(max_iterations, 200)
        config.workspace_root = repo.resolve()

        offline = bool(body.get("offline"))
        if not offline and not config.ready:
            self._send_json(
                {
                    "error": "未配置 DEEPSEEK_API_KEY。可以先用离线模式跑通链路，"
                    "或在配置中心填入密钥。",
                    "code": "no_api_key",
                },
                409,
            )
            return

        goal = str(body.get("goal") or "").strip() or spec.default_goal

        # 本次审查的限定范围（用户在输入坞里勾选的文件/目录）。
        # 校验放在服务端：前端传什么都不能让范围越出仓库根。
        scope_note = ""
        raw_scope = body.get("scope")
        if isinstance(raw_scope, list) and raw_scope:
            workspace = Workspace(repo)
            accepted: list[str] = []
            rejected: list[str] = []
            for item in raw_scope[:200]:
                try:
                    resolved = workspace.resolve(str(item))
                except PathEscape:
                    rejected.append(str(item))
                    continue
                accepted.append(workspace.relative(resolved))
            if accepted:
                scope_note = (
                    "本次任务**限定在以下路径**，其余文件可跳过（但若发现跨文件的因果关系，"
                    "可以扩大范围并说明理由）：\n"
                    + "\n".join(f"- {path}" for path in accepted)
                )
            if rejected:
                # 只报个数，不回显路径本身：被拒的路径在仓库之外，
                # 没有任何理由把它写进模型上下文（原始值已留在运行记录里可查）。
                scope_note = (
                    (scope_note + "\n\n" if scope_note else "")
                    + f"（另有 {len(rejected)} 个路径超出仓库范围，已忽略。）"
                )

        context_note = str(body.get("context") or "").strip()
        if scope_note:
            context_note = (context_note + "\n\n" + scope_note).strip()

        record = start_run(
            {
                "repo": str(repo.resolve()),
                "goal": goal,
                "offline": offline,
                "allow_high_risk": bool(body.get("allow_high_risk")),
                "agent": spec.id,
                # 事件流里要能自证「这次产出的是哪种形态」—— 回放时没有第二处
                # 可以再告诉你，缺了它渲染层只能猜。
                "outcome": spec.outcome,
                "context_note": context_note,
                "scope": [str(s) for s in (raw_scope or [])][:200] if isinstance(raw_scope, list) else [],
                "config": config,
            }
        )
        self._send_json(
            {
                "run_id": record.id,
                "agent": record.agent,
                "outcome": spec.outcome,
                "stream": f"/api/audit/{record.id}/stream",
                "snapshot": f"/api/audit/{record.id}",
            },
            201,
        )

    # ---- SSE ---- #

    def _stream(self, record: RunRecord) -> None:
        """事件流。

        先补发历史事件（``since`` 之后的），再实时推 —— 这样前端刷新或
        迟到连上都不会丢内容。用分块传输，因为是 HTTP/1.1，必须显式分块。
        """
        since = 0
        header = self.headers.get("Last-Event-ID")
        if header and header.isdigit():
            since = int(header) + 1
        query = parse_qs(urlparse(self.path).query)
        if "since" in query:
            try:
                since = max(since, int(query["since"][0]))
            except (ValueError, IndexError):
                pass

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        q, _ = record.subscribe()
        try:
            for event in list(record.events[since:]):
                if not self._write_event(event):
                    return
            if record.status in ("finished", "error", "stopped"):
                self._write_chunk(b"event: close\ndata: {}\n\n")
                return

            while True:
                try:
                    event = q.get(timeout=15.0)
                except queue.Empty:
                    # 心跳：既保活连接，也让客户端能区分「还在跑」和「断了」
                    if not self._write_chunk(b": ping\n\n"):
                        return
                    if record.status in ("finished", "error", "stopped"):
                        continue
                    continue
                if not self._write_event(event):
                    return
                if event["type"] in ("done", "error"):
                    self._write_chunk(b"event: close\ndata: {}\n\n")
                    return
        finally:
            record.unsubscribe(q)

    def _write_event(self, event: dict[str, Any]) -> bool:
        payload = json.dumps(event["data"], ensure_ascii=False, default=str)
        frame = f"id: {event['seq']}\nevent: {event['type']}\ndata: {payload}\n\n"
        return self._write_chunk(frame.encode("utf-8"))

    def _write_chunk(self, chunk: bytes) -> bool:
        try:
            self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    # ---- 静态文件 ---- #

    def _serve_static(self, path: str) -> None:
        relative = path.lstrip("/") or "index.html"
        if relative.endswith("/"):
            relative += "index.html"

        parts = [p for p in Path(relative).parts if p not in ("", ".")]
        if any(p in DENY_PARTS or p.startswith(".") for p in parts):
            self._send_text("403 拒绝访问该路径", 403)
            return
        if Path(parts[-1]).suffix.lower() not in SERVE_SUFFIXES:
            self._send_text("403 该类型文件不对外提供", 403)
            return

        target = (PROJECT_ROOT / Path(*parts)).resolve()
        try:
            target.relative_to(PROJECT_ROOT)
        except ValueError:
            self._send_text("403 路径越界", 403)
            return

        if not target.is_file():
            self._send_text(f"404 找不到 {relative}", 404)
            return
        if target.stat().st_size > MAX_STATIC_BYTES:
            self._send_text("413 文件过大", 413)
            return

        ctype, _ = mimetypes.guess_type(str(target))
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)


def _render_report(record: RunRecord) -> str:
    """从终态数据重放出一份 Markdown 报告。

    刻意不缓存渲染结果：报告是终态的纯函数，重算比维护两处一致性便宜。
    """
    from .loop import AuditResult
    from .types import Usage, revive_artifacts

    final = record.final or {}
    usage = final.get("usage") or {}
    # 每个桶都要还原，不能只还原 finding：
    # 只还原 finding 会让架构报告渲染出「本次没有登记分层」，那是回放丢了一半产出。
    artifacts = revive_artifacts(final.get("artifacts") or {})
    if not artifacts and final.get("findings"):
        # 改造前落盘的历史记录没有 artifacts 字段
        artifacts = revive_artifacts({"finding": final["findings"]})
    result = AuditResult(
        goal=record.goal,
        agent=final.get("agent") or record.agent,
        artifacts=artifacts,
        outcome=final.get("outcome") or "findings",
        summary=final.get("summary") or "",
        iterations=final.get("iterations") or 0,
        stop_reason=final.get("stop_reason") or "",
        usage=Usage(
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            cached_tokens=usage.get("cached_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
        ),
        warnings=list(final.get("warnings") or []),
        elapsed=final.get("elapsed") or 0.0,
    )
    label = "heuristic-offline（规则驱动）" if record.offline else f"{record.model}（DeepSeek）"
    return render_markdown(result, repo=record.repo, model=label)


# --------------------------------------------------------------------------- #
# 启动
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="harness.server", description="Margin Harness HTTP 服务")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认只绑本机）")
    parser.add_argument("--port", type=int, default=8765, help="监听端口")
    parser.add_argument("--open", action="store_true", help="启动后打开浏览器")
    parser.add_argument("--runs-dir", default=str(RUNS_DIR), help="历史运行归档目录")
    args = parser.parse_args(argv)

    REGISTRY.runs_dir = Path(args.runs_dir)
    restored = REGISTRY.load_archived()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True

    config = load_config()
    url = f"http://{args.host}:{args.port}/"
    print("─" * 64)
    print("  Margin Harness · 代码审计")
    print("─" * 64)
    print(f"  控制台   {url}")
    print(f"  模型     {config.model}（{'已配置密钥' if config.ready else '未配置密钥，仅可离线运行'}）")
    print(f"  历史运行 已载入 {restored} 条")
    print(f"  项目根   {PROJECT_ROOT}")
    print("  按 Ctrl+C 停止")
    print("─" * 64)

    if args.open:
        import webbrowser

        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
