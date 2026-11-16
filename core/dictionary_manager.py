# -*- coding: utf-8 -*-
"""词典管理：**不常驻**，按需打开、用完即关。

每次搜索 / 取词条 / 取资源都临时打开需要的词典，操作结束立刻 close()，
内存随之归还系统，不再像旧版那样把整组词典长期留在 ``loaded_dicts``。
代价是每次操作要重读 header 与块索引（有 ``.meta.cache.json`` 时只是读一份
很小的 JSON），换来的是空闲时几乎不占词典相关内存。
"""
import json
import logging
import os
import threading
from concurrent.futures import wait
from contextlib import contextmanager

from core.mdx_wrapper import MdxWrapper
from utils import error_reporter
from utils.executors import submit_many

logger = logging.getLogger(__name__)

# 单次搜索的整体超时（秒）。超时后未返回的词典任务会被取消并丢弃结果，
# 避免某本损坏/网络盘上的词典把整个搜索线程永久挂住。
SEARCH_TIMEOUT = 10.0


def dict_display_name(path: str) -> str:
    """文件名派生的显示名（打不开词典时的兜底）。"""
    return os.path.splitext(os.path.basename(path))[0]


class DictionaryManager:
    def __init__(self):
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

    # ==================== 按需打开 / 关闭 ====================

    def open_mdx(self, path: str, build_index: bool = True, quiet: bool = True):
        """临时打开一本词典；**调用方负责 close()**，打不开返回 None。

        quiet=True 时不打印词典元数据日志——按需模式下每次搜索 / 取词条都会
        打开词典，逐次打日志会把日志刷满。
        """
        abs_path = os.path.abspath(path)
        if not os.path.exists(abs_path):
            logger.warning("词典文件不存在: %s", abs_path)
            return None
        wrapper = MdxWrapper(abs_path, build_index=build_index)
        if not wrapper.load(variant_handler=self._variant_handler, quiet=quiet):
            wrapper.close()
            return None
        return wrapper

    @contextmanager
    def borrow(self, path: str, build_index: bool = True, quiet: bool = True):
        """借出一本词典，退出 with 块时必定关闭（不常驻）。"""
        wrapper = self.open_mdx(path, build_index=build_index, quiet=quiet)
        try:
            yield wrapper
        finally:
            if wrapper is not None:
                wrapper.close()

    def probe(self, path: str) -> bool:
        """校验词典能否打开（导入 / 启动检查用），打开后立即关闭。"""
        wrapper = self.open_mdx(path, quiet=False)
        if wrapper is None:
            return False
        wrapper.close()
        return True

    def get_variant_handler(self):
        """返回异体字处理器（未加载映射表时为 None）。"""
        return self._variant_handler

    # ==================== 搜索（统一线程池 + 超时 + 过期丢弃） ====================

    def search_one(self, path, keyword, use_variants):
        """单词典搜索，在共享池 worker 中执行，永不向外抛异常。

        打开 → 搜索 → 关闭，全程只在本任务内持有词典句柄。
        """
        name = dict_display_name(path)
        with self.borrow(path) as wrapper:
            if wrapper is None:
                return path, name, []
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
               dict_paths=None, timeout: float = SEARCH_TIMEOUT) -> list:
        """并行搜索多本词典，带整体超时与超时结果丢弃。

        ``dict_paths`` 是本次要搜索的词典绝对路径列表（不常驻，因此由调用方
        从配置里给出）。

        **调用约束**：必须在「非共享池 worker」线程调用（当前为 pywebview
        的 js_api 调用线程）。若在共享池任务内部再提交子任务并等待，8 个
        worker 会互相等待造成线程池饥饿死锁——详见 utils/executors.py。
        """
        paths = [os.path.abspath(p) for p in (dict_paths or []) if p]
        if not paths or not keyword:
            return []

        pairs = submit_many(
            lambda p: self.search_one(p, keyword, use_variants),
            paths,
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
        """取词条正文：临时打开词典、读完即关。"""
        abs_path = os.path.abspath(dict_id)
        with self.borrow(abs_path) as wrapper:
            if wrapper is None:
                return "", ""
            return wrapper.get_content(key, idx), wrapper.name

    def get_dict_header_info(self, dict_id: str) -> dict:
        """获取词典 Header 元数据；只读 header，读完立即关闭。"""
        abs_path = os.path.abspath(dict_id)
        if not os.path.exists(abs_path):
            return None
        with self.borrow(abs_path, build_index=False) as wrapper:
            if wrapper is None:
                return None
            return wrapper.get_header_info()

    def get_resource(self, dict_id: str, path: str) -> bytes:
        """取 MDD / 外挂资源：临时打开词典、读完即关。"""
        abs_path = os.path.abspath(dict_id)
        folder_path = None
        with self.borrow(abs_path) as wrapper:
            if wrapper is None:
                return None
            data = wrapper.get_resource(path)
            if data:
                return data
            folder_path = wrapper.folder_path

        # MDD 里没有：回退到词典目录的外挂文件（不需要词典句柄）
        if folder_path:
            try:
                from utils.path_helper import resolve_case_insensitive
                exact_path = os.path.normpath(
                    os.path.join(folder_path, path.replace("\\", "/").lstrip("/"))
                )
                if os.path.isfile(exact_path):
                    with open(exact_path, "rb") as f:
                        return f.read()
                ci_path = resolve_case_insensitive(folder_path, path)
                if ci_path and os.path.isfile(ci_path):
                    with open(ci_path, "rb") as f:
                        return f.read()
            except Exception:
                logger.debug("外挂资源读取失败: %s", path, exc_info=True)
        return None
