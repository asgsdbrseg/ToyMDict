# -*- coding: utf-8 -*-
import mimetypes
import os
import sys
from urllib.parse import quote


def safe_url_encode(path: str) -> str:
    """把词典路径 / 词条文本编码为 URL 片段。

    ``quote`` 默认 ``safe='/'``，不会编码正斜杠；而 POSIX 绝对路径必然含
    ``/``，会破坏 ``/mdd/{dict_id}/{path}`` 的单次分段解析（dict_id 被切成
    空串，资源全部 404）。这里显式 ``safe=''`` 把斜杠一并编码，由服务端
    ``unquote`` **一次**还原。

    不再预先 ``unquote``：输入一律是调用方持有的原始明文，预解码会破坏
    文件名中真实存在的 ``%xx`` 字面量，也违背「单次解码」约定。
    """
    if not path:
        return ""
    return quote(path, safe="")


def get_mime_type(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    mime_map = {
        # 文本与脚本
        '.css': 'text/css', '.js': 'application/javascript',
        '.json': 'application/json', '.xml': 'application/xml',
        '.html': 'text/html', '.htm': 'text/html', '.txt': 'text/plain',
        # 图片
        '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
        '.gif': 'image/gif', '.bmp': 'image/bmp', '.webp': 'image/webp',
        '.svg': 'image/svg+xml', '.ico': 'image/x-icon',
        '.tiff': 'image/tiff', '.tif': 'image/tiff',
        # 字体
        '.woff': 'font/woff', '.woff2': 'font/woff2',
        '.ttf': 'font/ttf', '.otf': 'font/otf', '.eot': 'application/vnd.ms-fontobject',
        # 音视频
        '.mp3': 'audio/mpeg', '.ogg': 'audio/ogg', '.wav': 'audio/wav',
        '.m4a': 'audio/mp4', '.aac': 'audio/aac', '.flac': 'audio/flac',
        '.mp4': 'video/mp4', '.webm': 'video/webm', '.avi': 'video/x-msvideo',
    }
    return mime_map.get(ext) or mimetypes.guess_type(filename)[0] or 'application/octet-stream'


def find_mdx_files(folder_path: str) -> list:
    """递归遍历文件夹获取所有 mdx 文件（排序保证导入顺序稳定）"""
    mdx_files = []
    if not os.path.exists(folder_path):
        return mdx_files
    for root, dirs, files in os.walk(folder_path):
        dirs.sort()
        for f in sorted(files):
            if f.lower().endswith('.mdx'):
                mdx_files.append(os.path.join(root, f))
    return mdx_files


def get_app_base_dir() -> str:
    """获取应用基础目录（开发时为项目根目录，打包后为 exe 所在目录）"""
    if getattr(sys, 'frozen', False):
        # PyInstaller 打包后：exe 所在目录
        return os.path.dirname(sys.executable)
    # 开发环境：向上两级（从 utils/ → 根目录）
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_web_dir() -> str:
    """前端静态资源目录（前后端分离后由 ResourceServer 提供 /static/）。"""
    return os.path.join(get_app_base_dir(), "web")


def normalize_resource_path(path: str) -> str:
    """资源路径规范化：小写、正斜杠、去前导 ``/``。

    **不做 URL 解码**：解码统一由 ``MdxResourceResolver.decode_request``
    完成一次，避免 resolve_resource → check_path_safety 的双重 unquote。
    例如：``\\css\\STYLE.CSS`` -> ``css/style.css``
    """
    path = (path or "").replace("\\", "/").lstrip("/")
    return path.lower()


def resolve_case_insensitive(base_dir: str, rel_path: str):
    """在 base_dir 下按大小写不敏感匹配 rel_path（支持多级目录与 \\ / 混用）。
    用于大小写敏感文件系统（Linux）上，外挂资源文件名大小写与 MDX 引用
    不一致时仍能定位到真实文件。找不到返回 None。"""
    rel_path = (rel_path or "").replace("\\", "/").lstrip("/")
    parts = [p for p in rel_path.split("/") if p and p != "."]
    cur = base_dir
    for part in parts:
        if not os.path.isdir(cur):
            return None
        target = part.lower()
        matched = None
        try:
            entries = os.listdir(cur)
        except OSError:
            return None
        for entry in entries:
            if entry.lower() == target:
                matched = entry
                break
        if matched is None:
            return None
        cur = os.path.join(cur, matched)
    return cur if os.path.isfile(cur) else None
