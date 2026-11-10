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
            width=1100,
            height=750,
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
