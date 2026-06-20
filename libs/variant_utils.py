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

    def should_use_regex(self, keyword: str, max_count: str = 100) -> bool:
        """判断是否应该用正则搜索（组合数多时才用）

        经验阈值：
        - 组合数 ≤ 100：多次 search_prefix 更快（正则开销 > 合并扫描收益）
        - 组合数 > 100：正则搜索更快（合并扫描收益 > 正则开销）
        """
        if not self.should_expand(keyword):
            return False
        count = 1
        for ch in keyword:
            count *= len(self.get_variants(ch))
            if count > max_count:
                return True
        return False

    def build_regex_pattern(self, keyword: str):
        """构建正则模式，返回 (pattern, first_chars, min_prefix, max_prefix)

        min_prefix/max_prefix: 首字符异体字的最小/最大值，用于 block 级 skip
        """
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
        first_variants = sorted(self.get_variants(keyword[0]))
        first_chars = set(first_variants)
        # 用完整首字符的 min/max 做 block 级 skip（比单字符更精确）
        min_first = first_variants[0]
        max_first = first_variants[-1]
        return pattern, first_chars, min_first, max_first