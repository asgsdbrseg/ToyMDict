# -*- coding: utf-8 -*-
"""
真实场景基准：模拟重叠块 + LRU 缓存驱逐 + 高频首字物化

修正合成基准的两个缺陷：
1. 异体字变体的 block 高度重叠（干/乾/幹 排序后相邻），V 次 search_prefix 重复解压
2. MAX_KEY_CACHE=10 导致高频查询互相驱逐，缓存失效
3. 高频首字（一、不、干）物化数万条目只为产出几十条结果
"""

import re
import time
import zlib
import timeit
from collections import OrderedDict
from typing import List, Set, Dict, Tuple


# ============================================================
# 异体字映射（真实中文异体组，变体多且排序相邻）
# ============================================================
VARIANT_MAP: Dict[str, Set[str]] = {
    '干': {'干', '乾', '幹', '榦', '乹'},      # 5 个变体，排序相邻
    '里': {'里', '裏', '裡'},
    '一': {'一', '壹'},
    '不': {'不', '否'},
    '春': {'春', '萅', '旾'},
    '秋': {'秋', '秌', '龝'},
}

def get_variants(char: str) -> Set[str]:
    return VARIANT_MAP.get(char, {char})


def build_full_regex(keyword: str):
    parts = []
    has_variant = False
    for ch in keyword:
        variants = get_variants(ch)
        if len(variants) > 1:
            has_variant = True
            chars = ''.join(re.escape(v) for v in sorted(variants))
            parts.append(f'[{chars}]')
        else:
            parts.append(re.escape(ch))
    if not has_variant:
        return None
    return re.compile('^' + ''.join(parts))


# ============================================================
# 模拟真实词典：block 级别 + LRU 缓存 + 解压开销
# ============================================================
BLOCK_SIZE = 1000          # 每 block 约 1000 条
MAX_KEY_CACHE = 10         # 与真实代码一致
DECOMPRESS_COST = 0.05     # 模拟单次 block 解压耗时（ms），含 zlib + split


class FakeDict:
    """模拟已排序的 MDX 词典，带 LRU block 缓存"""

    def __init__(self, total_entries: int, high_freq_char: str, high_freq_ratio: float):
        self.total = total_entries
        self.num_blocks = (total_entries + BLOCK_SIZE - 1) // BLOCK_SIZE
        self._cache: OrderedDict[int, list] = OrderedDict()
        self._cache_misses = 0
        self._block_access_count = {}

        # 生成已排序的 key（模拟 Unicode 排序）
        # 高频字的所有变体集中在一段连续 block 中
        high_freq_variants = sorted(get_variants(high_freq_char))
        high_freq_count = int(total_entries * high_freq_ratio)

        keys = []
        # 高频字变体条目（排序相邻）
        for i in range(high_freq_count):
            v = high_freq_variants[i % len(high_freq_variants)]
            rest_len = 2 + (i % 5)
            rest = ''.join(chr(0x4E00 + (i * 7 + j) % 200) for j in range(rest_len))
            keys.append(v + rest)

        # 其他条目
        other_chars = [chr(c) for c in range(0x4E00, 0x9FFF, 50) if chr(c) not in high_freq_variants]
        for i in range(total_entries - high_freq_count):
            first = other_chars[i % len(other_chars)]
            rest_len = 2 + (i % 4)
            rest = ''.join(chr(0x4E00 + (i * 13 + j) % 200) for j in range(rest_len))
            keys.append(first + rest)

        keys.sort()  # 按 Unicode 排序

        # 分 block
        self.blocks = []
        for i in range(0, total_entries, BLOCK_SIZE):
            block_keys = keys[i:i + BLOCK_SIZE]
            # 存储为 (rec_offset, key_bytes)
            block_data = [(i + j, k.encode('utf-8')) for j, k in enumerate(block_keys)]
            self.blocks.append(block_data)

        # 构建 block meta（first/last key）
        self._key_blocks_meta = []
        for bidx, block in enumerate(self.blocks):
            self._key_blocks_meta.append({
                "first": block[0][1].decode('utf-8', errors='ignore'),
                "last": block[-1][1].decode('utf-8', errors='ignore'),
            })

        self._key_count_prefix = [i * BLOCK_SIZE for i in range(self.num_blocks + 1)]

    def _get_key_block(self, meta_idx: int) -> list:
        """模拟带 LRU 缓存和解压开销的 block 获取"""
        if meta_idx in self._cache:
            self._cache.move_to_end(meta_idx)
            return self._cache[meta_idx]
        # 缓存未命中：模拟解压耗时
        self._cache_misses += 1
        self._block_access_count[meta_idx] = self._block_access_count.get(meta_idx, 0) + 1
        time.sleep(DECOMPRESS_COST / 1000)  # 模拟解压耗时
        self._cache[meta_idx] = self.blocks[meta_idx]
        if len(self._cache) > MAX_KEY_CACHE:
            self._cache.popitem(last=False)
        return self.blocks[meta_idx]

    def reset_stats(self):
        self._cache.clear()
        self._cache_misses = 0
        self._block_access_count = {}

    def get_stats(self):
        return {
            'cache_misses': self._cache_misses,
            'decompress_ms': self._cache_misses * DECOMPRESS_COST,
            'unique_blocks_accessed': len(self._block_access_count),
            'total_block_accesses': sum(self._block_access_count.values()),
        }


