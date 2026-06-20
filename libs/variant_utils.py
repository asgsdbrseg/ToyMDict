# -*- coding: utf-8 -*-
"""
异体字处理模块
"""

import itertools
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
    
    def expand_keyword(self, keyword: str, max_combinations: int = 1024) -> List[str]:
        """
        展开关键词为异体字组合列表（限制数量）
        
        Args:
            keyword: 原始关键词
            max_combinations: 最大组合数
            
        Returns:
            异体字组合列表
        """
        combinations = []
        for combo in self.generate_combinations(keyword):
            combinations.append(combo)
            if len(combinations) >= max_combinations:
                break
        return combinations
    
    def should_expand(self, keyword: str) -> bool:
        """
        判断是否需要展开异体字搜索
        
        Args:
            keyword: 关键词
            
        Returns:
            是否需要展开
        """
        # 如果关键词长度超过10，不展开（避免组合爆炸）
        if len(keyword) > 10:
            return False
        
        # 检查是否有异体字
        for ch in keyword:
            if ch in self.variant_map and len(self.variant_map[ch]) > 1:
                return True
        return False
