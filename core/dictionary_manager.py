# -*- coding: utf-8 -*-
from core.mdx_wrapper import MdxWrapper
import os
import json

class DictionaryManager:
    def __init__(self):
        self.loaded_dicts: dict[str, MdxWrapper] = {}
        self._variant_handler = None
        self._dict_config_path = None
        self._init_variant_handler()

    def _init_variant_handler(self):
        try:
            from libs.variant_utils import VariantHandler
            from utils.path_helper import get_app_base_dir
            
            base_dir = get_app_base_dir()
            json_path = os.path.join(base_dir, "variants.json")
            if not os.path.exists(json_path):
                print(f"[警告] 未找到异体字映射表: {json_path}，异体字搜索将被禁用")
                return
                
            with open(json_path, 'r', encoding='utf-8') as f:
                variants = json.load(f)
                
            variant_dict = {}
            for key, val in variants.items():
                variant_dict[key] = val
                
            print(f"[异体字] 映射表: {json_path}")
            self._variant_handler = VariantHandler(variant_dict)
            
            # 输出统计信息（在 VariantHandler 构建完成后才能获取准确的字符数）
            rule_count = len(variant_dict)  # 规则组数（JSON 中的顶级键数量）
            char_count = len(self._variant_handler.variant_map)  # 实际覆盖的字符数（构建后）
            print(f"  {rule_count} 组规则, {char_count} 个字符")
        except Exception as e:
            print(f"加载异体字失败: {e}")

    def set_dict_config_path(self, config_path: str):
        """设置词典配置文件路径"""
        self._dict_config_path = config_path

    def _load_dict_config(self) -> dict:
        """加载词典配置文件"""
        if not self._dict_config_path or not os.path.exists(self._dict_config_path):
            return None
        try:
            with open(self._dict_config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"加载词典配置失败: {e}")
            return None

    def _save_dict_config(self, config: dict):
        """保存词典配置文件"""
        if not self._dict_config_path:
            return False
        try:
            with open(self._dict_config_path, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=4)
            return True
        except Exception as e:
            print(f"保存词典配置失败: {e}")
            return False

    def _remove_invalid_paths_from_config(self, invalid_paths: set):
        """从配置文件中删除无效路径"""
        if not invalid_paths:
            return
        
        config = self._load_dict_config()
        if not config:
            return
        
        modified = False
        
        # 从 all_dicts 中删除
        if "all_dicts" in config:
            original_count = len(config["all_dicts"])
            config["all_dicts"] = [
                d for d in config["all_dicts"]
                if os.path.abspath(d["id"]) not in invalid_paths
            ]
            if len(config["all_dicts"]) < original_count:
                modified = True
                print(f"[清理] 从 all_dicts 删除 {original_count - len(config['all_dicts'])} 个无效路径")
        
        # 从 groups 中删除
        if "groups" in config:
            for group_name, paths in config["groups"].items():
                original_count = len(paths)
                config["groups"][group_name] = [
                    p for p in paths
                    if os.path.abspath(p) not in invalid_paths
                ]
                if len(config["groups"][group_name]) < original_count:
                    modified = True
                    print(f"[清理] 从 groups['{group_name}'] 删除 {original_count - len(config['groups'][group_name])} 个无效路径")
        
        # 从 excluded 中删除
        if "excluded" in config:
            original_count = len(config["excluded"])
            config["excluded"] = [
                p for p in config["excluded"]
                if os.path.abspath(p) not in invalid_paths
            ]
            if len(config["excluded"]) < original_count:
                modified = True
                print(f"[清理] 从 excluded 删除 {original_count - len(config['excluded'])} 个无效路径")
        
        if modified:
            self._save_dict_config(config)

    def load_group(self, group_name: str) -> bool:
        """加载指定分组的词典，自动清理无效路径"""
        config = self._load_dict_config()
        if not config or "groups" not in config:
            print(f"分组 '{group_name}' 不存在")
            return False
        
        group_paths = config["groups"].get(group_name, [])
        if not group_paths:
            print(f"分组 '{group_name}' 为空")
            return False
        
        # 加载词典并记录无效路径
        invalid_paths = set()
        loaded_count = 0
        
        for path in group_paths:
            abs_path = os.path.abspath(path)
            if not os.path.exists(abs_path):
                print(f"[无效] 词典文件不存在: {path}")
                invalid_paths.add(abs_path)
                continue
            
            if self.load_mdx(path):
                loaded_count += 1
            else:
                print(f"[失败] 加载词典失败: {path}")
                invalid_paths.add(abs_path)
        
        # 自动清理无效路径
        if invalid_paths:
            self._remove_invalid_paths_from_config(invalid_paths)
        
        print(f"分组 '{group_name}' 加载完成: {loaded_count}/{len(group_paths)} 个词典")
        return loaded_count > 0

    def load_mdx(self, path: str) -> bool:
        abs_path = os.path.abspath(path)
        if abs_path in self.loaded_dicts or not os.path.exists(abs_path):
            return abs_path in self.loaded_dicts
            
        wrapper = MdxWrapper(abs_path)
        if wrapper.load(variant_handler=self._variant_handler):
            self.loaded_dicts[abs_path] = wrapper
            return True
        return False

    def unload_mdx(self, path: str):
        abs_path = os.path.abspath(path)
        if abs_path in self.loaded_dicts:
            self.loaded_dicts[abs_path].close()
            del self.loaded_dicts[abs_path]

    def unload_all_except(self, keep_paths: set):
        to_unload = [p for p in self.loaded_dicts if p not in keep_paths]
        for p in to_unload:
            self.unload_mdx(p)

    def search(self, keyword: str, use_variants: bool) -> list:
        if not self.loaded_dicts or not keyword:
            return []
            
        merged_results = {}
        for path, wrapper in self.loaded_dicts.items():
            for key, idx in wrapper.search(keyword, use_variants):
                if key not in merged_results:
                    merged_results[key] = {"key": key, "sources": []}
                    
                # 修改：以 dict_id + idx 作为去重凭证，避免同名词条被吞并
                if not any(s["dict_id"] == path and s["idx"] == idx for s in merged_results[key]["sources"]):
                    merged_results[key]["sources"].append({
                        "dict_id": path,
                        "dict_name": wrapper.name,
                        "idx": idx  # 新增：保留词条索引
                    })

        results = list(merged_results.values())
        results.sort(key=lambda x: (0 if x["key"] == keyword else 1, len(x["key"])))
        return results

    def get_content(self, dict_id: str, key: str, idx: int = None) -> tuple:
        # 修改：增加 idx 传递
        abs_path = os.path.abspath(dict_id)
        wrapper = self.loaded_dicts.get(abs_path)
        if not wrapper:
            return "", ""
        return wrapper.get_content(key, idx), wrapper.name

    def get_resource(self, dict_id: str, path: str) -> bytes:
        abs_path = os.path.abspath(dict_id)
        wrapper = self.loaded_dicts.get(abs_path)
        if not wrapper:
            return None

        # 修改：统一调用 wrapper.get_resource，内部会自动遍历所有 MDD
        data = wrapper.get_resource(path)
        if data:
            return data

        # 本地文件兜底（MDX 同目录）
        if wrapper.folder_path:
            try:
                from utils.path_helper import normalize_resource_path
                file_path = os.path.join(wrapper.folder_path, normalize_resource_path(path))
                if os.path.isfile(file_path):
                    with open(file_path, 'rb') as f:
                        return f.read()
            except Exception as e:
                pass

        return None
