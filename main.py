# -*- coding: utf-8 -*-
import logging
import os

import webview

from core.dictionary_manager import DictionaryManager
from services.global_hotkey import GlobalHotkeyService
from services.resource_server import ResourceServer
from ui.html_template import load_index_html
from ui.window_api import WindowApi
from utils import error_reporter
from utils.logging_config import setup_logging

logger = logging.getLogger(__name__)


def _configure_webview2_args():
    """给 WebView2 追加启动参数，与 Rust/Tauri 版保持一致。

    pywebview 在 Windows 上底层是 WebView2（Edge Chromium）。WebView2
    Runtime 通过 ``WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS`` 环境变量读取
    附加启动参数，必须在 ``webview.start()`` 创建浏览器环境之前设置。

    - disable-features: 关掉 OOUI / SmartScreen 等组件，避免出现「保存的信息」；
    - disk-cache-size: 磁盘缓存限制到 32MB，避免长期占用；
    - enable-low-end-device-mode: 让 Chromium 更积极回收内存。

    追加而非覆盖，避免抹掉外部或框架已设定的参数；非 Windows 平台设置该
    环境变量无害（WebView2 Runtime 会忽略）。
    """
    args = [
        "--disable-features=msWebOOUI,msPdfOOUI,msSmartScreenProtection",
        "--disk-cache-size=33554432",
        "--enable-low-end-device-mode",
    ]
    key = "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"
    existing = os.environ.get(key, "").strip()
    merged = (existing + " " + " ".join(args)).strip() if existing else " ".join(args)
    os.environ[key] = merged
    logger.info("WebView2 启动参数: %s", merged)


def main():
    # 环境变量 TOYMDICT_DEBUG=1 时才开启 DevTools / DEBUG 日志
    debug = os.environ.get("TOYMDICT_DEBUG") == "1"
    setup_logging(debug=debug, log_to_file=True)
    logger.info("ToyMDict 启动")

    manager = DictionaryManager()
    server = ResourceServer(manager)
    server.start()

    hotkey = None
    try:
        inline_frontend = os.environ.get("TOYMDICT_INLINE_FRONTEND") == "1"
        window = webview.create_window(
            title="ToyMDict",
            html=load_index_html(server.port, inline=inline_frontend),
            width=1300,
            height=850,
            min_size=(800, 500),
        )
        error_reporter.attach(window)

        api = WindowApi(window, manager, server)
        server.set_entry_provider(api.build_entry_html)
        window.expose(
            api.open_file,
            api.open_folder,
            api.switch_group,
            api.add_group,
            api.delete_group,
            api.search,
            api.show_entry,
            api.get_advanced_search_dicts,
            api.advanced_search,
            api.cancel_advanced_search,
            api.init_group_view,
            api.add_dict_to_group,
            api.remove_dict_from_group,
            api.exclude_dict,
            api.reload_excluded_dict,
            api.get_dict_info,
            api.move_dict,
            api.get_pending_errors,
        )

        # 全局快捷键：双击 Ctrl 取词（默认常开）
        hotkey = GlobalHotkeyService(window, enabled=True)
        hotkey.start()

        webview.settings['ALLOW_DOWNLOADS'] = True
        webview.settings['OPEN_DEVTOOLS_IN_DEBUG'] = False
        _configure_webview2_args()
        webview.start(debug=True)
    except Exception as exc:
        logger.exception("启动失败")
        error_reporter.report("启动失败", str(exc), exc=exc)
        raise
    finally:
        if hotkey is not None:
            hotkey.stop()
        server.stop()
        error_reporter.detach()
        logger.info("ToyMDict 已退出")


if __name__ == '__main__':
    main()
