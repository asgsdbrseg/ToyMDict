# -*- coding: utf-8 -*-
import os
import gzip
import io
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote
from utils.resource_resolver import MdxResourceResolver
from utils.path_helper import get_mime_type, safe_url_encode
from utils.path_helper import normalize_resource_path


class ResourceHandler(BaseHTTPRequestHandler):
    dict_manager = None

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
            # 转成 bytes
            if isinstance(data, str):
                try:
                    data = data.encode("utf-8")
                except Exception:
                    data = data.encode("gbk", errors="ignore")
            mime = get_mime_type(path) or "application/octet-stream"
            return data, mime
        return None, None

    def do_GET(self):
        try:
            parsed = urlparse(self.path)

            # ===== 新路由 A：/mdd/{dict_id}/{path:.*}（推荐与 <base> 配合） =====
            if parsed.path.startswith("/mdd/"):
                rest = parsed.path[5:]  # 去掉 /mdd/
                parts = rest.split("/", 1)
                if len(parts) == 2:
                    dict_id_raw, path_raw = parts
                    dict_id = unquote(dict_id_raw)
                    path = normalize_resource_path(path_raw)

                    data, mime = self._resolve_resource(dict_id, path)
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

    def start(self):
        import threading
        ResourceHandler.dict_manager = self.dict_manager
        start_port = self.port
        bound_port = None
        for port in range(start_port, start_port + 50):
            try:
                self.server = ThreadingHTTPServer(("localhost", port), ResourceHandler)
                bound_port = port
                break
            except OSError:
                continue
        if not self.server:
            raise RuntimeError(f"无法在端口 {start_port}-{start_port + 49} 范围内找到可用端口")
        self.port = bound_port  # 记录实际绑定端口供 window_api 使用
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        if self.server:
            self.server.shutdown()