# -*- coding: utf-8 -*-
"""主窗口 HTML 模板加载器（前后端分离）。

原先 530 行 HTML/CSS/JS 内嵌在这个 Python 文件的三引号字符串中，前端无法
独立 lint / 调试。现在页面本体位于 ``web/index.html``，样式与脚本分别位于
``web/style.css`` / ``web/app.js``，由 ResourceServer 的 ``/static/`` 路由
提供；本模块只负责把运行期才知道的端口注入模板。
"""
import logging
import os

from utils import error_reporter
from utils.path_helper import get_app_base_dir, get_web_dir

logger = logging.getLogger(__name__)

_FALLBACK_HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<title>ToyMDict</title></head><body style="font-family:sans-serif;padding:32px;color:#c00;">
<h2>前端资源缺失</h2><p>未找到 web/index.html，请确认安装完整或重新打包。详见 toymdict.log。</p>
<script>function showError(t,m){var d=document.createElement('pre');d.textContent=(t||'')+'\\n'+(m||'');document.body.appendChild(d);}</script>
</body></html>"""


def _read_web(name: str) -> str:
    with open(os.path.join(get_web_dir(), name), "r", encoding="utf-8") as f:
        return f.read()


def load_index_html(port: int, inline: bool = False) -> str:
    """读取 web/index.html 并注入静态资源基址与 origin。

    ``inline=True`` 时把主窗口的 CSS/JS 内联进 HTML（源码仍保持分离），
    用于个别 webview 后端``load_html`` 不允许加载 http 子资源的环境，
    通过环境变量 ``TOYMDICT_INLINE_FRONTEND=1`` 开启。词条 iframe 本身
    就是 http 页面，始终走静态资源，不受影响。
    """
    origin = f"http://localhost:{port}"
    try:
        html = _read_web("index.html")
    except Exception as exc:
        path = os.path.join(get_web_dir(), "index.html")
        logger.exception("加载前端模板失败: %s", path)
        error_reporter.report("前端资源缺失", f"未找到 {path}", exc=exc)
        return _FALLBACK_HTML

    html = (html
            .replace("__STATIC_BASE__", origin + "/static")
            .replace("__ORIGIN__", origin))
    if not inline:
        return html

    try:
        custom_css = ""
        custom_path = os.path.join(get_app_base_dir(), "custom.css")
        if os.path.isfile(custom_path):
            with open(custom_path, "r", encoding="utf-8") as f:
                custom_css = f.read()
        return (html
                .replace(f'<link rel="stylesheet" href="{origin}/static/style.css">',
                         "<style>\n" + _read_web("style.css") + "\n</style>")
                .replace(f'<link rel="stylesheet" href="{origin}/static/custom.css">',
                         "<style>\n" + custom_css + "\n</style>")
                .replace(f'<script src="{origin}/static/app.js"></script>',
                         "<script>\n" + _read_web("app.js") + "\n</script>"))
    except Exception as exc:
        logger.exception("内联前端资源失败，回退到静态资源模式")
        error_reporter.report("前端资源加载失败", str(exc), level="warning", exc=exc)
        return html
