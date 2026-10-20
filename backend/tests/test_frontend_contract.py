"""前端契约测试。

这里的断言都很「笨」——读文件、找字符串。但每一条都对应一个真实发生过的
问题，比人工看一眼页面可靠：

1. 界面不许出现假数据（2026-10-19 移除了 mock-data.js 与整套演示回放，
   因为一份「看起来跑完了」的审查很容易被当成真结果）；
2. 每个被 JS 取用的 id 都必须在 HTML 里存在（改了结构忘了改引用，
   表现是某个按钮点了没反应，而且没有报错）；
3. CSS 不许出现 transition: all（会把布局属性也一起过渡，掉帧）。
"""

from __future__ import annotations

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2]
SCRIPTS = FRONTEND / "scripts"
STYLES = FRONTEND / "styles"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_no_mock_data_file_or_reference() -> None:
    """演示数据必须彻底消失，不能只是不再加载。"""
    assert not (SCRIPTS / "mock-data.js").exists(), (
        "mock-data.js 又出现了。界面不应该内置样例数据 —— "
        "一份看起来跑完了的审查，会让人误判系统能力。"
    )

    for path in list(SCRIPTS.glob("*.js")) + [FRONTEND / "index.html"]:
        text = _read(path)
        # 允许在注释里提到它的名字（说明来龙去脉），但不允许真的引用
        assert "MARGIN_DATA" not in text, f"{path.name} 仍在引用 MARGIN_DATA"
        assert not re.search(r'src="[^"]*mock-data\.js"', text), (
            f"{path.name} 仍在加载 mock-data.js"
        )


def test_demo_replay_chain_is_gone() -> None:
    """回放链路的入口都不该留着，否则会有人「顺手接回去」。"""
    app = _read(SCRIPTS / "app.js")
    for symbol in ("playDemo(", "demoPack(", "playRun(", "demoReplies("):
        assert symbol not in app, f"app.js 里还有 {symbol}"


def test_offline_state_is_honest() -> None:
    """连不上后端时必须说明「怎么连上」，而不是渲染一份假结果。

    判据是**字符串字面量**里不许出现「演示」—— 注释里解释「为什么删掉演示」
    是应该保留的，但任何会给用户看的文案都不能再提它。
    """
    app = _read(SCRIPTS / "app.js")
    assert "renderOffline" in app
    assert "harness.server" in app, "未连接面板里应给出启动服务的命令"

    literals = re.findall(r"'((?:[^'\\\n]|\\.)*)'", app)
    offenders = [text for text in literals if "演示" in text]
    assert not offenders, "这些文案还在说「演示」：\n  " + "\n  ".join(offenders)



# settings.html 有自己的脚本，不在工作台的校验范围内 —— 但它的 id 必须存在
PAGE_FOR_SCRIPT = {
    "settings.js": "settings.html",
    "auth.js": "login.html",
}


def test_every_script_id_exists_in_html() -> None:
    """JS 里 getElementById 的每一个 id，对应页面里都得有。

    这类错误没有运行时报错（返回 null 后某处静默失效），
    曾经导致「发送按钮文案不随助手切换」。
    """
    pages = {
        name: set(re.findall(r'id="([^"]+)"', _read(FRONTEND / name)))
        for name in ("index.html",)
    }

    missing: list[str] = []
    for path in SCRIPTS.glob("*.js"):
        page = PAGE_FOR_SCRIPT.get(path.name, "index.html")
        if page not in pages:
            continue
        ids = pages[page]
        for name in re.findall(r"getElementById\('([^']+)'\)", _read(path)):
            if name not in ids:
                missing.append(f"{path.name} → #{name}（{page}）")

    assert not missing, "HTML 里缺少这些 id：\n  " + "\n  ".join(missing)



def test_html_loads_every_stylesheet_and_script() -> None:
    html = _read(FRONTEND / "index.html")
    for sheet in ("tokens.css", "app.css", "views.css"):
        assert sheet in html, f"index.html 没有引入 {sheet}"
        assert (STYLES / sheet).exists(), f"{sheet} 文件不存在"
    for script in ("api.js", "agents.js", "blueprint.js", "live.js", "app.js"):
        assert script in html, f"index.html 没有引入 {script}"


def test_glass_only_on_floating_layers() -> None:
    """玻璃只给浮层（顶栏/输入坞/浮层），内容卡必须是实心。

    整屏都在发光会让层级全平 —— 视线上没有落点。
    """
    app = _read(STYLES / "app.css")
    views = _read(STYLES / "views.css")

    assert "backdrop-filter" in app, "外壳层应该有玻璃"
    for selector in (".topbar {", ".composer__box {", ".pop {"):
        block = app.split(selector, 1)[1].split("}", 1)[0]
        assert "backdrop-filter" in block, f"{selector} 应该是玻璃"

    # 内容卡不许有玻璃
    for selector in (".finding {", ".plan {", ".bp-node {"):
        block = views.split(selector, 1)[1].split("}", 1)[0]
        assert "backdrop-filter" not in block, f"{selector} 是内容卡，不该用玻璃"


