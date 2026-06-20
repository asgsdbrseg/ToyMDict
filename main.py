# -*- coding: utf-8 -*-
import webview
from core.dictionary_manager import DictionaryManager
from services.resource_server import ResourceServer
from ui.window_api import WindowApi
from ui.html_template import HTML_TEMPLATE


def main():
    # 初始化核心管理器
    manager = DictionaryManager()

    # 启动资源服务
    server = ResourceServer(manager)
    server.start()

    try:
        # 创建窗口
        window = webview.create_window(
            title="ToyMDict",
            html=HTML_TEMPLATE,
            width=1100,
            height=750,
            min_size=(800, 500)
        )

        # 绑定 API
        api = WindowApi(window, manager, server)
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
        )

        webview.start(debug=False)
    finally:
        server.stop()

if __name__ == '__main__':
    main()
