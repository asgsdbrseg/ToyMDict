# -*- coding: utf-8 -*-
from core.mdx_wrapper import MdxWrapper
import os
import json
import threading
import atexit
import concurrent.futures


class _DaemonThreadPoolExecutor(concurrent.futures.ThreadPoolExecutor):
    """工作线程为 daemon 的线程池：主线程退出时进程可正常终止，
    避免 ThreadPoolExecutor 默认创建非 daemon 线程导致程序无法退出。

    由于父类 _adjust_thread_count 会先 start 线程再返回，已 active 的线程
    无法再修改 daemon；这里在调用父类的极短窗口内把 threading.Thread 的
    daemon 默认值临时注入为 True（加锁避免并发竞态），不依赖私有内部结构。"""
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


class DictionaryManager:
    def __init__(self):
        self.loaded_dicts: dict[str, MdxWrapper] = {}
        self._lock = threading.RLock()
        self._variant_handler = None
        self._init_variant_handler()
        # daemon 线程池：在后台线程内并行搜索多本词典，避免进程退出挂起
        self._search_executor = _DaemonThreadPoolExecutor(
            max_workers=8, thread_name_prefix="dict-search")
        atexit.register(self._search_executor.shutdown, wait=False)

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
                                
            print(f"[异体字] 映射表: {json_path}")
            self._variant_handler = VariantHandler(variants)
            
            # 输出统计信息（在 VariantHandler 构建完成后才能获取准确的字符数）
            rule_count = len(variants)  # 规则组数（JSON 中的顶级键数量）
            char_count = len(self._variant_handler.variant_map)  # 实际覆盖的字符数（构建后）
            print(f"  {rule_count} 组规则, {char_count} 个字符")
        except Exception as e:
            print(f"加载异体字失败: {e}")

    def load_mdx(self, path: str) -> bool:
        abs_path = os.path.abspath(path)
        # 快速检查：已加载则直接返回（短锁）
        with self._lock:
            if abs_path in self.loaded_dicts:
                return True
        if not os.path.exists(abs_path):
            return False

        # 耗时加载过程在锁外执行，不阻塞 search / get_content / get_resource
        wrapper = MdxWrapper(abs_path)
        if not wrapper.load(variant_handler=self._variant_handler):
            return False

        # 加载完成后，短锁写入字典
        with self._lock:
            if abs_path in self.loaded_dicts:
                # 并发重复加载，关闭多余的 wrapper
                wrapper.close()
                return True
            self.loaded_dicts[abs_path] = wrapper
        return True

    def unload_mdx(self, path: str):
        abs_path = os.path.abspath(path)
        with self._lock:
            wrapper = self.loaded_dicts.pop(abs_path, None)
        if wrapper:
            wrapper.close()

    def unload_all_except(self, keep_paths: set):
        with self._lock:
            to_unload = [p for p in self.loaded_dicts if p not in keep_paths]
            wrappers = [self.loaded_dicts.pop(p) for p in to_unload]
        for wrapper in wrappers:
            wrapper.close()

    def search(self, keyword: str, use_variants: bool) -> list:
        with self._lock:
            wrappers = list(self.loaded_dicts.items())
        if not wrappers or not keyword:
            return []

        def _search_one(item):
            path, wrapper = item
            try:
                return path, wrapper.name, wrapper.search(keyword, use_variants)
            except Exception as e:
                print(f"[搜索] 词典 {wrapper.name} 搜索失败: {e}")
                return path, wrapper.name, []

        merged_results = {}
        seen_pairs: set[tuple[str, int]] = set()
        # 在调用方的后台线程内并行搜索各词典，多词典场景显著加速。
        # 按提交顺序（即分组配置顺序）收集结果：所有词典已并行搜索，
        # 此处仅按序等待，总耗时仍取决于最慢的词典；
        # 这样保证同一词条的 sources 顺序与词条间相对顺序稳定可预期，
        # 不随各词典完成先后波动。
        futures = [self._search_executor.submit(_search_one, item) for item in wrappers]
        for fut in futures:
            path, name, hits = fut.result()
            for key, idx in hits:
                if key not in merged_results:
                    merged_results[key] = {"key": key, "sources": []}
                pair = (path, idx)
                if pair not in seen_pairs:
                    seen_pairs.add(pair)
                    merged_results[key]["sources"].append({
                        "dict_id": path,
                        "dict_name": name,
                        "idx": idx
                    })

        results = list(merged_results.values())
        results.sort(key=lambda x: (0 if x["key"] == keyword else 1, len(x["key"])))
        return results

    def get_content(self, dict_id: str, key: str, idx: int = None) -> tuple:
        abs_path = os.path.abspath(dict_id)
        with self._lock:
            wrapper = self.loaded_dicts.get(abs_path)
        if not wrapper:
            return "", ""
        return wrapper.get_content(key, idx), wrapper.name

    def get_dict_header_info(self, dict_id: str) -> dict:
        """获取词典的 Header 元数据信息。词典已加载则直接返回；未加载则临时加载读取后关闭。"""
        abs_path = os.path.abspath(dict_id)
        with self._lock:
            wrapper = self.loaded_dicts.get(abs_path)
        if wrapper:
            return wrapper.get_header_info()
        if not os.path.exists(abs_path):
            return None
        from core.mdx_wrapper import MdxWrapper
        # 仅读 header，避免为查看词典信息而触发全量索引构建（尤其词典在只读/网络盘时）
        temp_wrapper = MdxWrapper(abs_path, build_index=False)
        try:
            if not temp_wrapper.load(variant_handler=None):
                return None
            return temp_wrapper.get_header_info()
        finally:
            temp_wrapper.close()

    def get_resource(self, dict_id: str, path: str) -> bytes:
        abs_path = os.path.abspath(dict_id)
        with self._lock:
            wrapper = self.loaded_dicts.get(abs_path)
        if not wrapper:
            return None

        data = wrapper.get_resource(path)
        if data:
            return data

        if wrapper.folder_path:
            try:
                from utils.path_helper import normalize_resource_path
                file_path = os.path.join(wrapper.folder_path, normalize_resource_path(path))
                if os.path.isfile(file_path):
                    with open(file_path, 'rb') as f:
                        return f.read()
            except Exception:
                pass

        return None