# -*- coding: utf-8 -*-
import os
import sys
from urllib.parse import quote, unquote

def safe_url_encode(path: str) -> str:
    """处理特殊字符图片路径，防止双重编码"""
    if not path: return ""
    # 防止原本就是 %xx 格式被二次编码，先解码一次还原
    decoded_path = unquote(path)
    return quote(decoded_path)

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
    return mime_map.get(ext, 'application/octet-stream')

def find_mdx_files(folder_path: str) -> list:
    """递归遍历文件夹获取所有 mdx 文件"""
    mdx_files = []
    if not os.path.exists(folder_path):
        return mdx_files
    for root, dirs, files in os.walk(folder_path):
        for f in files:
            if f.lower().endswith('.mdx'):
                mdx_files.append(os.path.join(root, f))
    return mdx_files

def get_app_base_dir() -> str:
    """获取应用基础目录（开发时为项目根目录，打包后为 exe 所在目录）"""
    if getattr(sys, 'frozen', False):
        # PyInstaller 打包后：exe 所在目录
        return os.path.dirname(sys.executable)
    else:
        # 开发环境：向上两级（从 utils/ → 根目录）
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    
def normalize_resource_path(path: str) -> str:
    """统一的资源路径规范化
    
    将路径转换为小写、正斜杠、去前导/
    例如：\\css\\STYLE.CSS -> css/style.css
    """
    from urllib.parse import unquote
    path = unquote(path or "")
    path = path.replace("\\", "/")
    path = path.lstrip("/")
    return path.lower()
