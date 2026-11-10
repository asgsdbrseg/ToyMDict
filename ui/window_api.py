# -*- coding: utf-8 -*-
import threading
import json
import os
import copy
import atexit
import traceback
import html as html_module
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from utils.path_helper import safe_url_encode, get_app_base_dir
from utils.html_link_rewriter import rewrite_html_links
from services import storage
from utils.daemon_executor import DaemonThreadPoolExecutor


_TASK_EXECUTOR = DaemonThreadPoolExecutor(max_workers=8, thread_name_prefix="window-api")
_LOAD_EXECUTOR = DaemonThreadPoolExecutor(max_workers=8, thread_name_prefix="load-entry")


def _report_future_error(future):
    """Future 异常回调：保持与原 threading.Thread 同等的错误可见性。"""
    try:
        future.result()
    except Exception:
        traceback.print_exc()


def _submit_task(fn, *args, **kwargs):
    """提交后台任务到模块级线程池，异常时打印堆栈。"""
    future = _TASK_EXECUTOR.submit(fn, *args, **kwargs)
    future.add_done_callback(_report_future_error)
    return future


@atexit.register
def _shutdown_executors():
    _TASK_EXECUTOR.shutdown(wait=False)
    _LOAD_EXECUTOR.shutdown(wait=False)


# ==================== 词条 iframe 注入脚本（模块级常量，避免每词条重建字符串） ====================

# iframe 高度自适应：ResizeObserver 覆盖图片/字体/details 等一切尺寸变化，
# rAF 节流 + 同高去重；保留 calcHeight 消息作为父端主动请求通道。
# "__IFRAME_ID__" 为占位符，由 _build_complete_html 替换。
_RESIZE_SCRIPT_TEMPLATE = '''<script>(function() {
    var id = "__IFRAME_ID__";
    var last = -1, raf = 0;
    function measure() {
        raf = 0;
        var d = document.getElementById('mdx-content');
        if (!d) return;
        var h = d.offsetHeight;
        if (h === last) return;          // 同高去重，避免重复 postMessage
        last = h;
        window.parent.postMessage({type:'resize',id:id,height:h},'*');
    }
    function schedule() {                // rAF 节流：一帧最多量一次
        if (!raf) raf = requestAnimationFrame(measure);
    }
    var d = document.getElementById('mdx-content');
    if (window.ResizeObserver && d) {
        new ResizeObserver(schedule).observe(d);   // 覆盖图片/字体/details 等一切尺寸变化
    } else {
        window.addEventListener("load", schedule); // 兜底（老内核）
    }
    window.addEventListener("message", function(e) {
        if (e.data === 'calcHeight') schedule();   // 保留父端主动请求（兼容 details 展开）
    });
})();</script>'''

# 音频播放与词条内链接处理（entry:// 跳转、sound:// 发音、内联 new Audio()）。
# 完全静态，无需按词条定制。
_ENTRY_SCRIPT = '''
        <script>
        (function() {
            var currentAudio = null;

            // 从 onclick 属性中提取音频 URL（兼容 new Audio('url') / "url" / `url` 及反引号包裹的情况）
            function extractAudioUrl(onclick) {
                if (!onclick) return null;
                // 用反向引用匹配同种引号闭合，URL 内容允许包含反引号（紧凑样式表遗留）
                var m = onclick.match(/new\\s+Audio\\(\\s*(['"`])(.*?)\\1\\s*\\)/);
                if (!m) return null;
                var url = m[2].trim();
                // 去除 MDX 紧凑样式表遗留的反引号包裹
                url = url.replace(/^`+|`+$/g, '');
                return url || null;
            }

            function playAudio(url) {
                if (!url) return;
                try {
                    if (currentAudio) { currentAudio.pause(); currentAudio = null; }
                    currentAudio = new Audio(url);
                    currentAudio.play().catch(function(err) {
                        console.error("音频播放失败:", err, "URL:", url);
                    });
                } catch (err) {
                    console.error("音频播放异常:", err);
                }
            }

            // 解码 URI 分量；畸形 % 序列（损坏词典中真实存在）返回原文而非抛异常，
            // 避免中断捕获阶段的 click handler 导致该词条所有链接失效
            function safeDecode(s) {
                try { return decodeURIComponent(s); } catch (e) { return s; }
            }

            document.addEventListener('click', function(e) {
                var a = e && e.target && e.target.closest('a');
                if (!a) return;

                var href = (a.getAttribute('href') || '').trim();

                // 最高优先级：HTML 重写阶段识别出的音频链接（外部/本地音频文件）
                var dataAudioUrl = a.getAttribute('data-audio-url');
                if (dataAudioUrl) {
                    e.preventDefault();
                    e.stopPropagation();
                    playAudio(dataAudioUrl);
                    return;
                }

                // 处理内联 onclick 中通过 new Audio() 播放语音的情况（如在线发音）
                var onclick = a.getAttribute('onclick') || '';
                var audioUrl = extractAudioUrl(onclick);
                if (audioUrl) {
                    e.preventDefault();
                    e.stopPropagation();
                    playAudio(audioUrl);
                    return;
                }

                if (href.toLowerCase().startsWith('entry://')) {
                    e.preventDefault(); e.stopPropagation();
                    var word = safeDecode(href.substring(8));
                    if (word.indexOf('#') !== -1) { word = word.split('#')[0]; }
                    if (word) { window.parent.postMessage({ type: 'entry-link', word: word }, '*'); }
                }
                else if (href.toLowerCase().startsWith('sound://')) {
                    e.preventDefault(); e.stopPropagation();
                    var soundPath = safeDecode(href.substring(8));
                    if (soundPath.startsWith('/')) { soundPath = soundPath.substring(1); }
                    var baseUrl = document.baseURI;
                    var soundUrl = baseUrl + soundPath;
                    playAudio(soundUrl);
                }
                else if (!href) {
                    // 无 href 的 <a>：阻止可能的默认跳转行为
                    e.preventDefault();
                }
            }, true);
        })();
        </script>
        '''

