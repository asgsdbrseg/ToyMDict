# -*- coding: utf-8 -*-
"""HTML 链接重写器：用 lxml 解析并统一处理 MDX 正文中的资源链接。

替代 window_api.py 中正则替换的方案，优势：
- 覆盖 href / src / background / poster / data / cite / codebase 等所有链接属性
- 正确处理畸形 HTML（属性换行、单双引号混用等）
- 仅对 style 属性与 <style> 块内的 url() / @import 做替换，避免误伤正文文本
- 支持 MDX 专有协议（entry://、sound:// 等）透传给 JS 点击处理
"""
import re
from typing import Optional

try:
    from lxml import html as _lxml_html
    _HAS_LXML = True
except ImportError:  # pragma: no cover
    _HAS_LXML = False


# 需要重写的链接属性（HTML 规范中可指向资源的属性）
_LINK_ATTRS = (
    'href', 'src', 'background', 'poster', 'data', 'cite',
    'codebase', 'classid', 'archive', 'longdesc', 'usemap',
    'action', 'formaction', 'icon', 'manifest',
)

# 保持原样、不做路径处理的 scheme
_PASS_THROUGH_SCHEMES = (
    'http:', 'https:', 'data:', 'javascript:', 'mailto:',
    'tel:', 'ftp:', 'about:', 'blob:',
)

# 由 JS 点击处理的 MDX 专有 scheme，不做路径改写
_MDX_PROTOCOL_SCHEMES = (
    'entry://', 'entryx://', 'sound://', 'source://',
)

# 匹配 CSS url(...)，兼容 url("x") / url('x') / url(x) 三种写法
_CSS_URL_RE = re.compile(
    r'url\(\s*(["\']?)([^"\')\s]+)\1\s*\)',
    re.IGNORECASE,
)

# 匹配 @import "x" / @import 'x' / @import url(x)
_CSS_IMPORT_RE = re.compile(
    r'@import\s+(?:url\(\s*)?(["\']?)([^"\')\s;]+)\1(?:\s*\))?',
    re.IGNORECASE,
)


def _is_mdx_protocol(url: str) -> bool:
    lowered = url.lower()
    return any(lowered.startswith(s) for s in _MDX_PROTOCOL_SCHEMES)


def _is_pass_through(url: str) -> bool:
    lowered = url.lower()
    return any(lowered.startswith(s) for s in _PASS_THROUGH_SCHEMES)


def _rewrite_local_path(url: str) -> str:
    """将本地资源路径规范化：去掉前导 /，使 <base> 能正确拼接。"""
    # 保留 // 开头的协议相对 URL
    if url.startswith('//'):
        return url
    if url.startswith('/'):
        return url[1:]
    return url


def _rewrite_attr_value(url: str) -> str:
    """重写单个链接属性值。"""
    if not url:
        return url
    url = url.strip()
    if _is_mdx_protocol(url) or _is_pass_through(url):
        return url
    # file:// 路径：提取本地部分
    if url.lower().startswith('file://'):
        path = url[7:]
        if path.startswith('//'):
            path = path[1:]
        return _rewrite_local_path(path)
    return _rewrite_local_path(url)


def _rewrite_css_text(css_text: str) -> str:
    """重写 CSS 文本中的 url() 与 @import 路径。"""
    def _url_repl(m):
        quote = m.group(1)
        path = m.group(2)
        if _is_mdx_protocol(path) or _is_pass_through(path):
            return m.group(0)
        if path.lower().startswith('file://'):
            p = path[7:]
            if p.startswith('//'):
                p = p[1:]
            new_path = _rewrite_local_path(p)
        else:
            new_path = _rewrite_local_path(path)
        return f'url({quote}{new_path}{quote})'

    def _import_repl(m):
        quote = m.group(1)
        path = m.group(2)
        if _is_mdx_protocol(path) or _is_pass_through(path):
            return m.group(0)
        if path.lower().startswith('file://'):
            p = path[7:]
            if p.startswith('//'):
                p = p[1:]
            new_path = _rewrite_local_path(p)
        else:
            new_path = _rewrite_local_path(path)
        return f'@import {quote}{new_path}{quote}'

    result = _CSS_URL_RE.sub(_url_repl, css_text)
    result = _CSS_IMPORT_RE.sub(_import_repl, result)
    return result


def rewrite_html_links(raw_html: str) -> str:
    """重写 HTML 中的资源链接（不含 <base> 注入，由调用方负责）。

    Args:
        raw_html: 原始 HTML 字符串

    Returns:
        重写后的 HTML 字符串
    """
    if not raw_html:
        return raw_html

    if not _HAS_LXML:
        return _regex_fallback(raw_html)

    try:
        # 用 div 包裹多根节点的片段，保证不丢失内容
        wrapper = _lxml_html.fragment_fromstring(raw_html, create_parent='div')
    except Exception:
        return _regex_fallback(raw_html)

    # 1) 处理所有链接属性（手动遍历，不依赖 iterlinks 的内置属性白名单）
    for elem in wrapper.iter():
        for attr in _LINK_ATTRS:
            val = elem.get(attr)
            if val:
                new_val = _rewrite_attr_value(val)
                if new_val != val:
                    elem.set(attr, new_val)

    # 2) 处理内联 style 属性中的 url() / @import
    for elem in wrapper.iter():
        style = elem.get('style')
        if style and ('url(' in style.lower() or '@import' in style.lower()):
            new_style = _rewrite_css_text(style)
            if new_style != style:
                elem.set('style', new_style)

    # 3) 处理 <style> 块中的 url() / @import
    for style_elem in wrapper.iter('style'):
        if style_elem.text and ('url(' in style_elem.text.lower() or '@import' in style_elem.text.lower()):
            new_text = _rewrite_css_text(style_elem.text)
            if new_text != style_elem.text:
                style_elem.text = new_text

    # 序列化并提取 wrapper 内部内容，避免引入多余包裹标签
    inner_parts = []
    for child in wrapper:
        inner_parts.append(_lxml_html.tostring(child, encoding='unicode', method='html'))
    # 处理 wrapper 直接持有的文本节点（fragment 开头/结尾的纯文本）
    if wrapper.text:
        inner_parts.insert(0, wrapper.text)
    if wrapper.tail:
        inner_parts.append(wrapper.tail)

    return ''.join(inner_parts)


def _regex_fallback(raw_html: str) -> str:
    """lxml 不可用时的正则回退方案（仅覆盖 src/href 等与 CSS url()）。"""
    attr_pattern = re.compile(
        r'\b(href|src|background|poster|data|cite|codebase|classid|archive|longdesc|usemap|action|formaction|icon|manifest)\s*=\s*(["\'])([^"\']*)\2',
        re.IGNORECASE,
    )

    def _attr_repl(m):
        attr_name = m.group(1)
        quote = m.group(2)
        url = m.group(3)
        new_url = _rewrite_attr_value(url)
        return f'{attr_name}={quote}{new_url}{quote}'

    result = attr_pattern.sub(_attr_repl, raw_html)
    result = _rewrite_css_text(result)
    return result
