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
        self.variant_map: Dict[str, Set[str]] = {}
        if variant_dict:
            self.build_variant_map(variant_dict)
    
    def build_variant_map(self, variant_dict: Dict[str, List[str]]):
        """
        构建每个字符到其所有异体字（包括自身）的映射
        
        Args:
            variant_dict: 异体字字典
        """
        self.variant_map = {}
        for key, variants in variant_dict.items():
            # 每个 key 对应的 variants 列表中，所有字符互为异体字
            for v in variants:
                if v not in self.variant_map:
                    self.variant_map[v] = set()
                # 将该组所有异体字加入映射
                self.variant_map[v].update(variants)
        
        # 确保每个字符的映射中包含自身
        for k in self.variant_map:
            self.variant_map[k].add(k)
        
        # 不再单独打印字符数（已在 DictionaryManager 中统一输出）
    
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
    
    def expand_keyword(self, keyword: str, max_combinations: int = 100) -> List[str]:
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
    
    def search_with_variants(self, keyword: str, search_func, max_results: int = 100) -> List[Tuple[str, int]]:
        """
        使用异体字组合进行搜索
        
        Args:
            keyword: 原始关键词
            search_func: 搜索函数，接受一个关键词参数，返回结果列表
            max_results: 最大结果数
            
        Returns:
            搜索结果列表（去重后）
        """
        all_results = {}
        
        # 生成所有异体字组合
        combo_count = 0
        for variant_keyword in self.generate_combinations(keyword):
            combo_count += 1
            # 只搜索相同长度的组合
            if len(variant_keyword) != len(keyword):
                continue
            
            results = search_func(variant_keyword, max_results * 2)
            
            for key, idx in results:
                if key not in all_results:
                    all_results[key] = idx
            
            # 限制组合数量，避免过多
            if combo_count > 500:
                break
        
        # 按词条名排序
        sorted_results = sorted(all_results.items(), key=lambda x: x[0])
        
        return sorted_results[:max_results]