# 双击图片：把图片完整 URL 发给父窗口，由其主窗口浮层显示（方案A）。完全静态。
_IMAGE_SCRIPT = '''
        <script>
        (function() {
            function getImgSrc(img) {
                 try { return img.currentSrc || img.src || ''; } catch (e) { return img.src || ''; }
             }
            document.addEventListener('dblclick', function(e) {
                 var img = e && e.target && e.target.closest ? e.target.closest('img') : null;
                 if (!img) return;
                 var src = getImgSrc(img);
                 if (!src) return;
                window.parent.postMessage({ type: 'open-image', url: src }, '*');
             }, true);
        })();
        </script>
        '''


class WindowApi:
    def __init__(self, window, manager, resource_server):
        self.window = window
        self.manager = manager
        self.server = resource_server
        self._current_results = []
        self._results_lock = threading.Lock()
        self._config_lock = threading.RLock()  # 新增：保护 self.config
        self.config = {}
        self._init_system()
        self._save_timer = None
        self._save_delay = 0.5
        # atexit 兜底：进程退出时立即落盘，避免丢失延迟窗口内的配置改动
        atexit.register(self._flush_config)

        self._custom_css = ""
        self._load_custom_css()
        # 词条 HTML 缓存（iframe 直连 /entry/ 路由用）：
        # key=(dict_id, key, idx, css_stamp)，value=含 __IFRAME_ID__ 占位符的完整 HTML；
        # ifid 每次请求时才替换，避免结果顺序变化导致缓存失效
        self._entry_cache = OrderedDict()
        self._entry_cache_lock = threading.Lock()
        self._ENTRY_CACHE_MAX = 32

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
        except Exception as e:
            print(f"[DEBUG] 加载配置失败: {e}")
            self.config = {}
        self.config.setdefault("all_dicts", [])
        self.config.setdefault("groups", {})
        self.config.setdefault("excluded", [])
        self.config.setdefault("current_group", "")
        self._config_loaded = True

    def _schedule_save_config(self):
        """延迟保存配置，避免频繁文件写入"""
        if self._save_timer:
            self._save_timer.cancel()
        self._save_timer = threading.Timer(self._save_delay, self._save_config_now)
        self._save_timer.daemon = True
        self._save_timer.start()

    def _flush_config(self):
        """atexit 兜底：取消延迟保存定时器并立即落盘，避免程序退出时
        丢失最近一次（0.5s 延迟窗口内）的配置改动。"""
        if self._save_timer:
            self._save_timer.cancel()
            self._save_timer = None
        if getattr(self, "_config_loaded", False):
            self._save_config_now()

    def _save_config_now(self):
        with self._config_lock:
            if not isinstance(self.config.get("excluded"), list):
                self.config["excluded"] = []
            config_snapshot = copy.deepcopy(self.config)  # 深拷贝避免迭代时被并发修改
        storage.save_config(config_snapshot)

    # ==================== 自定义 CSS ====================

    def _load_custom_css(self):
        base_dir = get_app_base_dir()
        custom_css_path = os.path.join(base_dir, "custom.css")
        if not os.path.exists(custom_css_path):
            print(f"[CSS] 未找到自定义样式文件: {custom_css_path}")
            return
        
        with open(custom_css_path, 'r', encoding='utf8') as f:
            self._custom_css = f.read()
        print(f"[CSS] 已加载自定义样式: {custom_css_path} ")

    def _inject_custom_css_to_framework(self):
        """将自定义 CSS 注入主框架页面"""
        if not self._custom_css:
            return
        try:
            escaped_css = json.dumps(self._custom_css, ensure_ascii=False)
            js_code = f"""
                (function() {{
                    var el = document.getElementById('custom-framework-css');
                    if (el) {{
                        el.textContent = {escaped_css};
                    }}
                }})();
            """
            self.window.evaluate_js(js_code)
        except Exception as e:
            print(f"[CSS] 注入框架CSS失败: {e}")

    # ==================== 无效路径清理 ====================
    def _cleanup_invalid_paths(self, invalid_paths: set):
        """从 all_dicts 和所有分组中删除无效路径"""
        modified = False
        with self._config_lock:
            # 从 all_dicts 中删除
            if "all_dicts" in self.config:
                original_count = len(self.config["all_dicts"])
                self.config["all_dicts"] = [
                    d for d in self.config["all_dicts"]
                    if os.path.abspath(d["id"]) not in invalid_paths
                ]
                if len(self.config["all_dicts"]) < original_count:
                    modified = True
                    print(f"[清理] 从 all_dicts 删除 {original_count - len(self.config['all_dicts'])} 个无效路径")

            # 从 groups 中删除（所有分组，不仅是当前分组）
            if "groups" in self.config:
                for group_name, paths in self.config["groups"].items():
                    original_count = len(paths)
                    self.config["groups"][group_name] = [
                        p for p in paths
                        if os.path.abspath(p) not in invalid_paths
                    ]
                    if len(self.config["groups"][group_name]) < original_count:
                        modified = True
                        print(f"[清理] 从 groups['{group_name}'] 删除 {original_count - len(self.config['groups'][group_name])} 个无效路径")

            # 从 excluded 中删除
            if "excluded" in self.config:
                original_count = len(self.config["excluded"])
                self.config["excluded"] = [
                    p for p in self.config["excluded"]
                    if os.path.abspath(p) not in invalid_paths
                ]
                if len(self.config["excluded"]) < original_count:
                    modified = True
                    print(f"[清理] 从 excluded 删除 {original_count - len(self.config['excluded'])} 个无效路径")

        if modified:
            self._schedule_save_config()

    # ==================== 系统初始化 ====================
    def _init_system(self):
        def task():
            self._load_config()
            with self._config_lock:
                current_group = self.config.get("current_group", "")
                group_paths = list(self.config.get("groups", {}).get(current_group, []))
            if current_group:
                invalid_paths = set()
                for p in group_paths:
                    abs_p = os.path.abspath(p)
                    if not os.path.exists(abs_p):
                        print(f"[无效] 词典文件不存在: {p}")
                        invalid_paths.add(abs_p)
                    else:
                        if not self.manager.load_mdx(p):
                            print(f"[失败] 加载词典失败: {p}")
                            invalid_paths.add(abs_p)
                if invalid_paths:
                    self._cleanup_invalid_paths(invalid_paths)
            self._refresh_ui()
            self._inject_custom_css_to_framework()
        _submit_task(task)

    def _refresh_ui(self):
        try:
            with self._config_lock:
                data = {"groups": [{"name": g} for g in self.config.get("groups", {}).keys()],
                        "current": self.config.get("current_group", "")}
            self.window.evaluate_js(f"updateUI({json.dumps(data, ensure_ascii=False)})")
        except Exception:
            pass

    # ==================== 导入逻辑 ====================
    def _load_mdx_batch(self, file_paths):
        """统一的批量加载逻辑"""
        with self._config_lock:
            existing_ids = {d["id"] for d in self.config.get("all_dicts", [])}
            for p in file_paths:
                abs_p = os.path.abspath(p)
                if abs_p not in existing_ids:
                    name = os.path.splitext(os.path.basename(p))[0]
                    self.config.setdefault("all_dicts", []).append({"id": abs_p, "name": name})
        # 加载词典在锁外执行
        for p in file_paths:
            self.manager.load_mdx(os.path.abspath(p))
        self._schedule_save_config()
        self._refresh_ui()
        self.window.evaluate_js(
            "if(document.getElementById('groupView').style.display === 'flex') pywebview.api.init_group_view();")

    def open_file(self):
        try:
            from webview import FileDialog
            paths = self.window.create_file_dialog(FileDialog.OPEN, allow_multiple=True, file_types=('MDX (*.mdx)',))
            if paths:
                _submit_task(self._load_mdx_batch, paths)
        except Exception as e:
            print(e)

    def open_folder(self):
        try:
            from webview import FileDialog
            folders = self.window.create_file_dialog(FileDialog.FOLDER)
            if folders:
                from utils.path_helper import find_mdx_files
                mdx_files = find_mdx_files(folders[0])
                if mdx_files:
                    _submit_task(self._load_mdx_batch, mdx_files)
                else:
                    print("该文件夹下未找到 MDX 文件")
        except Exception as e:
            print(e)

    # ==================== 分组切换与查询 ====================
    def switch_group(self, group_name: str):
        with self._config_lock:
            self.config["current_group"] = group_name if group_name else ""
        self._schedule_save_config()
        self._refresh_ui()
        self.init_group_view()
        if group_name:
            with self._config_lock:
                new_group_paths = {os.path.abspath(p) for p in self.config.get("groups", {}).get(group_name, [])}
                group_paths = list(self.config.get("groups", {}).get(group_name, []))

            def switch_task():
                self.manager.unload_all_except(new_group_paths)
                invalid_paths = set()
                for p in group_paths:
                    abs_p = os.path.abspath(p)
                    if not os.path.exists(abs_p):
                        print(f"[无效] 词典文件不存在: {p}")
                        invalid_paths.add(abs_p)
                    else:
                        if not self.manager.load_mdx(p):
                            print(f"[失败] 加载词典失败: {p}")
                            invalid_paths.add(abs_p)
                if invalid_paths:
                    self._cleanup_invalid_paths(invalid_paths)
                self._auto_search_after_switch()

            _submit_task(switch_task)

    def _auto_search_after_switch(self):
        try:
            js_code = """
            (function() {
                var input = document.getElementById('searchInput');
                var keyword = input ? input.value.trim() : '';
                if (keyword) {
                    var use_variants = document.getElementById('variantCheck').checked;
                    pywebview.api.search(keyword, use_variants);
                }
            })()
            """
            self.window.evaluate_js(js_code)
        except Exception as e:
            print(f"[DEBUG] 自动搜索失败: {e}")

    def search(self, keyword: str, use_variants: bool):
        with self._config_lock:
            current_group = self.config.get("current_group", "")
            group_paths = self.config.get("groups", {}).get(current_group, [])
        if not current_group:
            self.window.evaluate_js('updateResults([])')
            return
        allowed_ids = set(os.path.abspath(p) for p in group_paths)
        if not allowed_ids:
            self.window.evaluate_js('updateResults([])')
            return

        def task():
            # manager.search 已按 allowed_ids 限定只搜当前分组词典，
            # 避免对 open_file 加载但未入组的词典做无效扫描
            results = self.manager.search(keyword, use_variants, allowed_ids)

            with self._results_lock:
                self._current_results = results
                first_match = (results and results[0]["key"] == keyword)

            self.window.evaluate_js(f"updateResults({json.dumps(results, ensure_ascii=False)})")
            if first_match:
                self.show_entry(0)

        _submit_task(task)

    def show_entry(self, index: int):
        with self._results_lock:
            if index < 0 or index >= len(self._current_results):
                return
            item = self._current_results[index]
            key = item["key"]
            sources = list(item["sources"])

        def task():
            # iframe 直连本地 resource server：不再把整包 HTML 经 pywebview bridge
            # 序列化传输，由浏览器并行请求 /entry/ 路由（HTML 构建与缓存都在服务端）
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

        _submit_task(task)

    def _build_complete_html(self, raw_html: str, dict_id: str, iframe_id: str) -> str:
        # 用 lxml 流式重写所有资源链接（替代正则），覆盖多属性与 CSS url()
        raw_html = rewrite_html_links(raw_html)
        url_safe_dict_id = safe_url_encode(dict_id)
        base_url = f"http://localhost:{self.server.port}/mdd/{url_safe_dict_id}/"
        head_content = f'<base href="{base_url}">'

        custom_css_tag = f'<style>{self._custom_css}</style>' if self._custom_css else ''

        body_html = f'''<div id="mdx-content" style="padding: 8px; overflow: hidden;">
            {raw_html}
        </div>'''

        # 注入脚本均为模块级常量；resize_script 仅替换 iframe id 占位符
        resize_script = _RESIZE_SCRIPT_TEMPLATE.replace("__IFRAME_ID__", iframe_id)
        image_cursor_style = '<style>img{cursor:zoom-in;}</style>'

        return f'''<!DOCTYPE html><html style="overflow: hidden; font-size: 24px;"><head><meta charset="UTF-8">{custom_css_tag}{head_content}{image_cursor_style}</head>
        <body style="margin: 0; padding: 0; height: auto;">
        {body_html}
        {resize_script}
        {_ENTRY_SCRIPT}
        {_IMAGE_SCRIPT}
        </body></html>'''

    # ==================== 词条 iframe 直连（/entry/ 路由） ====================
    def build_entry_html(self, dict_id: str, key: str, idx, iframe_id: str):
        """供 ResourceServer 的 /entry/ 路由回调：构建词条完整 HTML（带 LRU 缓存）。

        缓存存"含 __IFRAME_ID__ 占位符"的模板，ifid 命中后才替换——
        iframe 顺序变化不影响缓存命中。custom_css 变化会改变缓存键。
        返回 None 表示词条不可用（由路由返回 404 页面）。
        """
        css_stamp = len(self._custom_css)
        cache_key = (dict_id, key, idx, css_stamp)
        with self._entry_cache_lock:
            cached = self._entry_cache.get(cache_key)
            if cached is not None:
                self._entry_cache.move_to_end(cache_key)
                return cached.replace("__IFRAME_ID__", iframe_id)

        try:
            raw_html, _ = self.manager.get_content(dict_id, key, idx)
        except Exception as e:
            print(f"[词条] 加载失败 {dict_id} #{idx}: {e}")
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
        try:
            with self._config_lock:
                all_dicts = list(self.config.get("all_dicts", []))
                groups = self.config.get("groups", {})
                current_group_name = self.config.get("current_group", "")
                current_active = set(os.path.abspath(p) for p in groups.get(current_group_name, []))
                current_excluded = set(os.path.abspath(p) for p in self.config.get("excluded", []))

                groups_data = []
                for name, dict_list in groups.items():
                    dicts_in_group = []
                    for d_id in dict_list:
                        abs_id = os.path.abspath(d_id)
                        match_d = next((d for d in all_dicts if d.get("id") == abs_id), None)
                        dicts_in_group.append({
                            "id": abs_id,
                            "name": match_d.get("name", "未知词典") if match_d else "未知词典"
                        })
                    groups_data.append({"name": name, "dicts": dicts_in_group})

                all_dicts_with_state = []
                for d in all_dicts:
                    abs_id = os.path.abspath(d.get("id"))
                    if abs_id in current_active:
                        status = "active"
                    elif abs_id in current_excluded:
                        status = "excluded"
                    else:
                        status = "none"
                    all_dicts_with_state.append({"id": abs_id, "name": d.get("name", ""), "status": status})

            js_code = f"renderGroupView({json.dumps(all_dicts_with_state, ensure_ascii=False)}, {json.dumps(groups_data, ensure_ascii=False)}, {json.dumps(current_group_name, ensure_ascii=False)})"
            self.window.evaluate_js(js_code)
        except Exception as e:
            print(f"[DEBUG] init_group_view 错误: {e}")

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
        with self._config_lock:
            current_group = self.config.get("current_group", "")
            groups = self.config.setdefault("groups", {})
            if not current_group or current_group not in groups:
                if groups:
                    current_group = next(iter(groups.keys()), "")
                    self.config["current_group"] = current_group
                else:
                    self.window.evaluate_js("alert('请先新建一个分组！')")
                    return
            dict_list = groups[current_group]
            abs_id = os.path.abspath(dict_id)
            if abs_id not in dict_list:
                dict_list.append(abs_id)
                if abs_id in self.config.get("excluded", []):
                    self.config["excluded"].remove(abs_id)
        # 锁外加载
        self.manager.load_mdx(abs_id)
        self._schedule_save_config()
        self.init_group_view()

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
            # 从所有分组中移除该词典
            for group_name, dict_list in self.config.get("groups", {}).items():
                if abs_id in dict_list:
                    dict_list.remove(abs_id)
            # 加入排除列表
            if abs_id not in self.config.get("excluded", []):
                self.config.setdefault("excluded", []).append(abs_id)
        self._schedule_save_config()
        # 锁外卸载
        try:
            self.manager.unload_mdx(abs_id)
        except Exception as e:
            print(f"卸载词典时发生异常(已忽略): {e}")
        self.init_group_view()

    def reload_excluded_dict(self, dict_id):
        abs_id = os.path.abspath(dict_id)
        with self._config_lock:
            if abs_id in self.config.get("excluded", []):
                self.config["excluded"].remove(abs_id)
        self._schedule_save_config()
        # 锁外加载
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
            with self._config_lock:
                all_dicts = list(self.config.get("all_dicts", []))
            target = next((d for d in all_dicts if d.get("id") == abs_id), None)
            if not target:
                return
            title = target.get("name", "未知词典")
            header_info = self.manager.get_dict_header_info(abs_id)

            parts = [f"<p><b>文件路径:</b> <code style='word-break:break-all;'>{html_module.escape(target.get('id', '未知'))}</code></p>"]

            if header_info:
                disp_title = header_info.get("title", "") or "（无）"
                source = header_info.get("title_source", "")
                label = f"（来自{'Header' if source == 'header' else '文件名'}）" if source else ""
                parts.append(f"<p style='margin-top:6px;'><b>标题:</b> {html_module.escape(disp_title)} "
                             f"<span style='color:#888;font-size:16px;'>{label}</span></p>")

                desc = header_info.get("description", "")
                if desc:
                    srcdoc_escaped = html_module.escape(desc, quote=True)
                    parts.append("<p style='margin-top:8px;'><b>描述:</b></p>"
                                 f"<iframe srcdoc=\"{srcdoc_escaped}\" ""style='width:100%;height:150px;border:1px solid #e0e0e0;border-radius:4px;""background:#fff;'></iframe>")

                tech = []
                if header_info.get("version"):
                    tech.append(f"<li><b>MDX版本:</b> v{html_module.escape(header_info['version'])}</li>")
                if header_info.get("encoding"):
                    tech.append(f"<li><b>编码:</b> {html_module.escape(header_info['encoding'])}</li>")
                if header_info.get("encrypted"):
                    tech.append(f"<li><b>加密:</b> {html_module.escape(header_info['encrypted'])}</li>")
                if header_info.get("creation_date"):
                    tech.append(f"<li><b>创建日期:</b> {html_module.escape(header_info['creation_date'])}</li>")
                num = header_info.get("num_entries", 0)
                if num:
                    tech.append(f"<li><b>词条数:</b> {num:,}</li>")
                mdd_count = header_info.get("mdd_count", 0)
                if mdd_count:
                    tech.append(f"<li><b>MDD文件:</b> {mdd_count}</li>")
                if tech:
                    parts.append(f"<p style='margin-top:8px;'><b>技术参数:</b></p>"
                                 f"<ul style='margin-left:20px;font-size:16px;'>{''.join(tech)}</ul>")

                raw = header_info.get("raw_header", {})
                if raw:
                    parts.append("<details style='margin-top:8px;'>"
                                 "<summary style='cursor:pointer;color:#2196F3;font-size:18px;'>全部 Header 字段</summary>"
                                 "<div style='overflow:auto;background:#f5f5f5;padding:8px;"
                                 "border-radius:4px;font-size:16px;margin-top:6px;'>")
                    for k, v in raw.items():
                        shown = v if len(v) <= 300 else v[:300] + "..."
                        parts.append(f"<p style='margin:2px 0;'><b>{html_module.escape(k)}:</b> "
                                     f"<code style='word-break:break-all;'>{html_module.escape(shown)}</code></p>")
                    parts.append("</div></details>")

                if header_info.get("error"):
                    parts.append(f"<p style='color:#f44336;margin-top:8px;'>⚠ {html_module.escape(header_info['error'])}</p>")
            else:
                parts.append("<p style='color:#888;margin-top:8px;'>词典文件未加载，无法读取 Header 信息。</p>")

            info_str = "".join(parts)
            self.window.evaluate_js(
                f"showDictInfoModal({json.dumps(title, ensure_ascii=False)}, {json.dumps(info_str, ensure_ascii=False)})")

        _submit_task(task)