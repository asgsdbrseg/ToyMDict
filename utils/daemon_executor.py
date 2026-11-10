# -*- coding: utf-8 -*-
"""工作线程为 daemon 的线程池（不修改任何全局状态）。

原实现为了让 worker 变成 daemon，临时替换了 ``threading.Thread.__init__``。
那是进程级全局副作用：patch 窗口内其它库（pywebview / http.server / pynput）
创建的线程同样被强制为 daemon，退出时不会被等待。

本实现只覆写自身的 ``_adjust_thread_count``，在局部显式创建 daemon 线程，
并且**刻意不**把线程登记到 ``concurrent.futures.thread._threads_queues``——
该注册表供解释器退出时的 ``_python_exit`` 逐个 join worker，正是导致进程
退出挂起的根源（CPython 3.9+ 的 ThreadPoolExecutor 默认创建非 daemon 线程
并注册该表）。

若未来 CPython 移除私有 ``_worker``，会记录警告并回退到父类实现，保证可用。
"""
import logging
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import thread as _cf_thread

logger = logging.getLogger(__name__)

_worker = getattr(_cf_thread, "_worker", None)


class DaemonThreadPoolExecutor(ThreadPoolExecutor):
    """线程为 daemon、且不参与解释器退出 join 的 ThreadPoolExecutor。"""

    def _adjust_thread_count(self):
        if _worker is None:  # pragma: no cover - 仅在未来 CPython 变更时触发
            logger.warning(
                "无法获取 concurrent.futures.thread._worker，回退到父类实现"
                "（工作线程可能非 daemon，进程退出可能被阻塞）"
            )
            return super()._adjust_thread_count()

        # 有空闲线程时不必新建（与父类逻辑一致）
        if self._idle_semaphore.acquire(timeout=0):
            return

        # executor 被回收时唤醒 worker，让其退出
        def weakref_cb(_, q=self._work_queue):
            q.put(None)

        num_threads = len(self._threads)
        if num_threads < self._max_workers:
            thread_name = "%s_%d" % (
                self._thread_name_prefix or self,
                num_threads,
            )
            t = threading.Thread(
                name=thread_name,
                target=_worker,
                args=(
                    weakref.ref(self, weakref_cb),
                    self._work_queue,
                    self._initializer,
                    self._initargs,
                ),
                daemon=True,
            )
            t.start()
            self._threads.add(t)
            # 不写入 _threads_queues：避免 _python_exit 在退出时 join 本池线程。
