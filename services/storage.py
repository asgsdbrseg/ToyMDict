# -*- coding: utf-8 -*-
import json
import os

CONFIG_FILE = "dict_groups.json"

def load_config():
    """读取配置，返回包含 all_dicts, groups, current_group 的字典"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return {
                    "all_dicts": data.get("all_dicts", []),
                    "groups": data.get("groups", {}),
                    "current_group": data.get("current_group", "")
                }
        except (json.JSONDecodeError, OSError, ValueError) as e:
            print(f"[storage] 加载配置失败，使用默认配置: {e}")
    return {"all_dicts": [], "groups": {}, "current_group": ""}

def save_config(config: dict):
    """保存完整配置"""
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=4)
