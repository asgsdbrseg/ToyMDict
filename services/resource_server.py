# -*- coding: utf-8 -*-
import os
import gzip
import io
from functools import partial
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote
from utils.resource_resolver import MdxResourceResolver
from utils.path_helper import get_mime_type


class ResourceHandler(BaseHTTPRequestHandler):
    def __init__(self, request, client_address, server, dict_manager=None):
        # dict_manager 作为实例属性绑定，避免多个 ResourceServer 实例
        # 通过类属性互相覆盖（旧实现于 start() 内类级赋值 ResourceHandler.dict_manager）。
        # 词条回调（entry_provider）挂在其 HTTP server 实例上，由 _handle_entry 动态读取。
        self.dict_manager = dict_manager
        super().__init__(request, client_address, server)

    def log_message(self, format, *args):
        pass

    @staticmethod
    def _should_gzip(mime: str, data: bytes) -> bool:
        if not mime:
            return False
        mt = mime.lower()
        # 常见文本类与脚本类
        if mt.startswith(("text/", "application/json", "application/javascript", "application/xml")):
            # 太小的压缩收益不大
            return len(data) >= 256
        return False

    def _resolve_resource(self, dict_id: str, path: str):
        """
        统一资源解析逻辑：
        - 优先从 MDD 获取；
        - 若 MDD 无数据，尝试从 MDX 同目录读取；
        - 返回 (bytes, mime_type) 或 (None, None)
        """
        data = MdxResourceResolver.resolve_resource(self.dict_manager, dict_id, path)
        if data:
            mime = get_mime_type(path) or "application/octet-stream"
            return data, mime
        return None, None

    def _send_html(self, html: str, status: int = 200):
        """输出 UTF-8 HTML 响应（词条路由专用，禁止缓存——内容由词典状态决定）。"""
        data = html.encode('utf-8')
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(data)

    def _handle_entry(self, parsed):
        """/entry/{dict_id_urlsafe}?idx=&k=&ifid=  → 词条完整 HTML（iframe 直连）。

        - dict_id/k 均经 safe_url_encode 编码，此处 unquote 还原；
        - idx 为词条绝对索引，-1 表示缺失（按 key 精确查找）；
        - ifid 为父页面为 iframe 分配的 id，注入 resize 脚本占位符；
        - HTML 构建与缓存由 entry_provider（WindowApi.build_entry_html）负责。
        """
        dict_id = unquote(parsed.path[len("/entry/"):])
        if not dict_id:
            self._send_html("<body><p>参数缺失</p></body>", status=400)
            return
        qs = parse_qs(parsed.query)
        idx_raw = qs.get("idx", ["-1"])[0]
        try:
            idx = int(idx_raw)
        except ValueError:
            idx = -1
        key = unquote(qs.get("k", [""])[0])
        ifid = qs.get("ifid", ["dict-iframe-0"])[0]
        # 只允许字母/数字/下划线/连字符，避免注入其他字符
        import re
        if not re.fullmatch(r"[\w-]+", ifid):
            ifid = "dict-iframe-0"
        # 动态读取当前 provider：允许 start() 之后再注入（main.py 的初始化顺序即如此）
        provider = getattr(self.server, "entry_provider", None)
        if not provider:
            self._send_html("<body><p>词条服务不可用</p></body>", status=503)
            return
        html = provider(dict_id, key, None if idx < 0 else idx, ifid)
        if html:
            self._send_html(html)
        else:
            self._send_html("<body style='color:#888;padding:16px;'>词条内容加载失败或词典未加载。</body>",
                            status=404)

    def do_GET(self):
        try:
            parsed = urlparse(self.path)

            # ===== 路由 A：/mdd/{dict_id}/{path:.*}（推荐与 <base> 配合） =====
            if parsed.path.startswith("/mdd/"):
                rest = parsed.path[5:]  # 去掉 /mdd/
                parts = rest.split("/", 1)
                if len(parts) == 2:
                    dict_id_raw, path_raw = parts

                    data, mime = self._resolve_resource(dict_id_raw, path_raw)
                    if data:
                        if self._should_gzip(mime, data):
                            buf = io.BytesIO()
                            with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6) as gz:
                                gz.write(data)
                            compressed = buf.getvalue()
                            self.send_response(200)
                            self.send_header("Content-Type", mime)
                            self.send_header("Content-Encoding", "gzip")
                            self.send_header("Cache-Control", "public, max-age=31536000")
                            self.send_header("Content-Length", str(len(compressed)))
                            self.send_header('Access-Control-Allow-Origin', '*')
                            self.send_header('Vary', 'Accept-Encoding')
                            self.end_headers()
                            self.wfile.write(compressed)
                            return
                        else:
                            self.send_response(200)
                            self.send_header("Content-Type", mime)
                            self.send_header("Cache-Control", "public, max-age=31536000")
                            self.send_header("Content-Length", str(len(data)))
                            self.send_header('Access-Control-Allow-Origin', '*')
                            self.end_headers()
                            self.wfile.write(data)
                            return


            # ===== 路由 B：/entry/{dict_id}?idx=&k=&ifid= （词条 iframe 直连） =====
            if parsed.path.startswith("/entry/"):
                self._handle_entry(parsed)
                return

            # 未匹配任何路由
            self.send_response(404)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
        except Exception as e:
            print(f"资源服务器错误: {e}")
            self.send_response(500)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()


class ResourceServer:
    def __init__(self, dict_manager, port: int = 8765):
        self.dict_manager = dict_manager
        self.port = port
        self.server = None
        self.entry_provider = None  # WindowApi.build_entry_html，主流程创建 WindowApi 后注入

    def set_entry_provider(self, provider):
        """注入词条 HTML 构建回调：provider(dict_id, key, idx, iframe_id) -> str|None

        回调挂在 ThreadingHTTPServer 实例上（handler 每次请求动态读取），
        因此 start() 前后注入均生效。
        """
        self.entry_provider = provider
        if self.server is not None:
            self.server.entry_provider = provider

    def start(self):
        import threading
        handler = partial(ResourceHandler, dict_manager=self.dict_manager)
        start_port = self.port
        bound_port = None
        for port in range(start_port, start_port + 50):
            try:
                self.server = ThreadingHTTPServer(("localhost", port), handler)
                bound_port = port
                break
            except OSError:
                continue
        if not self.server:
            raise RuntimeError(f"无法在端口 {start_port}-{start_port + 49} 范围内找到可用端口")
        self.port = bound_port  # 记录实际绑定端口供 window_api 使用
        self.server.entry_provider = self.entry_provider  # 挂到 HTTP server 上供 handler 动态读取
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        if self.server:
            self.server.shutdown()