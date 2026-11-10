# -*- coding: utf-8 -*-
"""资源请求的唯一解码入口与安全边界校验。

约定：**URL 解码只发生一次**，就在本模块的 ``decode_request``。
下游（``check_path_safety`` / ``normalize_resource_path`` / ``get_resource``）
拿到的都是明文路径，不得再次 ``unquote``；否则 ``%252e%252e`` 之类的
双重编码可绕过 ``..`` 检查。
"""
import os
from urllib.parse import unquote

from utils.path_helper import normalize_resource_path


class MdxResourceResolver:
    """统一资源解析器：单次解码、路径安全检查、路由到 DictionaryManager。"""

    @staticmethod
    def decode_request(dict_id, path):
        """URL → 明文，全链路唯一一次解码。"""
        return unquote(dict_id or ""), unquote(path or "")

    @staticmethod
    def check_path_safety(dict_id, path):
        """校验资源路径是否落在词典所在目录内。

        ``dict_id`` / ``path`` 必须是已解码的明文（本模块不再解码）。
        用 ``os.path.commonpath`` 判断边界，避免 ``str.startswith`` 的
        ``/data/dict`` 放行 ``/data/dict_evil`` 这类前缀陷阱。
        """
        if not dict_id or not path:
            return False

        # 分段判断 '..'：比子串匹配更准确，也不会误伤含 '..' 的合法文件名
        parts = path.replace("\\", "/").split("/")
        if any(part == ".." for part in parts):
            return False

        safe_path = normalize_resource_path(path)
        abs_dict_dir = os.path.dirname(os.path.abspath(dict_id))
        abs_resource = os.path.normpath(os.path.join(abs_dict_dir, safe_path))
        try:
            return os.path.commonpath([abs_dict_dir, abs_resource]) == abs_dict_dir
        except ValueError:
            # Windows 不同盘符 / 混合绝对相对路径会抛 ValueError
            return False

    @staticmethod
    def resolve_resource(dict_manager, dict_id, path):
        """供 ResourceServer 调用：解码一次 → 校验 → 取资源。"""
        decoded_id, decoded_path = MdxResourceResolver.decode_request(dict_id, path)
        if not MdxResourceResolver.check_path_safety(decoded_id, decoded_path):
            return None  # 拦截越界 / 恶意请求
        return dict_manager.get_resource(decoded_id, decoded_path)
