# -*- coding: utf-8 -*-
"""本地资源服务：MDD / 外挂资源、词条 iframe、前端静态文件。

前后端分离后，主窗口与词条页都通过 ``/static/`` 加载 CSS / JS，
Python 侧不再内嵌任何前端代码。
"""
import gzip
import io
import logging
import os
import re
from functools import partial
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

from utils import error_reporter
from utils.path_helper import get_app_base_dir, get_mime_type, get_web_dir
from utils.resource_resolver import MdxResourceResolver

logger = logging.getLogger(__name__)

# /static/ 白名单：只允许这些文件，杜绝目录穿越与任意文件读取
_STATIC_FILES = {
    "index.html": "text/html; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
    "app.js": "application/javascript; charset=utf-8",
    "entry.js": "application/javascript; charset=utf-8",
    "entry.css": "text/css; charset=utf-8",
    "custom.css": "text/css; charset=utf-8",
}


class DaemonThreadingHTTPServer(ThreadingHTTPServer):
    """显式固化 daemon_threads / allow_reuse_address。

    http.server.ThreadingHTTPServer 基类已设 ``daemon_threads = True``，
    这里显式声明以避免未来默认值变化导致 ``server_close()`` 等待活跃连接、
    进程退出被挂住。
    """
    daemon_threads = True
    allow_reuse_address = True