# ============================================================
# 方案②（当前实现）：V 次独立 search_prefix + 外层正则过滤
# ============================================================
def approach_2_search_prefix(d: FakeDict, prefix: str) -> List[Tuple[str, int]]:
    """模拟当前 search_prefix：线性扫描 meta，解压匹配 block，decode 所有前缀匹配"""
    results = []
    prefix_lower = prefix.lower()
    prefix_bytes = prefix_lower.encode('utf-8')
    for idx, meta in enumerate(d._key_blocks_meta):
        if meta["last"].lower() < prefix_lower:
            continue
        if meta["first"].lower() > prefix_lower and not meta["first"].lower().startswith(prefix_lower):
            break
        keys_block = d._get_key_block(idx)
        base_abs_idx = d._key_count_prefix[idx]
        for local_idx, (rec_offset, key_bytes) in enumerate(keys_block):
            key_lower_bytes = key_bytes.lower()
            if key_lower_bytes.startswith(prefix_bytes):
                key_str = key_bytes.decode('utf-8', errors='ignore')
                results.append((key_str, base_abs_idx + local_idx))
            elif key_lower_bytes > prefix_bytes:
                break
    return results


def approach_2(d: FakeDict, keyword: str) -> Tuple[List, dict]:
    """方案②：遍历首字异体，各自 search_prefix，外层正则过滤"""
    regex = build_full_regex(keyword)
    if regex is None:
        return [], {}
    first_char_variants = sorted(get_variants(keyword[0]))
    results = []
    seen = set()
    total_collected = 0
    for first_v in first_char_variants:
        candidates = approach_2_search_prefix(d, first_v)
        total_collected += len(candidates)
        for key, idx in candidates:
            if idx not in seen:
                seen.add(idx)
                if regex.match(key):
                    results.append((key, idx))
    return results, {'total_collected': total_collected, **d.get_stats()}


# ============================================================
# 方案①：单次遍历，正则在 block 内匹配，去重变体 block 解压
# ============================================================
def approach_1(d: FakeDict, keyword: str) -> Tuple[List, dict]:
    """方案①：单次扫描所有 block，对每个 key 检查首字变体前缀 + 正则匹配"""
    regex = build_full_regex(keyword)
    if regex is None:
        return [], {}
    first_char_variants = sorted(get_variants(keyword[0]))
    first_variant_bytes = [v.lower().encode('utf-8') for v in first_char_variants]

    results = []
    seen = set()
    total_checked = 0

    # 单次扫描所有可能匹配的 block
    # 找到所有首字变体可能落入的 block 范围
    min_first = min(first_char_variants).lower()
    max_first = max(first_char_variants).lower()

    for idx, meta in enumerate(d._key_blocks_meta):
        if meta["last"].lower() < min_first:
            continue
        if meta["first"].lower() > max_first:
            break
        keys_block = d._get_key_block(idx)
        base_abs_idx = d._key_count_prefix[idx]
        for local_idx, (rec_offset, key_bytes) in enumerate(keys_block):
            key_lower_bytes = key_bytes.lower()
            # 检查是否匹配任一首字变体
            matched_first = False
            for fvb in first_variant_bytes:
                if key_lower_bytes.startswith(fvb):
                    matched_first = True
                    break
            if matched_first:
                total_checked += 1
                key_str = key_bytes.decode('utf-8', errors='ignore')
                if regex.match(key_str):
                    idx_abs = base_abs_idx + local_idx
                    if idx_abs not in seen:
                        seen.add(idx_abs)
                        results.append((key_str, idx_abs))
            elif key_lower_bytes > max_first.encode('utf-8'):
                break
    return results, {'total_checked': total_checked, **d.get_stats()}


