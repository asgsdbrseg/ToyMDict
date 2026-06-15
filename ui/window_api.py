# -*- coding: utf-8 -*-
import threading
import json
import os
import re
import html as html_module
from utils.path_helper import safe_url_encode, get_app_base_dir
from utils.resource_resolver import MdxResourceResolver
import time

class WindowApi:
    def __init__(self, window, manager, resource_server):
        self.window = window
        self.manager = manager
        self.server = resource_server
        self._current_results = []
        self._results_lock = threading.Lock()
        self.config = {}
        self._init_system()
        self._save_timer = None
        self._save_delay = 0.5  # 500ms 防抖

        # ===== 新增：加载自定义 CSS =====
        self._custom_css = ""
        self._load_custom_css()

    # ==================== 配置读写 ====================
    def _load_config(self):
        from services import storage
        try:
            self.config = storage.load_config()
        except Exception as e:
            print(f"[DEBUG] 加载配置失败: {e}")
            self.config = {}
        self.config.setdefault("all_dicts", [])
        self.config.setdefault("groups", {})
        self.config.setdefault("excluded", [])
        self.config.setdefault("current_group", "")

    def _schedule_save_config(self):
        """延迟保存配置，避免频繁文件写入"""
        if self._save_timer:
            self._save_timer.cancel()
        self._save_timer = threading.Timer(self._save_delay, self._save_config_now)
        self._save_timer.daemon = True
        self._save_timer.start()

    def _save_config_now(self):
        """实际保存配置"""
        from services import storage
        if not isinstance(self.config.get("excluded"), list):
            self.config["excluded"] = []
        storage.save_config(self.config)

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
            current_group = self.config.get("current_group", "")
            if current_group:
                invalid_paths = set()
                group_paths = self.config.get("groups", {}).get(current_group, [])
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
        threading.Thread(target=task, daemon=True).start()

    def _refresh_ui(self):
        try:
            data = {"groups": [{"name": g} for g in self.config.get("groups", {}).keys()],
                    "current": self.config.get("current_group", "")}
            self.window.evaluate_js(f"updateUI({json.dumps(data, ensure_ascii=False)})")
        except Exception:
            pass

    # ==================== 导入逻辑 ====================
    def _load_mdx_batch(self, file_paths):
        """统一的批量加载逻辑"""
        existing_ids = {d["id"] for d in self.config.get("all_dicts", [])}
        for p in file_paths:
            abs_p = os.path.abspath(p)
            if abs_p not in existing_ids:
                name = os.path.splitext(os.path.basename(p))[0]
                self.config.setdefault("all_dicts", []).append({"id": abs_p, "name": name})
            self.manager.load_mdx(abs_p)
        self._schedule_save_config()
        self._refresh_ui()
        self.window.evaluate_js(
            "if(document.getElementById('groupView').style.display === 'flex') pywebview.api.init_group_view();")

    def open_file(self):
        try:
            from webview import FileDialog
            paths = self.window.create_file_dialog(FileDialog.OPEN, allow_multiple=True, file_types=('MDX (*.mdx)',))
            if paths:
                threading.Thread(target=self._load_mdx_batch, args=(paths,), daemon=True).start()
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
                    threading.Thread(target=self._load_mdx_batch, args=(mdx_files,), daemon=True).start()
                else:
                    print("该文件夹下未找到 MDX 文件")
        except Exception as e:
            print(e)

    # ==================== 分组切换与查询 ====================
    def switch_group(self, group_name: str):
        self.config["current_group"] = group_name if group_name else ""
        self._schedule_save_config()
        self._refresh_ui()
        self.init_group_view()
        if group_name:
            new_group_paths = {os.path.abspath(p) for p in self.config.get("groups", {}).get(group_name, [])}

            def switch_task():
                self.manager.unload_all_except(new_group_paths)
                invalid_paths = set()
                group_paths = self.config.get("groups", {}).get(group_name, [])
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

            threading.Thread(target=switch_task, daemon=True).start()

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
        current_group = self.config.get("current_group", "")
        if not current_group:
            self.window.evaluate_js('updateResults([])')
            return
        allowed_ids = set(os.path.abspath(p) for p in self.config.get("groups", {}).get(current_group, []))
        if not allowed_ids:
            self.window.evaluate_js('updateResults([])')
            return

        def task():
            results = self.manager.search(keyword, use_variants)
            filtered_results = []
            for r in results:
                valid_sources = [s for s in r.get("sources", [])
                                 if os.path.abspath(s["dict_id"]) in allowed_ids]
                if valid_sources:
                    filtered_results.append({"key": r["key"], "sources": valid_sources})
            self._current_results = filtered_results
            self.window.evaluate_js(f"updateResults({json.dumps(filtered_results, ensure_ascii=False)})")
            if filtered_results and filtered_results[0]["key"] == keyword:
                self.show_entry(0)
            with self._results_lock:
                self._current_results = filtered_results

        threading.Thread(target=task, daemon=True).start()

    def show_entry(self, index: int):
        with self._results_lock:
            if index < 0 or index >= len(self._current_results):
                return
            item = self._current_results[index]
            key = item["key"]
            sources = item["sources"]

        def task():
            render_list = []
            for i, source in enumerate(sources):
                idx = source.get("idx")
                raw_html, _ = self.manager.get_content(source["dict_id"], key, idx)
                if not raw_html:
                    continue
                safe_html = self._build_complete_html(raw_html, source["dict_id"], i)
                render_list.append({"dict_name": source["dict_name"], "html": safe_html})
            if render_list:
                self.window.evaluate_js(f"setContent({json.dumps(render_list, ensure_ascii=False)})")

        threading.Thread(target=task, daemon=True).start()

    def _build_complete_html(self, raw_html: str, dict_id: str, iframe_index: int) -> str:
        raw_html = re.sub(
            r'(src|href)\s*=\s*(["\'])/(?![/])',
            r'\1=\2',
            raw_html
        )
        raw_html = re.sub(
            r'url\(\s*(["\']?)/(?![/])',
            r'url(\1',
            raw_html
        )
        url_safe_dict_id = safe_url_encode(dict_id)
        base_url = f"http://localhost:{self.server.port}/mdd/{url_safe_dict_id}/"
        head_content = f'<base href="{base_url}">'

        custom_css_tag = f'<style>{self._custom_css}</style>' if self._custom_css else ''

        body_html = f'''<div id="mdx-content" style="padding: 8px; overflow: hidden;">
            {raw_html}
        </div>'''

        resize_script = f'''<script>(function() {{
            var t="dict-iframe-{iframe_index}";
            function s() {{
                var contentDiv = document.getElementById('mdx-content');
                if (!contentDiv) return;
                var h = contentDiv.offsetHeight;
                window.parent.postMessage({{type:'resize',id:t,height:h}},'*');
            }}
            if(window.addEventListener) window.addEventListener("load", function(){{ s() }});
            else window.attachEvent("onload", function(){{ s() }});
            window.addEventListener("message", function(e){{
                if(e.data==='calcHeight') setTimeout(s,50);
            }});
        }})();</script>'''

        entry_script = '''
        <script>
        (function() {
            var currentAudio = null;
            document.addEventListener('click', function(e) {
                var a = e && e.target && e.target.closest('a');
                if (!a) return;
                var href = (a.getAttribute('href') || '').trim();
                if (href.toLowerCase().startsWith('entry://')) {
                    e.preventDefault(); e.stopPropagation();
                    var word = decodeURIComponent(href.substring(8));
                    if (word.indexOf('#') !== -1) { word = word.split('#')[0]; }
                    if (word) { window.parent.postMessage({ type: 'entry-link', word: word }, '*'); }
                }
                else if (href.toLowerCase().startsWith('sound://')) {
                    e.preventDefault(); e.stopPropagation();
                    var soundPath = decodeURIComponent(href.substring(8));
                    if (soundPath.startsWith('/')) { soundPath = soundPath.substring(1); }
                    var baseUrl = document.baseURI;
                    var soundUrl = baseUrl + soundPath;
                    if (currentAudio) { currentAudio.pause(); currentAudio = null; }
                    currentAudio = new Audio(soundUrl);
                    currentAudio.play().catch(function(err) {
                        console.error("音频播放失败:", err, "URL:", soundUrl);
                    });
                }
            }, true);
        })();
        </script>
        '''

        return f'''<!DOCTYPE html><html><head><meta charset="UTF-8">{custom_css_tag}{head_content}</head>
        <body style="margin: 0; padding: 0; height: auto; overflow: hidden; max-width: 100vw; font-size: 24px;">
        {body_html}
        {resize_script}
        {entry_script}
        </body></html>'''

    # ==================== 分组管理界面 ====================
    def init_group_view(self):
        try:
            all_dicts = self.config.get("all_dicts", [])
            groups_data = []
            for name, dict_list in self.config.get("groups", {}).items():
                dicts_in_group = []
                for d_id in dict_list:
                    abs_id = os.path.abspath(d_id)
                    match_d = next((d for d in all_dicts if d.get("id") == abs_id), None)
                    dicts_in_group.append({
                        "id": abs_id,
                        "name": match_d.get("name", "未知词典") if match_d else "未知词典"
                    })
                groups_data.append({"name": name, "dicts": dicts_in_group})
            current_group_name = self.config.get("current_group", "")
            current_active = set(os.path.abspath(p) for p in self.config.get("groups", {}).get(current_group_name, []))
            current_excluded = set(os.path.abspath(p) for p in self.config.get("excluded", []))
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
        groups = self.config.setdefault("groups", {})
        groups[name] = []
        self._schedule_save_config()
        self._refresh_ui()
        self.init_group_view()

    def delete_group(self):
        current_group = self.config.get("current_group", "")
        if not current_group:
            return
        self.config.get("groups", {}).pop(current_group, None)
        self.config["current_group"] = ""
        self._schedule_save_config()
        self._refresh_ui()
        self.init_group_view()

    def add_dict_to_group(self, dict_id):
        current_group = self.config.get("current_group", "")
        groups = self.config.setdefault("groups", {})
        if not current_group or current_group not in groups:
            if groups:
                current_group = next(iter(groups.keys()), "")
                self.config["current_group"] = current_group
                self._schedule_save_config()
                self._refresh_ui()
            else:
                self.window.evaluate_js("alert('请先新建一个分组！')")
                return
        dict_list = groups[current_group]
        abs_id = os.path.abspath(dict_id)
        if abs_id not in dict_list:
            dict_list.append(abs_id)
            if abs_id in self.config.get("excluded", []):
                self.config["excluded"].remove(abs_id)
            self._schedule_save_config()
            self.manager.load_mdx(abs_id)
            self.init_group_view()

    def remove_dict_from_group(self, dict_id):
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

        # 从所有分组中移除该词典（不仅限当前分组）
        for group_name, dict_list in self.config.get("groups", {}).items():
            if abs_id in dict_list:
                dict_list.remove(abs_id)

        # 加入排除列表
        if abs_id not in self.config.get("excluded", []):
            self.config.setdefault("excluded", []).append(abs_id)

        self._schedule_save_config()

        # 从内存卸载
        try:
            self.manager.unload_mdx(abs_id)
        except Exception as e:
            print(f"卸载词典时发生异常(已忽略): {e}")

        self.init_group_view()


    def reload_excluded_dict(self, dict_id):
        abs_id = os.path.abspath(dict_id)
        if abs_id in self.config.get("excluded", []):
            self.config["excluded"].remove(abs_id)
            self._schedule_save_config()
            self.manager.load_mdx(abs_id)
            self.init_group_view()

    def move_dict(self, dict_id: str, action: str):
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
        all_dicts = self.config.get("all_dicts", [])
        target = next((d for d in all_dicts if d.get("id") == os.path.abspath(dict_id)), None)
        if target:
            title = target.get("name", "未知词典")
            info_str = (f"<p><b>词典ID:</b> <code>{html_module.escape(dict_id)}</code></p>"
                        f"<p><b>文件路径:</b> <code>{html_module.escape(target.get('id', '未知'))}</code></p>")
            self.window.evaluate_js(
                f"showDictInfoModal({json.dumps(title, ensure_ascii=False)}, {json.dumps(info_str, ensure_ascii=False)})")
