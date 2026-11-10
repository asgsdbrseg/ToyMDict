# -*- coding: utf-8 -*-
"""全进程唯一的后台任务线程池。

原先 core / ui 各自创建 8 线程池（共 24 个线程，其中一个还从未使用）。
这里统一为一个共享池，所有后台任务、并行词典搜索都通过 ``submit_task``
提交。

重要约束：**不要在共享池的 worker 内部再提交任务并等待结果**，否则
worker 全部互相等待会造成线程池饥饿死锁。因此并行搜索的编排发生在
pywebview 的 API 调用线程（见 ``ui/window_api.py`` 的 ``search``），
只有「单词典搜索」这一层被提交进池。
"""
import atexit
import logging
import threading
import traceback

from utils.daemon_executor import DaemonThreadPoolExecutor

logger = logging.getLogger(__name__)

MAX_WORKERS = 8

_executor = None
_lock = threading.Lock()


def get_executor() -> DaemonThreadPoolExecutor:
    global _executor
    with _lock:
        if _executor is None:
            _executor = DaemonThreadPoolExecutor(
                max_workers=MAX_WORKERS,
                thread_name_prefix="toymdict",
            )
            logger.debug("共享线程池已创建（max_workers=%d）", MAX_WORKERS)
        return _executor


def _report_future_error(future):
    """Future 异常回调：异常不会静默消失。"""
    try:
        future.result()
    except Exception:
        logger.error("后台任务异常:\n%s", traceback.format_exc())


def submit_task(fn, *args, **kwargs):
    """提交后台任务，自动记录未捕获异常。"""
    future = get_executor().submit(fn, *args, **kwargs)
    future.add_done_callback(_report_future_error)
    return future


def submit_many(fn, items):
    """批量提交，返回 ``[(item, future), ...]``，供 wait(timeout=...) 编排。

    这里不挂异常回调：调用方需要自行 ``future.result()`` 并按超时/过期策略
    处理结果，避免重复打印堆栈。
    """
    executor = get_executor()
    return [(item, executor.submit(fn, item)) for item in items]


@atexit.register
def shutdown_executors():
    executor = _executor
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)
        logger.debug("共享线程池已请求关闭")
