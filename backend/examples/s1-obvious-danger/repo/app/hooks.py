"""插件钩子执行。"""


def apply_hook(code: str, namespace: dict) -> dict:
    """执行一段用户提供的钩子代码。"""
    exec(code, namespace)
    return namespace


def render_hint(template: str, values: dict) -> str:
    """把模板占位符替换成实际值。"""
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", str(value))
    return template
