# -*- coding: utf-8 -*-
from core.mdx_wrapper import MdxWrapper
import os
import json

class DictionaryManager:
    def __init__(self):
        self.loaded_dicts: dict[str, MdxWrapper] = {}  # 绝对路径 -> 实例
        self._variant_handler = None
        # 修复点1：程序启动时直接初始化异体字
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
            print(f"  {rule_count} 组规则,  {char_count} 个字符")
        except Exception as e:
            print(f"加载异体字失败: {e}")


    def load_mdx(self, path: str) -> bool:
        abs_path = os.path.abspath(path)
        if abs_path in self.loaded_dicts or not os.path.exists(abs_path):
            return abs_path in self.loaded_dicts

        wrapper = MdxWrapper(abs_path)
        if wrapper.load(variant_handler=self._variant_handler):
            self.loaded_dicts[abs_path] = wrapper
            # 注意：不再单独打印 "[加载词典]" 行，因为 MdxWrapper.load() 已输出详细信息
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
        if not self.loaded_dicts or not keyword: return []
        merged_results = {}
        for path, wrapper in self.loaded_dicts.items():
            for key, idx in wrapper.search(keyword, use_variants):
                if key not in merged_results:
                    merged_results[key] = {"key": key, "sources": []}
                if not any(s["dict_id"] == path for s in merged_results[key]["sources"]):
                    merged_results[key]["sources"].append({
                        "dict_id": path,
                        "dict_name": wrapper.name
                    })
        results = list(merged_results.values())
        results.sort(key=lambda x: (0 if x["key"] == keyword else 1, len(x["key"])))
        return results

    def get_content(self, dict_id: str, key: str, idx: int = None) -> tuple:
        abs_path = os.path.abspath(dict_id)
        wrapper = self.loaded_dicts.get(abs_path)
        if not wrapper: return "", ""
        return wrapper.get_content(key, idx), wrapper.name

    def get_resource(self, dict_id: str, path: str) -> bytes:
        abs_path = os.path.abspath(dict_id)
        if abs_path not in self.loaded_dicts: return None
        wrapper = self.loaded_dicts[abs_path]
        data = None
        if wrapper.mdd:
            try: data = wrapper.mdd.get(path)
            except: pass
        if not data and wrapper.folder_path:
            try:
                file_path = os.path.join(wrapper.folder_path, path)
                if os.path.exists(file_path):
                    with open(file_path, 'rb') as f: data = f.read()
            except: pass
        if data:
            if isinstance(data, str):
                try: return data.encode('utf-8')
                except: return data.encode('gbk', errors='ignore')
            return data
        return None
