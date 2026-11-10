# -*- coding: utf-8 -*-
"""分组配置持久化：原子写 + 结构校验 + 失败可见。

原实现直接 ``open(..., 'w')`` 覆盖，崩溃会留下半截 JSON；下次启动
``load_config`` 静默回落默认配置，用户分组直接丢失。这里改为临时文件 +
``os.replace`` 原子替换，并在读写失败时记录日志、弹窗告知用户。
"""
import json
import logging
import os
import tempfile

from utils import error_reporter
from utils.path_helper import get_app_base_dir

logger = logging.getLogger(__name__)

CONFIG_FILE = os.path.join(get_app_base_dir(), "dict_groups.json")


def _normalize(data):
    """补齐默认字段并校验类型，避免损坏配置让后续代码崩溃。"""
    if not isinstance(data, dict):
        raise ValueError("配置根节点必须是对象")

    all_dicts = data.get("all_dicts", [])
    groups = data.get("groups", {})
    excluded = data.get("excluded", [])
    current_group = data.get("current_group", "")

    if not isinstance(all_dicts, list):
        all_dicts = []
    if not isinstance(groups, dict):
        groups = {}
    if not isinstance(excluded, list):
        excluded = []
    if not isinstance(current_group, str):
        current_group = ""

    all_dicts = [d for d in all_dicts if isinstance(d, dict) and isinstance(d.get("id"), str)]
    groups = {
        name: [p for p in paths if isinstance(p, str)]
        for name, paths in groups.items()
        if isinstance(name, str) and isinstance(paths, list)
    }
    excluded = [p for p in excluded if isinstance(p, str)]

    return {
        "all_dicts": all_dicts,
        "groups": groups,
        "excluded": excluded,
        "current_group": current_group,
    }


def load_config():
    """读取配置，返回 all_dicts / groups / excluded / current_group。"""
    if not os.path.exists(CONFIG_FILE):
        return _normalize({})
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return _normalize(json.load(f))
    except Exception as exc:
        logger.exception("加载配置失败，使用默认配置")
        error_reporter.report(
            "配置读取失败",
            f"已临时使用默认配置，原文件未被修改：{CONFIG_FILE}",
            exc=exc,
        )
        return _normalize({})


def save_config(config: dict) -> bool:
    """原子写入完整配置；成功返回 True。"""
    try:
        data = _normalize(config)
    except Exception as exc:
        logger.exception("配置结构非法，拒绝写入")
        error_reporter.report("配置保存失败", "配置结构非法，已跳过本次写入", exc=exc)
        return False

    tmp_path = None
    directory = os.path.dirname(CONFIG_FILE) or "."
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=".dict_groups.", suffix=".tmp", dir=directory
        )
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, CONFIG_FILE)
        tmp_path = None
        logger.debug("配置已保存: %s", CONFIG_FILE)
        return True
    except Exception as exc:
        logger.exception("保存配置失败")
        error_reporter.report("配置保存失败", "分组设置可能未写入磁盘", exc=exc)
        return False
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
