# -*- coding: utf-8 -*-
"""pywebview 暴露给前端的 API 层。

前后端分离后，本模块不再包含任何 HTML/CSS/JS 字符串：主窗口页面由
``web/index.html`` 提供，词条 iframe 的脚本由 ``/static/entry.js`` 提供。
"""
import atexit
import copy
import html as html_module
import json
import logging
import os
import threading
from collections import OrderedDict

from services import storage
from utils import error_reporter
from utils.executors import submit_task
from utils.html_link_rewriter import HtmlRewriteError, rewrite_html_links
from utils.path_helper import safe_url_encode

logger = logging.getLogger(__name__)

# 词典路径失效的统一提示口径：界面与弹窗都不再出现含糊的「未知词典」。
INVALID_DICT_TITLE = "词典路径已失效"


class WindowApi:
    def __init__(self, window, manager, resource_server):
        self.window = window
        self.manager = manager
        self.server = resource_server
        self._current_results = []
        self._results_lock = threading.Lock()
        self._search_seq = 0               # 用于丢弃过期搜索结果
        self._config_lock = threading.RLock()
        self.config = {}

        # 定时器必须先于 _init_system 初始化：其后台任务可能触发延迟保存
        self._save_timer = None
        self._save_timer_lock = threading.Lock()
        self._save_delay = 0.5
        atexit.register(self._flush_config)

        # 词条 HTML 缓存（iframe 直连 /entry/ 路由用）
        # key=(dict_id, key, idx)，value=含 __IFRAME_ID__ 占位符的完整 HTML
        self._entry_cache = OrderedDict()
        self._entry_cache_lock = threading.Lock()
        self._ENTRY_CACHE_MAX = 32

        self._init_system()

    def _get_config(self, key, default=None):
        with self._config_lock:
            return self.config.get(key, default)

    def _set_config(self, key, value):
        with self._config_lock:
            self.config[key] = value

    # ==================== 配置读写 ====================
    def _load_config(self):
        try:
            self.config = storage.load_config()
        except Exception as exc:  # storage 内部已兜底，这里再防一层
            logger.exception("加载配置失败")
            error_reporter.report("配置读取失败", str(exc), exc=exc)
            self.config = {}
        self.config.setdefault("all_dicts", [])
        self.config.setdefault("groups", {})
        self.config.setdefault("excluded", [])
        self.config.setdefault("current_group", "")
        self._config_loaded = True

    def _schedule_save_config(self):
        """延迟保存配置（加锁避免并发创建多个 Timer）。"""
        with self._save_timer_lock:
            if self._save_timer:
                self._save_timer.cancel()
            self._save_timer = threading.Timer(self._save_delay, self._save_config_now)
            self._save_timer.daemon = True
            self._save_timer.start()

    def _flush_config(self):
        """atexit 兜底：取消延迟保存并立即落盘。"""
        with self._save_timer_lock:
            timer = self._save_timer
            self._save_timer = None
        if timer:
            timer.cancel()
        if getattr(self, "_config_loaded", False):
            self._save_config_now()

    def _save_config_now(self):
        with self._config_lock:
            if not isinstance(self.config.get("excluded"), list):
                self.config["excluded"] = []
            config_snapshot = copy.deepcopy(self.config)
        storage.save_config(config_snapshot)

    # ==================== 无效路径清理 ====================
    def _cleanup_invalid_paths(self, invalid_paths: set):
        """从 all_dicts / 所有分组 / excluded 中删除无效路径。"""
        modified = False
        with self._config_lock:
            if "all_dicts" in self.config:
                original_count = len(self.config["all_dicts"])
                self.config["all_dicts"] = [
                    d for d in self.config["all_dicts"]
                    if os.path.abspath(d.get("id", "")) not in invalid_paths
                ]
                if len(self.config["all_dicts"]) < original_count:
                    modified = True
                    logger.info("从 all_dicts 删除 %d 个无效路径",
                                original_count - len(self.config["all_dicts"]))

            for group_name, paths in self.config.get("groups", {}).items():
                original_count = len(paths)
                self.config["groups"][group_name] = [
                    p for p in paths if os.path.abspath(p) not in invalid_paths
                ]
                if len(self.config["groups"][group_name]) < original_count:
                    modified = True
                    logger.info("从 groups['%s'] 删除 %d 个无效路径",
                                group_name, original_count - len(self.config["groups"][group_name]))

            if "excluded" in self.config:
                original_count = len(self.config["excluded"])
                self.config["excluded"] = [
                    p for p in self.config["excluded"]
                    if os.path.abspath(p) not in invalid_paths
                ]
                if len(self.config["excluded"]) < original_count:
                    modified = True
                    logger.info("从 excluded 删除 %d 个无效路径",
                                original_count - len(self.config["excluded"]))

        if modified:
            self._schedule_save_config()

    # ==================== 失效词典清理 ====================
    @staticmethod
    def _is_valid_dict_path(path) -> bool:
        """词典文件真实存在才算有效；目录、失效挂载点一律视为失效。"""
        try:
            return bool(path) and os.path.isfile(path)
        except OSError:
            return False

    def _collect_invalid_paths(self) -> set:
        """扫描配置中出现的全部词典路径，返回其中已失效的绝对路径集合。"""
        with self._config_lock:
            candidates = set()
            for d in self.config.get("all_dicts", []):
                if isinstance(d, dict) and isinstance(d.get("id"), str) and d["id"]:
                    candidates.add(os.path.abspath(d["id"]))
            for paths in self.config.get("groups", {}).values():
                if isinstance(paths, list):
                    for p in paths:
                        if isinstance(p, str):
                            candidates.add(os.path.abspath(p))
            for p in self.config.get("excluded", []):
                if isinstance(p, str):
                    candidates.add(os.path.abspath(p))
        return {p for p in candidates if not self._is_valid_dict_path(p)}

    def _purge_invalid_dicts(self, invalid_paths: set, notify: bool = True) -> set:
        """删除失效词典（配置 + 已加载实例），并按统一口径提示用户。

        调用方负责确认传入的路径确实已失效；返回实际清理的集合。
        路径失效与「加载失败」是两回事，后者的清理仍走 _cleanup_invalid_paths。
        """
        invalid_paths = set(invalid_paths or ())
        if not invalid_paths:
            return set()
        self._cleanup_invalid_paths(invalid_paths)
        for p in invalid_paths:
            try:
                self.manager.unload_mdx(p)
            except Exception as exc:
                logger.warning("卸载失效词典时异常 %s: %s", p, exc)
        logger.warning("已删除 %d 个失效词典: %s", len(invalid_paths), sorted(invalid_paths))
        if notify:
            names = "、".join(os.path.basename(p) for p in sorted(invalid_paths)[:5])
            more = f" 等 {len(invalid_paths)} 个" if len(invalid_paths) > 5 else ""
            error_reporter.report(
                INVALID_DICT_TITLE,
                f"{names}{more} 已失效（文件不存在），已从词典列表和分组中删除。"
                "如需继续使用，请重新导入该词典。",
                level="warning",
            )
        return invalid_paths

    # ==================== 系统初始化 ====================
    def _init_system(self):
        def task():
            self._load_config()
            # 主界面启动即检测失效词典：提示统一在此产生（启动期暂存，
            # 由前端 get_pending_errors() 拉取后显示在主界面）
            self._purge_invalid_dicts(self._collect_invalid_paths())
            with self._config_lock:
                current_group = self.config.get("current_group", "")
                group_paths = list(self.config.get("groups", {}).get(current_group, []))
            if current_group:
                invalid_paths = set()
                for p in group_paths:
                    abs_p = os.path.abspath(p)
                    if not os.path.exists(abs_p):
                        logger.warning("词典文件不存在: %s", p)
                        invalid_paths.add(abs_p)
                    elif not self.manager.load_mdx(p):
                        logger.warning("加载词典失败: %s", p)
                        invalid_paths.add(abs_p)
                if invalid_paths:
                    self._cleanup_invalid_paths(invalid_paths)
            self._refresh_ui()

        submit_task(task)

    def _refresh_ui(self):
        try:
            with self._config_lock:
                data = {
                    "groups": [{"name": g} for g in self.config.get("groups", {}).keys()],
                    "current": self.config.get("current_group", ""),
                }
            self.window.evaluate_js(f"updateUI({json.dumps(data, ensure_ascii=False)})")
        except Exception:
            logger.debug("刷新 UI 失败（窗口可能尚未就绪）", exc_info=True)

    def get_pending_errors(self):
        """前端页面就绪后拉取启动期间暂存的后端错误（显示后即清空）。"""
        return [list(item) for item in error_reporter.drain_pending()]

    # ==================== 导入逻辑 ====================
    def _load_mdx_batch(self, file_paths):
        """统一的批量加载逻辑。"""
        # 导入前先过滤失效路径，避免把已经不存在的词典写进 all_dicts
        file_paths = [p for p in file_paths
                      if self._is_valid_dict_path(os.path.abspath(p))]
        if not file_paths:
            error_reporter.report(
                INVALID_DICT_TITLE,
                "所选词典已失效（文件不存在），未导入任何内容。", level="warning")
            return
        with self._config_lock:
            existing_ids = {
                d.get("id") for d in self.config.get("all_dicts", [])
                if isinstance(d, dict)
            }
            for p in file_paths:
                abs_p = os.path.abspath(p)
                if abs_p not in existing_ids:
                    name = os.path.splitext(os.path.basename(p))[0]
                    self.config.setdefault("all_dicts", []).append({"id": abs_p, "name": name})
                    existing_ids.add(abs_p)
        # 加载词典在锁外执行
        for p in file_paths:
            if not self.manager.load_mdx(os.path.abspath(p)):
                logger.warning("导入词典失败: %s", p)  # 具体异常由 MdxWrapper 弹窗上报
        self._schedule_save_config()
        self._refresh_ui()
        try:
            self.window.evaluate_js(
                "if(document.getElementById('groupView').style.display === 'flex') "
                "pywebview.api.init_group_view();")
        except Exception:
            logger.debug("刷新分组视图失败", exc_info=True)

    def open_file(self):
        try:
            from webview import FileDialog
            paths = self.window.create_file_dialog(
                FileDialog.OPEN, allow_multiple=True, file_types=('MDX (*.mdx)',))
            if paths:
                submit_task(self._load_mdx_batch, paths)
        except Exception as exc:
            logger.exception("打开词典文件失败")
            error_reporter.report("打开词典失败", str(exc), exc=exc)

    def open_folder(self):
        try:
            from webview import FileDialog
            folders = self.window.create_file_dialog(FileDialog.FOLDER)
            if folders:
                from utils.path_helper import find_mdx_files
                mdx_files = find_mdx_files(folders[0])
                if mdx_files:
                    submit_task(self._load_mdx_batch, mdx_files)
                else:
                    error_reporter.report("未找到词典", "该文件夹下没有 MDX 文件", level="warning")
        except Exception as exc:
            logger.exception("打开词典文件夹失败")
            error_reporter.report("打开词典失败", str(exc), exc=exc)

    # ==================== 分组切换与查询 ====================
    def switch_group(self, group_name: str):
        with self._config_lock:
            self.config["current_group"] = group_name if group_name else ""
        self._schedule_save_config()
        self._refresh_ui()
        self.init_group_view()
        if group_name:
            with self._config_lock:
                new_group_paths = {
                    os.path.abspath(p)
                    for p in self.config.get("groups", {}).get(group_name, [])
                }
                group_paths = list(self.config.get("groups", {}).get(group_name, []))

            def switch_task():
                self.manager.unload_all_except(new_group_paths)
                invalid_paths = set()
                for p in group_paths:
                    abs_p = os.path.abspath(p)
                    if not os.path.exists(abs_p):
                        logger.warning("词典文件不存在: %s", p)
                        invalid_paths.add(abs_p)
                    elif not self.manager.load_mdx(p):
                        logger.warning("加载词典失败: %s", p)
                        invalid_paths.add(abs_p)
                if invalid_paths:
                    self._cleanup_invalid_paths(invalid_paths)
                self._auto_search_after_switch()

            submit_task(switch_task)

    def _auto_search_after_switch(self):
        try:
            self.window.evaluate_js("""
            (function() {
                var input = document.getElementById('searchInput');
                var keyword = input ? input.value.trim() : '';
                if (keyword) {
                    var use_variants = document.getElementById('variantCheck').checked;
                    pywebview.api.search(keyword, use_variants);
                }
            })()
            """)
        except Exception as exc:
            logger.debug("自动搜索失败: %s", exc)

    def search(self, keyword: str, use_variants: bool):
        """执行一次搜索。

        本方法在 pywebview 的 js_api 调用线程中**同步**编排，内部的
        ``manager.search`` 会把单词典任务提交到共享线程池并 wait(timeout)。
        刻意不再额外套一层 ``submit_task``：在池 worker 内提交并等待池任务
        会造成线程池饥饿死锁（详见 utils/executors.py）。

        结果带回请求序号，慢请求返回时若已有更新的搜索，直接丢弃。
        """
        with self._config_lock:
            current_group = self.config.get("current_group", "")
            group_paths = self.config.get("groups", {}).get(current_group, [])
        if not current_group or not group_paths:
            self.window.evaluate_js('updateResults([])')
            return
        allowed_ids = {os.path.abspath(p) for p in group_paths}

        with self._results_lock:
            self._search_seq += 1
            seq = self._search_seq

        try:
            results = self.manager.search(keyword, use_variants, allowed_ids)
        except Exception as exc:
            logger.exception("搜索失败: %s", keyword)
            error_reporter.report("搜索失败", str(exc), exc=exc)
            return

        with self._results_lock:
            if seq != self._search_seq:
                logger.debug("丢弃过期搜索结果 seq=%d keyword=%s", seq, keyword)
                return  # 过期任务丢弃：已有更新的搜索
            self._current_results = results
            first_match = bool(results and results[0]["key"] == keyword)

        try:
            self.window.evaluate_js(f"updateResults({json.dumps(results, ensure_ascii=False)})")
        except Exception as exc:
            logger.warning("更新搜索结果失败: %s", exc)
        if first_match:
            self.show_entry(0)

    def show_entry(self, index: int):
        with self._results_lock:
            if index < 0 or index >= len(self._current_results):
                return
            item = self._current_results[index]
            key = item["key"]
            sources = list(item["sources"])

        def task():
            port = self.server.port
            render_list = []
            for i, source in enumerate(sources):
                idx = source.get("idx")
                url = (
                    f"http://localhost:{port}/entry/{safe_url_encode(source['dict_id'])}"
                    f"?ifid=dict-iframe-{i}"
                    f"&idx={idx if idx is not None else -1}"
                    f"&k={safe_url_encode(key)}"
                )
                render_list.append({"dict_name": source["dict_name"], "url": url})
            self.window.evaluate_js(f"setContent({json.dumps(render_list, ensure_ascii=False)})")

        submit_task(task)

    # ==================== 词条 HTML（前后端分离） ====================
    def _build_complete_html(self, raw_html: str, dict_id: str, iframe_id: str) -> str:
        """只生成骨架 HTML；样式与脚本全部走 /static/ 静态资源。"""
        try:
            raw_html = rewrite_html_links(raw_html)
        except HtmlRewriteError as exc:
            logger.warning("词典正文 HTML 重写失败，使用原文: %s", exc)
            error_reporter.report("词典正文解析失败", str(exc), level="warning")

        static = f"http://localhost:{self.server.port}/static"
        base_url = f"http://localhost:{self.server.port}/mdd/{safe_url_encode(dict_id)}/"
        return f'''<!DOCTYPE html><html><head><meta charset="UTF-8">
<link rel="stylesheet" href="{static}/custom.css">
<link rel="stylesheet" href="{static}/entry.css">
<base href="{base_url}">
</head>
<body data-iframe-id="{iframe_id}">
<div id="mdx-content">{raw_html}</div>
<script src="{static}/entry.js"></script>
</body></html>'''

    def build_entry_html(self, dict_id: str, key: str, idx, iframe_id: str):
        """供 /entry/ 路由回调：构建词条完整 HTML（带 LRU 缓存）。

        缓存存含 ``__IFRAME_ID__`` 占位符的模板，命中后才替换 iframe id；
        custom.css 改由静态服务提供，不再进入 HTML，也就无需 css_stamp。
        """
        cache_key = (dict_id, key, idx)
        with self._entry_cache_lock:
            cached = self._entry_cache.get(cache_key)
            if cached is not None:
                self._entry_cache.move_to_end(cache_key)
                return cached.replace("__IFRAME_ID__", iframe_id)

        try:
            raw_html, _ = self.manager.get_content(dict_id, key, idx)
        except Exception as exc:
            logger.exception("词条加载失败 %s #%s", dict_id, idx)
            error_reporter.report("词条加载失败", f"{key}（{exc}）", level="warning", exc=exc)
            return None
        if not raw_html:
            return None

        html = self._build_complete_html(raw_html, dict_id, "__IFRAME_ID__")
        with self._entry_cache_lock:
            self._entry_cache[cache_key] = html
            if len(self._entry_cache) > self._ENTRY_CACHE_MAX:
                self._entry_cache.popitem(last=False)
        return html.replace("__IFRAME_ID__", iframe_id)

    # ==================== 分组管理界面 ====================
    def init_group_view(self):
        # 分组界面只静默清理，不在这里弹提示（提示统一在主界面产生）
        self._purge_invalid_dicts(self._collect_invalid_paths(), notify=False)
        try:
            with self._config_lock:
                all_dicts = list(self.config.get("all_dicts", []))
                groups = self.config.get("groups", {})
                current_group_name = self.config.get("current_group", "")
                current_active = {
                    os.path.abspath(p) for p in groups.get(current_group_name, [])
                }
                current_excluded = {
                    os.path.abspath(p) for p in self.config.get("excluded", [])
                }

                groups_data = []
                for name, dict_list in groups.items():
                    dicts_in_group = []
                    for d_id in dict_list:
                        abs_id = os.path.abspath(d_id)
                        match_d = next((d for d in all_dicts if d.get("id") == abs_id), None)
                        # 缺元数据时回退文件名，不再显示含糊的「未知词典」
                        dicts_in_group.append({
                            "id": abs_id,
                            "name": (match_d.get("name") if match_d else None)
                                    or os.path.basename(abs_id),
                        })
                    groups_data.append({"name": name, "dicts": dicts_in_group})

                all_dicts_with_state = []
                for d in all_dicts:
                    abs_id = os.path.abspath(d.get("id", ""))
                    if abs_id in current_active:
                        status = "active"
                    elif abs_id in current_excluded:
                        status = "excluded"
                    else:
                        status = "none"
                    all_dicts_with_state.append({
                        "id": abs_id,
                        "name": d.get("name") or os.path.basename(abs_id),
                        "status": status,
                    })

            js_code = (
                f"renderGroupView({json.dumps(all_dicts_with_state, ensure_ascii=False)}, "
                f"{json.dumps(groups_data, ensure_ascii=False)}, "
                f"{json.dumps(current_group_name, ensure_ascii=False)})"
            )
            self.window.evaluate_js(js_code)
        except Exception as exc:
            logger.exception("init_group_view 失败")
            error_reporter.report("分组界面加载失败", str(exc), level="warning", exc=exc)

    def add_group(self, name: str):
        name = (name or "").strip()
        if not name:
            return {"ok": False, "error": "分组名称不能为空"}
        with self._config_lock:
            groups = self.config.setdefault("groups", {})
            if name in groups:
                return {"ok": False, "error": f"分组已存在：{name}"}
            groups[name] = []
        self._schedule_save_config()
        self._refresh_ui()
        self.init_group_view()
        return {"ok": True, "name": name}

    def delete_group(self):
        with self._config_lock:
            current_group = self.config.get("current_group", "")
            if not current_group:
                return
            self.config.get("groups", {}).pop(current_group, None)
            self.config["current_group"] = ""
        self._schedule_save_config()
        self._refresh_ui()
        self.init_group_view()

    def add_dict_to_group(self, dict_id):
        abs_id = os.path.abspath(dict_id)
        if not self._is_valid_dict_path(abs_id):
            # 路径已失效：不加入分组，静默删除（提示只在主界面产生）
            self._purge_invalid_dicts(self._collect_invalid_paths(), notify=False)
            self._refresh_ui()
            self.init_group_view()
            return {
                "ok": False,
                "error": f"{INVALID_DICT_TITLE}，已删除：{os.path.basename(abs_id)}",
            }
        with self._config_lock:
            current_group = self.config.get("current_group", "")
            groups = self.config.setdefault("groups", {})
            if not current_group or current_group not in groups:
                if groups:
                    current_group = next(iter(groups.keys()), "")
                    self.config["current_group"] = current_group
                else:
                    error_reporter.report("无法添加词典", "请先新建一个分组！", level="warning")
                    return
            dict_list = groups[current_group]
            if abs_id not in dict_list:
                dict_list.append(abs_id)
                if abs_id in self.config.get("excluded", []):
                    self.config["excluded"].remove(abs_id)
        self.manager.load_mdx(abs_id)
        self._schedule_save_config()
        self.init_group_view()
        return {"ok": True}

    def remove_dict_from_group(self, dict_id):
        with self._config_lock:
            current_group = self.config.get("current_group", "")
            if not current_group:
                return
            dict_list = self.config.get("groups", {}).get(current_group, [])
            abs_id = os.path.abspath(dict_id)
            if abs_id in dict_list:
                dict_list.remove(abs_id)
        self._schedule_save_config()
        self.init_group_view()

    def exclude_dict(self, dict_id):
        abs_id = os.path.abspath(dict_id)
        with self._config_lock:
            for group_name, dict_list in self.config.get("groups", {}).items():
                if abs_id in dict_list:
                    dict_list.remove(abs_id)
            if abs_id not in self.config.get("excluded", []):
                self.config.setdefault("excluded", []).append(abs_id)
        self._schedule_save_config()
        try:
            self.manager.unload_mdx(abs_id)
        except Exception as exc:
            logger.warning("卸载词典时发生异常: %s", exc)
        self.init_group_view()

    def reload_excluded_dict(self, dict_id):
        abs_id = os.path.abspath(dict_id)
        if not self._is_valid_dict_path(abs_id):
            # 恢复入口同样不能把失效路径放回列表（静默清理）
            self._purge_invalid_dicts(self._collect_invalid_paths(), notify=False)
            self.init_group_view()
            return
        with self._config_lock:
            if abs_id in self.config.get("excluded", []):
                self.config["excluded"].remove(abs_id)
        self._schedule_save_config()
        self.manager.load_mdx(abs_id)
        self.init_group_view()

    def move_dict(self, dict_id: str, action: str):
        with self._config_lock:
            current_group = self.config.get("current_group", "")
            if not current_group:
                return
            ids = self.config.get("groups", {}).get(current_group, [])
            abs_id = os.path.abspath(dict_id)
            if abs_id not in ids:
                return
            index = ids.index(abs_id)
            if action == 'up' and index > 0:
                ids[index], ids[index - 1] = ids[index - 1], ids[index]
            elif action == 'down' and index < len(ids) - 1:
                ids[index], ids[index + 1] = ids[index + 1], ids[index]
            elif action == 'top':
                ids.pop(index); ids.insert(0, abs_id)
            elif action == 'bottom':
                ids.pop(index); ids.append(abs_id)
        self._schedule_save_config()
        self.init_group_view()

    def get_dict_info(self, dict_id):
        abs_id = os.path.abspath(dict_id)

        def task():
            try:
                with self._config_lock:
                    all_dicts = list(self.config.get("all_dicts", []))
                target = next((d for d in all_dicts if d.get("id") == abs_id), None)
                if not target:
                    return
                title = target.get("name") or os.path.basename(abs_id)
                header_info = self.manager.get_dict_header_info(abs_id)
                parts = [
                    f"<p><b>文件路径:</b> <code style='word-break:break-all;'>"
                    f"{html_module.escape(target.get('id', abs_id))}</code></p>"
                ]
                if header_info:
                    disp_title = header_info.get("title", "") or "（无）"
                    source = header_info.get("title_source", "")
                    label = f"（来自{'Header' if source == 'header' else '文件名'}）" if source else ""
                    parts.append(
                        f"<p style='margin-top:6px;'><b>标题:</b> {html_module.escape(disp_title)} "
                        f"<span style='color:#888;font-size:16px;'>{label}</span></p>"
                    )
                    desc = header_info.get("description", "")
                    if desc:
                        escaped = html_module.escape(desc, quote=True)
                        parts.append(
                            "<p style='margin-top:8px;'><b>描述:</b></p>"
                            f"<iframe srcdoc=\"{escaped}\" "
                            "style='width:100%;height:150px;border:1px solid #e0e0e0;"
                            "border-radius:4px;background:#fff;'></iframe>"
                        )
                    tech = []
                    if header_info.get("version"):
                        tech.append(f"<li><b>MDX版本:</b> v{html_module.escape(header_info['version'])}</li>")
                    if header_info.get("encoding"):
                        tech.append(f"<li><b>编码:</b> {html_module.escape(header_info['encoding'])}</li>")
                    if header_info.get("encrypted"):
                        tech.append(f"<li><b>加密:</b> {html_module.escape(header_info['encrypted'])}</li>")
                    if header_info.get("creation_date"):
                        tech.append(f"<li><b>创建日期:</b> {html_module.escape(header_info['creation_date'])}</li>")
                    if header_info.get("num_entries"):
                        tech.append(f"<li><b>词条数:</b> {header_info['num_entries']:,}</li>")
                    if header_info.get("mdd_count"):
                        tech.append(f"<li><b>MDD文件:</b> {header_info['mdd_count']}</li>")
                    if tech:
                        parts.append(
                            "<p style='margin-top:8px;'><b>技术参数:</b></p>"
                            f"<ul style='margin-left:20px;font-size:16px;'>{''.join(tech)}</ul>"
                        )
                    raw = header_info.get("raw_header", {})
                    if raw:
                        parts.append(
                            "<details style='margin-top:8px;'>"
                            "<summary style='cursor:pointer;color:#2196F3;font-size:18px;'>全部 Header 字段</summary>"
                            "<div style='overflow:auto;background:#f5f5f5;padding:8px;"
                            "border-radius:4px;font-size:16px;margin-top:6px;'>"
                        )
                        for k, v in raw.items():
                            shown = v if len(v) <= 300 else v[:300] + "..."
                            parts.append(
                                f"<p style='margin:2px 0;'><b>{html_module.escape(k)}:</b> "
                                f"<code style='word-break:break-all;'>{html_module.escape(shown)}</code></p>"
                            )
                        parts.append("</div></details>")
                    if header_info.get("error"):
                        parts.append(
                            f"<p style='color:#f44336;margin-top:8px;'>⚠ "
                            f"{html_module.escape(header_info['error'])}</p>"
                        )
                else:
                    parts.append("<p style='color:#888;margin-top:8px;'>词典文件未加载，无法读取 Header 信息。</p>")

                info_str = "".join(parts)
                self.window.evaluate_js(
                    f"showDictInfoModal({json.dumps(title, ensure_ascii=False)}, "
                    f"{json.dumps(info_str, ensure_ascii=False)})"
                )
            except Exception as exc:
                logger.exception("读取词典信息失败: %s", abs_id)
                error_reporter.report("读取词典信息失败", str(exc), level="warning", exc=exc)

        submit_task(task)
