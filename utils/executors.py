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
import os
import threading
import traceback

from utils.daemon_executor import DaemonThreadPoolExecutor

logger = logging.getLogger(__name__)


def _elastic_max_workers() -> int:
    """按逻辑 CPU 数弹性决定线程池上限。

    词典搜索以磁盘 I/O + 块解压为主，线程数对齐逻辑核心可以让 I/O 等待
    互相重叠；上下限防止单核机器过少、超多核机器开太多线程推高内存。
    ``ThreadPoolExecutor`` 按需懒创建线程，实际线程数还受每次提交的词典
    数量约束，因此这里只是上限。可用 ``TOYMDICT_MAX_WORKERS`` 覆盖以便调优。
    """
    override = os.environ.get("TOYMDICT_MAX_WORKERS")
    if override:
        try:
            return max(1, int(override))
        except ValueError:
            logger.warning("忽略无效的 TOYMDICT_MAX_WORKERS=%r", override)
    cpu = os.cpu_count() or 4
    return max(2, min(cpu, 16))


MAX_WORKERS = _elastic_max_workers()

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
