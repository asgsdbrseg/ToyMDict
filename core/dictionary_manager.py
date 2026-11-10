# -*- coding: utf-8 -*-
import json
import logging
import os
import threading
from concurrent.futures import wait

from core.mdx_wrapper import MdxWrapper
from utils import error_reporter
from utils.executors import submit_many

logger = logging.getLogger(__name__)

# 单次搜索的整体超时（秒）。超时后未返回的词典任务会被取消并丢弃结果，
# 避免某本损坏/网络盘上的词典把整个搜索线程永久挂住。
SEARCH_TIMEOUT = 10.0


class DictionaryManager:
    def __init__(self):
        self.loaded_dicts: dict[str, MdxWrapper] = {}
        self._lock = threading.RLock()
        self._variant_handler = None
        self._init_variant_handler()

    def _init_variant_handler(self):
        try:
            from libs.variant_utils import VariantHandler
            from utils.path_helper import get_app_base_dir

            base_dir = get_app_base_dir()
            json_path = os.path.join(base_dir, "variants.json")
            if not os.path.exists(json_path):
                logger.warning("未找到异体字映射表: %s，异体字搜索将被禁用", json_path)
                return

            with open(json_path, 'r', encoding='utf-8') as f:
                variants = json.load(f)

            logger.info("异体字映射表: %s", json_path)
            self._variant_handler = VariantHandler(variants)
            logger.info("  %d 组规则, %d 个字符", len(variants), len(self._variant_handler.variant_map))
        except Exception as exc:
            logger.exception("加载异体字映射表失败")
            error_reporter.report("异体字表加载失败", "异体字搜索将被禁用", exc=exc)

    def load_mdx(self, path: str) -> bool:
        abs_path = os.path.abspath(path)
        # 快速检查：已加载则直接返回（短锁）
        with self._lock:
            if abs_path in self.loaded_dicts:
                return True
        if not os.path.exists(abs_path):
            logger.warning("词典文件不存在: %s", abs_path)
            return False

        # 耗时加载过程在锁外执行，不阻塞 search / get_content / get_resource
        wrapper = MdxWrapper(abs_path)
        if not wrapper.load(variant_handler=self._variant_handler):
            return False

        with self._lock:
            if abs_path in self.loaded_dicts:
                wrapper.close()  # 并发重复加载，关闭多余的 wrapper
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

    # ==================== 搜索（统一线程池 + 超时 + 过期丢弃） ====================

    def get_search_targets(self, allowed_ids=None):
        """返回当前可搜索词典的 ``[(abs_path, wrapper), ...]`` 快照。"""
        with self._lock:
            wrappers = list(self.loaded_dicts.items())
        if allowed_ids is not None:
            wrappers = [(p, w) for p, w in wrappers if p in allowed_ids]
        return wrappers

    @staticmethod
    def search_one(target, keyword, use_variants):
        """单词典搜索，在共享池 worker 中执行，永不向外抛异常。"""
        path, wrapper = target
        try:
            return path, wrapper.name, wrapper.search(keyword, use_variants)
        except Exception as exc:
            logger.exception("词典 %s 搜索失败", wrapper.name)
            error_reporter.report(
                "词典搜索失败", f"{wrapper.name}: {exc}", level="warning", exc=exc
            )
            return path, wrapper.name, []

    @staticmethod
    def merge_search_results(keyword, outcomes):
        """按提交顺序合并各词典命中，保证 sources 顺序稳定可预期。"""
        merged = {}
        seen_pairs: set[tuple[str, int]] = set()
        for path, name, hits in outcomes:
            for key, idx in hits:
                if key not in merged:
                    merged[key] = {"key": key, "sources": []}
                pair = (path, idx)
                if pair not in seen_pairs:
                    seen_pairs.add(pair)
                    merged[key]["sources"].append({
                        "dict_id": path,
                        "dict_name": name,
                        "idx": idx,
                    })
        results = list(merged.values())
        results.sort(key=lambda x: (0 if x["key"] == keyword else 1, len(x["key"])))
        return results

    def search(self, keyword: str, use_variants: bool,
               allowed_ids: set = None, timeout: float = SEARCH_TIMEOUT) -> list:
        """并行搜索多本词典，带整体超时与超时结果丢弃。

        **调用约束**：必须在「非共享池 worker」线程调用（当前为 pywebview
        的 js_api 调用线程）。若在共享池任务内部再提交子任务并等待，8 个
        worker 会互相等待造成线程池饥饿死锁——详见 utils/executors.py。
        """
        targets = self.get_search_targets(allowed_ids)
        if not targets or not keyword:
            return []

        pairs = submit_many(
            lambda t: DictionaryManager.search_one(t, keyword, use_variants),
            targets,
        )
        futures = [f for _, f in pairs]
        done, not_done = wait(futures, timeout=timeout)

        if not_done:
            logger.warning(
                "搜索超时：%d/%d 本词典未在 %.1fs 内返回，结果已丢弃",
                len(not_done), len(futures), timeout,
            )
            error_reporter.report(
                "搜索超时",
                f"{len(not_done)} 本词典响应超时，已忽略其结果",
                level="warning",
            )
            for fut in not_done:
                fut.cancel()  # 运行中的任务无法中断，但其结果不再被读取

        outcomes = []
        for target, fut in pairs:
            if fut not in done:  # 过期任务：直接丢弃
                continue
            try:
                outcomes.append(fut.result())
            except Exception as exc:  # pragma: no cover - search_one 已兜底
                logger.exception("词典搜索任务异常")
                error_reporter.report("词典搜索失败", str(exc), level="warning", exc=exc)

        return self.merge_search_results(keyword, outcomes)

    # ==================== 内容与资源 ====================

    def get_content(self, dict_id: str, key: str, idx: int = None) -> tuple:
        abs_path = os.path.abspath(dict_id)
        with self._lock:
            wrapper = self.loaded_dicts.get(abs_path)
        if not wrapper:
            return "", ""
        return wrapper.get_content(key, idx), wrapper.name

    def get_dict_header_info(self, dict_id: str) -> dict:
        """获取词典 Header 元数据；未加载时临时加载读取后关闭。"""
        abs_path = os.path.abspath(dict_id)
        with self._lock:
            wrapper = self.loaded_dicts.get(abs_path)
        if wrapper:
            return wrapper.get_header_info()
        if not os.path.exists(abs_path):
            return None
        # 仅读 header，避免为查看词典信息而触发全量索引构建
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
                from utils.path_helper import resolve_case_insensitive
                exact_path = os.path.normpath(
                    os.path.join(wrapper.folder_path, path.replace("\\", "/").lstrip("/"))
                )
                if os.path.isfile(exact_path):
                    with open(exact_path, "rb") as f:
                        return f.read()
                ci_path = resolve_case_insensitive(wrapper.folder_path, path)
                if ci_path and os.path.isfile(ci_path):
                    with open(ci_path, "rb") as f:
                        return f.read()
            except Exception:
                logger.debug("外挂资源读取失败: %s", path, exc_info=True)
        return None