class ResourceHandler(BaseHTTPRequestHandler):
    def __init__(self, request, client_address, server, dict_manager=None):
        # dict_manager 作为实例属性绑定，避免多个 ResourceServer 实例
        # 通过类属性互相覆盖。
        self.dict_manager = dict_manager
        super().__init__(request, client_address, server)

    def log_message(self, format, *args):
        logger.debug("HTTP %s - %s", self.address_string(), format % args)

    @staticmethod
    def _should_gzip(mime: str, data: bytes) -> bool:
        if not mime:
            return False
        mt = mime.lower()
        if mt.startswith(("text/", "application/json", "application/javascript", "application/xml")):
            return len(data) >= 256  # 太小的压缩收益不大
        return False

    def _resolve_resource(self, dict_id: str, path: str):
        """统一资源解析：解码与安全校验都在 MdxResourceResolver 内完成一次。"""
        data = MdxResourceResolver.resolve_resource(self.dict_manager, dict_id, path)
        if data:
            return data, get_mime_type(path) or "application/octet-stream"
        return None, None

    def _send_bytes(self, data: bytes, mime: str, *, cache: str = "no-cache",
                    extra_headers: dict = None):
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", cache)
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _send_html(self, html: str, status: int = 200):
        """输出 UTF-8 HTML 响应（词条路由专用，禁止缓存）。"""
        data = html.encode('utf-8')
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    # ==================== 前端静态资源 ====================
    def _handle_static(self, parsed):
        name = parsed.path[len("/static/"):]
        if name not in _STATIC_FILES:
            self.send_error(404, "static file not found")
            return

        if name == "custom.css":
            # 用户自定义样式：每次请求实时读取；文件不存在时返回空样式，
            # 避免浏览器控制台报 404。
            css_path = os.path.join(get_app_base_dir(), "custom.css")
            try:
                with open(css_path, "rb") as f:
                    data = f.read()
            except FileNotFoundError:
                data = b""
            except Exception as exc:
                logger.warning("读取 custom.css 失败: %s", exc)
                data = b""
            self._send_bytes(data, _STATIC_FILES[name], cache="no-store")
            return

        file_path = os.path.join(get_web_dir(), name)
        try:
            with open(file_path, "rb") as f:
                data = f.read()
        except Exception as exc:
            logger.error("静态资源读取失败: %s (%s)", file_path, exc)
            self.send_error(404, "static file not found")
            return
        self._send_bytes(data, _STATIC_FILES[name], cache="no-cache")

    # ==================== 词条 iframe ====================
    def _handle_entry(self, parsed):
        """/entry/{dict_id_urlsafe}?idx=&k=&ifid=  → 词条完整 HTML。

        - dict_id 位于 path 中，由 ``safe_url_encode`` 编码，此处 unquote **一次**；
        - 查询串 ``k`` 已由 ``parse_qs`` 解码，不能再 unquote（否则双重解码）；
        - idx 为词条绝对索引，-1 表示缺失（按 key 精确查找）。
        """
        dict_id = unquote(parsed.path[len("/entry/"):])
        if not dict_id:
            self._send_html("<body><p>参数缺失</p></body>", status=400)
            return
        qs = parse_qs(parsed.query)
        try:
            idx = int(qs.get("idx", ["-1"])[0])
        except ValueError:
            idx = -1
        key = qs.get("k", [""])[0]  # parse_qs 已完成解码，单次解码约定
        ifid = qs.get("ifid", ["dict-iframe-0"])[0]
        if not re.fullmatch(r"[\w-]+", ifid):
            ifid = "dict-iframe-0"

        provider = getattr(self.server, "entry_provider", None)
        if not provider:
            self._send_html("<body><p>词条服务不可用</p></body>", status=503)
            return
        try:
            html = provider(dict_id, key, None if idx < 0 else idx, ifid)
        except Exception as exc:
            logger.exception("构建词条 HTML 失败: %s #%s", dict_id, idx)
            error_reporter.report("词条加载失败", f"{key}（{exc}）", level="warning", exc=exc)
            self._send_html("<body style='color:#c00;padding:16px;'>词条内容构建失败，详见日志。</body>",
                            status=500)
            return
        if html:
            self._send_html(html)
        else:
            self._send_html("<body style='color:#888;padding:16px;'>词条内容加载失败或词典未加载。</body>",
                            status=404)

    def do_GET(self):
        try:
            parsed = urlparse(self.path)

            # ===== 路由 A：/static/{file}（前后端分离） =====
            if parsed.path.startswith("/static/"):
                self._handle_static(parsed)
                return

            # ===== 路由 B：/mdd/{dict_id}/{path:.*} =====
            if parsed.path.startswith("/mdd/"):
                rest = parsed.path[5:]
                parts = rest.split("/", 1)
                if len(parts) == 2:
                    dict_id_raw, path_raw = parts
                    data, mime = self._resolve_resource(dict_id_raw, path_raw)
                    if data:
                        accept_encoding = self.headers.get("Accept-Encoding", "").lower()
                        if "gzip" in accept_encoding and self._should_gzip(mime, data):
                            buf = io.BytesIO()
                            with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6) as gz:
                                gz.write(data)
                            compressed = buf.getvalue()
                            self._send_bytes(
                                compressed, mime,
                                cache="public, max-age=31536000",
                                extra_headers={
                                    "Content-Encoding": "gzip",
                                    "Vary": "Accept-Encoding",
                                },
                            )
                        else:
                            self._send_bytes(data, mime, cache="public, max-age=31536000")
                        return

            # ===== 路由 C：/entry/{dict_id}?idx=&k=&ifid= =====
            if parsed.path.startswith("/entry/"):
                self._handle_entry(parsed)
                return

            self.send_response(404)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
        except Exception as exc:
            logger.exception("资源服务器请求处理失败: %s", self.path)
            error_reporter.report("资源服务错误", self.path, level="warning", exc=exc)
            try:
                self.send_response(500)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
            except Exception:
                pass  # 连接已断开时无法再写响应

    def do_HEAD(self):
        """补 HEAD：浏览器/播放器探测资源时不必下载完整正文。"""
        self.do_GET()


class ResourceServer:
    def __init__(self, dict_manager, port: int = 8765):
        self.dict_manager = dict_manager
        self.port = port
        self.server = None
        self.entry_provider = None

    def set_entry_provider(self, provider):
        """注入词条 HTML 构建回调：provider(dict_id, key, idx, iframe_id) -> str|None"""
        self.entry_provider = provider
        if self.server is not None:
            self.server.entry_provider = provider

    def start(self):
        import threading
        handler = partial(ResourceHandler, dict_manager=self.dict_manager)
        bound_port = None
        for port in range(self.port, self.port + 50):
            try:
                self.server = DaemonThreadingHTTPServer(("localhost", port), handler)
                bound_port = port
                break
            except OSError:
                continue
        if not self.server:
            raise RuntimeError(f"无法在端口 {self.port}-{self.port + 49} 范围内找到可用端口")
        self.port = bound_port
        self.server.entry_provider = self.entry_provider
        threading.Thread(target=self.server.serve_forever, daemon=True,
                         name="resource-server").start()
        logger.info("资源服务器已启动: http://localhost:%d", bound_port)

    def stop(self):
        if self.server:
            server = self.server
            self.server = None
            try:
                server.shutdown()
            finally:
                server.server_close()  # 释放监听 socket
            logger.info("资源服务器已停止")
