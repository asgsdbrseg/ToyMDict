# -*- coding: utf-8 -*-
"""后端错误的用户可见出口：日志 + 前端弹窗。

任何模块都能 ``report(...)``，无需持有 window 引用。窗口 attach 之前产生
的错误会暂存在队列里，attach 后一次性推送，避免启动早期错误丢失。
"""
import json
import logging
import threading

logger = logging.getLogger(__name__)

_window = None
_pending = []
_lock = threading.Lock()
_MAX_PENDING = 20


def attach(window):
    """在窗口创建完成后注入（main.py 调用）。

    此时页面**尚未加载**，不能在这里 flush——``evaluate_js`` 会失败。
    暂存的提示由前端页面就绪后调用 ``drain_pending()`` 主动拉取。
    """
    global _window
    with _lock:
        _window = window


def drain_pending():
    """取出并清空暂存的提示，供前端页面加载完成后拉取。"""
    with _lock:
        pending = list(_pending)
        _pending.clear()
    return pending


def detach():
    global _window
    with _lock:
        _window = None


def report(title, message, *, level="error", exc=None):
    """记录日志并弹出提示。level 为 error / warning / info。"""
    text = str(message)
    if exc is not None:
        text = f"{text}\n{exc}"

    if level == "error":
        logger.error("%s: %s", title, text, exc_info=exc is not None)
    elif level == "warning":
        logger.warning("%s: %s", title, text)
    else:
        logger.info("%s: %s", title, text)

    with _lock:
        window = _window
        if window is None and len(_pending) < _MAX_PENDING:
            _pending.append((title, text, level))
    if window is not None:
        _send(window, title, text, level)


def _send(window, title, message, level):
    try:
        js = "showError(%s, %s, %s)" % (
            json.dumps(title, ensure_ascii=False),
            json.dumps(message, ensure_ascii=False),
            json.dumps(level),
        )
        window.evaluate_js(js)
    except Exception as exc:
        # 窗口尚未就绪 / 已销毁时不能让业务流程中断；
        # 把提示放回队列，等页面加载后由前端拉取（避免启动期错误丢失）。
        logger.warning("弹窗提示失败（窗口未就绪），已暂存: %s", exc)
        with _lock:
            if len(_pending) < _MAX_PENDING:
                _pending.append((title, message, level))
