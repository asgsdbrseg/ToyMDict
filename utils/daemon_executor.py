# -*- coding: utf-8 -*-
"""工作线程为 daemon 的线程池……（抽离为公共模块，避免 core 与 ui 各定义一份）。"""
import threading
import concurrent.futures


class DaemonThreadPoolExecutor(concurrent.futures.ThreadPoolExecutor):
    _patch_lock = threading.Lock()

    def _adjust_thread_count(self):
        with self._patch_lock:
            _orig_init = threading.Thread.__init__

            def _daemon_init(self_t, *args, **kwargs):
                kwargs.setdefault("daemon", True)
                _orig_init(self_t, *args, **kwargs)

            threading.Thread.__init__ = _daemon_init
            try:
                super()._adjust_thread_count()
            finally:
                threading.Thread.__init__ = _orig_init