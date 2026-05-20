# -*- coding: utf-8 -*-
import threading
import json
import os
import html as html_module
from utils.path_helper import safe_url_encode
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

    # ==================== 系统初始化 ====================
    def _init_system(self):
        def task():
            self._load_config()
            current_group = self.config.get("current_group", "")
            if current_group:
                for p in self.config.get("groups", {}).get(current_group, []):
                    self.manager.load_mdx(p)
            self._refresh_ui()
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
        self.window.evaluate_js("if(document.getElementById('groupView').style.display === 'flex') pywebview.api.init_group_view();")

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
                for p in self.config.get("groups", {}).get(group_name, []):
                    self.manager.load_mdx(p)
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
                valid_sources = [s for s in r.get("sources", []) if os.path.abspath(s["dict_id"]) in allowed_ids]
                if valid_sources:
                    # 此时 valid_sources 里面已经包含了 idx 信息
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
                idx = source.get("idx")  # 修改：获取精确的 idx
                raw_html, _ = self.manager.get_content(source["dict_id"], key, idx)  # 修改：传递 idx
                if not raw_html:
                    continue
                safe_html = self._build_complete_html(raw_html, source["dict_id"], i)
                render_list.append({"dict_name": source["dict_name"], "html": safe_html})
            if render_list:
                self.window.evaluate_js(f"setContent({json.dumps(render_list, ensure_ascii=False)})")
        threading.Thread(target=task, daemon=True).start()
        
    def _build_complete_html(self, raw_html: str, dict_id: str, iframe_index: int) -> str:
        # 与资源服务器 /mdd/{dict_id}/{path:.*} 对齐的 base
        url_safe_dict_id = safe_url_encode(dict_id)
        base_url = f"http://localhost:{self.server.port}/mdd/{url_safe_dict_id}/"

        # 不再在前端改写 HTML；由 <base> 改变相对路径的解析基准
        head_content = f'<base href="{base_url}">'
        body_html = f'''<div id="mdx-content" style="padding: 8px; overflow: hidden;">
    {raw_html}
</div>'''

        resize_script = f'''<script>(function() {{
    var t="dict-iframe-{iframe_index}";
    function s() {{ 
        var contentDiv = document.getElementById('mdx-content');
        if (!contentDiv) return;
        // offsetHeight 是最稳定的高度，不受 body margin 塌陷和默认边距影响
        var h = contentDiv.offsetHeight; 
        window.parent.postMessage({{type:'resize',id:t,height:h}},'*'); 
    }}
    if(window.addEventListener) window.addEventListener("load", function(){{ s() }});
    else window.attachEvent("onload", function(){{ s() }});
    window.addEventListener("message", function(e){{ if(e.data==='calcHeight') setTimeout(s,50); }});
}})();</script>'''
        
        entry_script = '''
<script>
(function() {
  // 使用捕获阶段拦截，防止被其他事件覆盖
  document.addEventListener('click', function(e) {
    var a = e && e.target && e.target.closest('a');
    if (!a) return;
    var href = (a.getAttribute('href') || '').trim();
    if (href.toLowerCase().startsWith('entry://')) {
      e.preventDefault();
      e.stopPropagation();
      // 提取词条名并解码
      var word = decodeURIComponent(href.substring(8));
      // 去掉可能存在的锚点 (如 entry://word#anchor)
      if (word.indexOf('#') !== -1) {
        word = word.split('#')[0];
      }
      if (word) {
        // 发送消息给主窗口
        window.parent.postMessage({ type: 'entry-link', word: word }, '*');
      }
    }
  }, true);
})();
</script>
'''

        return f'''<!DOCTYPE html><html style="font-size: 24px; height: auto;"><head><meta charset="UTF-8">{head_content}</head>
<body style="margin: 0; padding: 0; height: auto; overflow: hidden;">
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
        current_group = self.config.get("current_group", "")
        if not current_group:
            return
        dict_list = self.config.get("groups", {}).get(current_group, [])
        abs_id = os.path.abspath(dict_id)
        if abs_id in dict_list:
            dict_list.remove(abs_id)
        if abs_id not in self.config.get("excluded", []):
            self.config.setdefault("excluded", []).append(abs_id)
        self._schedule_save_config()
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
            ids[index], ids[index-1] = ids[index-1], ids[index]
        elif action == 'down' and index < len(ids) - 1:
            ids[index], ids[index+1] = ids[index+1], ids[index]
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
            self.window.evaluate_js(f"showDictInfoModal({json.dumps(title, ensure_ascii=False)}, {json.dumps(info_str, ensure_ascii=False)})")
            