# ============================================================
# 基准测试
# ============================================================
def benchmark():
    print('=' * 80)
    print('真实场景：重叠 block + LRU 缓存驱逐 + 高频首字物化')
    print(f'参数：BLOCK_SIZE={BLOCK_SIZE}, MAX_KEY_CACHE={MAX_KEY_CACHE}, 模拟解压={DECOMPRESS_COST}ms/block')
    print('=' * 80)

    test_cases = [
        # (keyword, total, high_freq_ratio, desc)
        ('干净', 100000, 0.05, '10万条，"干"匹配5%（5变体，块高度重叠）'),
        ('干净', 100000, 0.10, '10万条，"干"匹配10%（高频字）'),
        ('干净', 500000, 0.05, '50万条，"干"匹配5%'),
        ('一切', 100000, 0.08, '10万条，"一"匹配8%（2变体）'),
        ('不是', 100000, 0.06, '10万条，"不"匹配6%（2变体）'),
    ]

    for keyword, total, hf_ratio, desc in test_cases:
        high_freq_char = keyword[0]
        d = FakeDict(total, high_freq_char, hf_ratio)
        first_variants = sorted(get_variants(high_freq_char))

        # 方案②
        d.reset_stats()
        t0 = time.perf_counter()
        r2, stats2 = approach_2(d, keyword)
        t2 = time.perf_counter() - t0

        # 方案①
        d.reset_stats()
        t0 = time.perf_counter()
        r1, stats1 = approach_1(d, keyword)
        t1 = time.perf_counter() - t0

        assert len(r1) == len(r2), f'结果不一致 ①={len(r1)} ②={len(r2)}'

        print(f'\n【{desc}】关键词="{keyword}"  首字变体数={len(first_variants)}  命中={len(r1)}')
        print(f'  {"指标":<25} {"方案①":<20} {"方案②":<20}')
        print(f'  {"-"*65}')
        print(f'  {"总耗时":<25} {t1*1000:<20.2f}ms {t2*1000:<20.2f}ms')
        print(f'  {"block 解压次数":<25} {stats1["cache_misses"]:<20} {stats2["cache_misses"]:<20}')
        print(f'  {"解压耗时(模拟)":<25} {stats1["decompress_ms"]:<20.1f}ms {stats2["decompress_ms"]:<20.1f}ms')
        print(f'  {"访问不同 block 数":<25} {stats1["unique_blocks_accessed"]:<20} {stats2["unique_blocks_accessed"]:<20}')
        print(f'  {"block 总访问次数":<25} {stats1["total_block_accesses"]:<20} {stats2["total_block_accesses"]:<20}')
        collected_key = 'total_checked' if 'total_checked' in stats1 else 'total_collected'
        print(f'  {"decode+正则条目数":<25} {stats1.get(collected_key, "-"):<20} {stats2.get("total_collected", "-"):<20}')
        if t2 > 0:
            print(f'  {"方案① vs ②":<25} {"":<20} {t1/t2:.2f}x')

        # 缓存驱逐分析
        repeats_2 = stats2['total_block_accesses'] - stats2['unique_blocks_accessed']
        repeats_1 = stats1['total_block_accesses'] - stats1['unique_blocks_accessed']
        print(f'  {"重复解压(缓存驱逐)":<25} {repeats_1:<20} {repeats_2:<20}')


if __name__ == '__main__':
    benchmark()
