# -*- coding: utf-8 -*-
"""
异体字处理模块
"""

import itertools
import re
from typing import Set, Dict, List, Generator, Optional, Tuple


class VariantHandler:
    """异体字处理器"""
    
    def __init__(self, variant_dict: Optional[Dict[str, List[str]]] = None):
        """
        初始化异体字处理器
        
        Args:
            variant_dict: 异体字字典，格式如 {'字': ['异体1', '异体2'], ...}
        """
        self.variant_map: Dict[str, Set[str]] = variant_dict if variant_dict else {}
      
    def get_variants(self, char: str) -> Set[str]:
        """
        获取某个字符的所有异体字（包括自身）
        
        Args:
            char: 要查询的字符
            
        Returns:
            异体字集合
        """
        if char in self.variant_map:
            return self.variant_map[char]
        return {char}
    
    def generate_combinations(self, input_str: str) -> Generator[str, None, None]:
        """
        生成输入字符串所有可能的异体字组合
        
        Args:
            input_str: 输入字符串
            
        Yields:
            所有可能的组合字符串
        """
        if not input_str:
            yield ""
            return
        
        # 获取每个字符的异体字列表
        chars_variants = []
        for ch in input_str:
            chars_variants.append(sorted(self.get_variants(ch)))
        
        # 生成所有组合
        for combo in itertools.product(*chars_variants):
            yield ''.join(combo)


    def build_full_regex(self, keyword: str, exact: bool = False):
        """构建完整正则匹配模式（从头匹配）

        例如 "莺歌燕舞" → '^[莺𮹘𬸕鶯𦾉鸎][歌𬤐謌][燕𱊴𮹜𬸧𪈏䴏鷰][舞儛]'

        Args:
            keyword: 关键词
            exact: True 为精确匹配（加 $ 结尾锚定），False 为前缀匹配（默认，用于搜索结果过滤）

        Returns:
            编译好的正则对象，或 None（无可展开字符时）
        """
        import re
        if not keyword:
            return None
        parts = []
        has_variant = False
        for ch in keyword:
            variants = self.get_variants(ch)
            if len(variants) > 1:
                has_variant = True
                chars = ''.join(re.escape(v) for v in sorted(variants))
                parts.append(f'[{chars}]')
            else:
                parts.append(re.escape(ch))
        if not has_variant:
            return None
        pattern = '^' + ''.join(parts)
        if exact:
            pattern += '$'
        return re.compile(pattern)