def test_no_transition_all() -> None:
    """transition: all 会把布局属性也一起过渡，是掉帧的常见来源。"""
    offenders = []
    for path in STYLES.glob("*.css"):
        for index, line in enumerate(_read(path).splitlines(), start=1):
            if re.search(r"transition:\s*all\b", line):
                offenders.append(f"{path.name}:{index}")
    assert not offenders, "这些地方用了 transition: all：\n  " + "\n  ".join(offenders)


def test_theme_default_is_dark_and_light_is_override() -> None:
    """深色是默认（:root），浅色是同构覆盖（[data-theme="light"]）。"""
    tokens = _read(STYLES / "tokens.css")
    assert ":root {" in tokens
    assert '[data-theme="light"] {' in tokens
    # 浅色块必须重新定义关键表面色，否则只是换了个变量名
    light = tokens.split('[data-theme="light"] {', 1)[1]
    for token in ("--surface:", "--ink:", "--glass-fill:"):
        assert token in light, f"浅色模式缺 {token}"


def test_evidence_report_is_exposed_over_http() -> None:
    """证据核验报告必须进 HTTP 的 final。

    CLI 的 --json 一直有，HTTP 层漏了 —— 结果界面上拿不到这个字段，
    「可精确定位率」只能靠翻 warnings 的数字句子看。
    """
    server = _read(FRONTEND / "backend" / "harness" / "server.py")
    assert '"evidence_report": result.evidence_report or {}' in server


def test_diff_mode_is_wired_through_http() -> None:
    server = _read(FRONTEND / "backend" / "harness" / "server.py")
    assert 'body.get("diff")' in server
    assert "diff_unavailable" in server
    # 拿不到 diff 时必须报错，不能静默退化成全仓审查
    assert "collect_diff" in server


# =============================================================================
# 模型配置：只留三项
# =============================================================================


def test_settings_page_has_only_the_three_fields() -> None:
    """配置中心只该有接模型真正需要的三项（外加可选运行参数）。

    旧版有 6 张卡片、20 多个字段：组织 ID、Top P、seed、频率惩罚、停止序列、
    自定义请求头…… 后端一个都不读。界面答应了一堆它做不到的事。
    """
    html = _read(FRONTEND / "settings.html")
    ids = set(re.findall(r'id="([^"]+)"', html))

    for required in ("baseUrl", "apiKey", "model"):
        assert required in ids, f"缺少必需字段 #{required}"

    # 这些字段后端不读，不该出现在界面上
    for ghost in ("provider", "org", "topP", "seed", "frequency", "presence", "stop", "headers"):
        assert ghost not in ids, f"#{ghost} 是个不落盘的装饰字段，不该留"

    assert "本月用量" not in html, "用量卡片是假数据（写死的 token 数与进度条）"


def test_settings_has_no_demo_branch() -> None:
    """配置中心不许有「演示结果」。

    旧版在 file:// 下点「测试连接」会给出「连接正常 · 3 个模型已启用（演示结果）」，
    点保存会说「已保存（演示，未写入文件）」—— 两句话都在说谎。

    判据是**字符串字面量**：注释里解释「为什么不给假成功」是应该保留的，
    但任何会给用户看的文案都不能这么说。
    """
    script = _read(SCRIPTS / "settings.js")
    literals = re.findall(r"'((?:[^'\\\n]|\\.)*)'", script)
    offenders = [
        text
        for text in literals
        if any(phrase in text for phrase in ("演示", "未发出真实请求", "未写入文件"))
    ]
    assert not offenders, "这些文案还在假装成功：\n  " + "\n  ".join(offenders)


def test_settings_never_prefills_a_fake_api_key() -> None:
    """输入框不许预填占位密钥 —— 用户会以为已经配好了。"""
    html = _read(FRONTEND / "settings.html")
    assert "sk-demo-placeholder" not in html
    key_input = html.split('id="apiKey"', 1)[1].split("/>", 1)[0]
    assert 'value=""' in key_input


def test_no_deepseek_hardcoded_in_model_labels() -> None:
    """模型标签不许硬编码厂商名：换服务后那句「（DeepSeek）」就是错的。

    同样只查字符串字面量 —— 注释里说明这件事不算违规。
    """
    for name in ("__main__.py", "server.py"):
        source = _read(FRONTEND / "backend" / "harness" / name)
        literals = re.findall(r'"((?:[^"\\\n]|\\.)*)"', source)
        offenders = [text for text in literals if "（DeepSeek）" in text]
        assert not offenders, f"{name} 的文案里还硬编码了厂商名：{offenders}"
