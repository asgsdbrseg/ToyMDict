# -*- coding: utf-8 -*-
import os
import re
from urllib.parse import quote, unquote
from utils.path_helper import normalize_resource_path

class MdxResourceResolver:
    """统一资源解析器：处理URL重写、安全检查、路由"""
    
    @staticmethod
    def check_path_safety(dict_id, path):
        if not path or '..' in path:
            return False
        safe_path = normalize_resource_path(path)
        abs_dict_dir = os.path.dirname(os.path.abspath(dict_id))
        abs_resource = os.path.normpath(os.path.join(abs_dict_dir, safe_path))
        # 必须确保解析后的路径还在词典目录下
        return abs_resource.startswith(abs_dict_dir)

    @staticmethod
    def resolve_resource(dict_manager, dict_id, path):
        """供 ResourceServer 调用的统一入口"""
        path = unquote(path)
        dict_id = unquote(dict_id)
        
        if not MdxResourceResolver.check_path_safety(dict_id, path):
            return None # 拦截恶意请求
            
        return dict_manager.get_resource(dict_id, path)
