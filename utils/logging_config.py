# -*- coding: utf-8 -*-
"""全进程统一的日志配置。

模块统一使用 ``logging.getLogger(__name__)``，由 ``setup_logging()`` 在
main() 入口完成一次初始化；其余模块不再直接 print 到 stdout。

日志文件 ``toymdict.log`` 每次启动以 ``mode='w'`` 清空重建，只记录本次运行；
不做轮转、不生成任何备份文件。
"""
import logging
import os
import sys

from utils.path_helper import get_app_base_dir

_CONFIGURED = False


def setup_logging(debug: bool = False, log_to_file: bool = True) -> logging.Logger:
    """初始化根 logger（控制台 + 可选日志文件），可重复调用但只生效一次。"""
    global _CONFIGURED
    if _CONFIGURED:
        return logging.getLogger("toymdict")

    level = logging.DEBUG if debug else logging.INFO
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(formatter)
    root.addHandler(console)

    if log_to_file:
        try:
            log_path = os.path.join(get_app_base_dir(), "toymdict.log")
            # mode='w'：启动时立即截断，只保留本次运行的日志；
            file_handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
            file_handler.setLevel(level)
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except Exception as exc:  # 日志本身失败不能阻止程序启动
            print(f"[日志] 文件日志不可用: {exc}", file=sys.stderr)

    # 第三方库降噪
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("PIL").setLevel(logging.WARNING)

    _CONFIGURED = True
    return logging.getLogger("toymdict")


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
