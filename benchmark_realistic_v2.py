# -*- coding: utf-8 -*-
"""
真实场景基准 v2：修正方案①实现

方案①的正确做法：
  1. 对每个首字变体，计算其可能匹配的 block 范围（用 first/last 二分定位）
  2. 合并所有变体的 block 范围，去重
  3. 每个 block 只解压一次
  4. 在 block 内，对每个 key 检查：是否匹配任一首字变体前缀 → 正则匹配

方案②（当前实现）：
  1. V 个变体各自独立调 search_prefix
  2. 重叠 block 被解压 V 次（或靠缓存，但 MAX_KEY_CACHE=10 会驱逐）
  3. 每个变体的所有前缀匹配条目被 decode 并收集
  4. 外层正则过滤

关键修正：按拼音排序，异体字变体落在相同/相邻 block（真实词典场景）
"""

import re
import time
from collections import OrderedDict
from typing import List, Set, Dict, Tuple


# ============================================================
# 异体字映射
# ============================================================
VARIANT_MAP: Dict[str, Set[str]] = {
    '干': {'干', '乾', '幹', '榦', '乹'},
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
# 模拟词典：按"拼音排序"，异体字变体落在相邻 block
# ============================================================
BLOCK_SIZE = 1000
MAX_KEY_CACHE = 10
DECOMPRESS_COST = 0.05  # ms/block 模拟解压（zlib + split）


class FakeDict:
    def __init__(self, total_entries: int, high_freq_char: str, high_freq_ratio: float):
        self.total = total_entries
        self.num_blocks = (total_entries + BLOCK_SIZE - 1) // BLOCK_SIZE
        self._cache: OrderedDict[int, list] = OrderedDict()
        self._cache_misses = 0
        self._block_access_count = {}

        high_freq_variants = sorted(get_variants(high_freq_char))
        high_freq_count = int(total_entries * high_freq_ratio)

        # 关键：模拟拼音排序，所有变体的条目集中在连续区间
        # 生成 key 时，高频变体集中在前部，其余字均匀分布
        keys = []

        # 高频变体条目（连续区间，模拟拼音排序下干/乾/幹相邻）
        for i in range(high_freq_count):
            v = high_freq_variants[i % len(high_freq_variants)]
            rest_len = 2 + (i % 5)
            rest = ''.join(chr(0x4E00 + (i * 7 + j) % 200) for j in range(rest_len))
            keys.append(v + rest)

        # 其他条目
        other_count = total_entries - high_freq_count
        other_chars = [chr(c) for c in range(0x4E00, 0x9FFF, 7)
                       if chr(c) not in high_freq_variants]
        for i in range(other_count):
            first = other_chars[i % len(other_chars)]
            rest_len = 2 + (i % 4)
            rest = ''.join(chr(0x4E00 + (i * 13 + j) % 200) for j in range(rest_len))
            keys.append(first + rest)

        # 按变体分组内的顺序排序（模拟拼音排序下同音字相邻）
        # 高频变体保持连续，其他按字排序
        hf_keys = keys[:high_freq_count]
        other_keys = keys[high_freq_count:]
        other_keys.sort()
        keys = hf_keys + other_keys

        # 分 block
        self.blocks = []
        for i in range(0, total_entries, BLOCK_SIZE):
            block_keys = keys[i:i + BLOCK_SIZE]
            block_data = [(i + j, k.encode('utf-8')) for j, k in enumerate(block_keys)]
            self.blocks.append(block_data)

        self._key_blocks_meta = []
        for bidx, block in enumerate(self.blocks):
            self._key_blocks_meta.append({
                "first": block[0][1].decode('utf-8', errors='ignore'),
                "last": block[-1][1].decode('utf-8', errors='ignore'),
            })
        self._key_count_prefix = [i * BLOCK_SIZE for i in range(self.num_blocks + 1)]

    def _get_key_block(self, meta_idx: int) -> list:
        if meta_idx in self._cache:
            self._cache.move_to_end(meta_idx)
            return self._cache[meta_idx]
        self._cache_misses += 1
        self._block_access_count[meta_idx] = self._block_access_count.get(meta_idx, 0) + 1
        time.sleep(DECOMPRESS_COST / 1000)
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
            'unique_blocks': len(self._block_access_count),
            'total_accesses': sum(self._block_access_count.values()),
        }

    def find_block_range(self, prefix: str) -> Tuple[int, int]:
        """二分定位 prefix 可能匹配的 block 范围 [start, end)"""
        prefix_lower = prefix.lower()
        start, end = 0, len(self._key_blocks_meta)
        for idx, meta in enumerate(self._key_blocks_meta):
            if meta["last"].lower() < prefix_lower:
                start = idx + 1
            elif meta["first"].lower() > prefix_lower:
                end = idx
                break
        return start, end


# ============================================================
# 方案②（当前实现）：V 次独立 search_prefix
# ============================================================
def approach_2_search_prefix(d: FakeDict, prefix: str) -> List[Tuple[str, int]]:
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
    return results, {'collected': total_collected, **d.get_stats()}


# ============================================================
# 方案①（优化版）：合并 block 范围，每个 block 只解压一次
# ============================================================
def approach_1(d: FakeDict, keyword: str) -> Tuple[List, dict]:
    """
    1. 对每个变体计算 block 范围
    2. 合并去重所有 block idx
    3. 每个 block 只解压一次
    4. block 内：首字变体前缀检查 + 正则匹配
    """
    regex = build_full_regex(keyword)
    if regex is None:
        return [], {}
    first_char_variants = sorted(get_variants(keyword[0]))
    first_variant_bytes = [v.lower().encode('utf-8') for v in first_char_variants]

    # 步骤1+2：计算所有变体的 block 范围并合并
    block_indices = set()
    for first_v in first_char_variants:
        start, end = d.find_block_range(first_v)
        block_indices.update(range(start, end))

    # 步骤3+4：按顺序遍历 block，每个只解压一次
    results = []
    seen = set()
    total_checked = 0
    for idx in sorted(block_indices):
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
    return results, {'checked': total_checked, **d.get_stats()}


# ============================================================
# 基准测试
# ============================================================
def benchmark():
    print('=' * 85)
    print('真实场景 v2：异体字变体在相同 block（拼音排序）')
    print(f'BLOCK_SIZE={BLOCK_SIZE}, MAX_KEY_CACHE={MAX_KEY_CACHE}, 解压模拟={DECOMPRESS_COST}ms/block')
    print('=' * 85)

    test_cases = [
        ('干净', 100000, 0.05, '10万条，"干"5%（5变体同block）'),
        ('干净', 100000, 0.10, '10万条，"干"10%（高频，5变体）'),
        ('干净', 500000, 0.05, '50万条，"干"5%'),
        ('一切', 100000, 0.08, '10万条，"一"8%（2变体）'),
        ('不是', 100000, 0.06, '10万条，"不"6%（2变体）'),
        ('春秋', 100000, 0.04, '10万条，"春"4%（3变体）'),
    ]

    for keyword, total, hf_ratio, desc in test_cases:
        high_freq_char = keyword[0]
        d = FakeDict(total, high_freq_char, hf_ratio)
        first_variants = sorted(get_variants(high_freq_char))

        # 方案②
        d.reset_stats()
        t0 = time.perf_counter()
        r2, s2 = approach_2(d, keyword)
        t2 = time.perf_counter() - t0

        # 方案①
        d.reset_stats()
        t0 = time.perf_counter()
        r1, s1 = approach_1(d, keyword)
        t1 = time.perf_counter() - t0

        assert len(r1) == len(r2), f'结果不一致 ①={len(r1)} ②={len(r2)}'

        repeats_2 = s2['total_accesses'] - s2['unique_blocks']
        repeats_1 = s1['total_accesses'] - s1['unique_blocks']

        print(f'\n【{desc}】变体数={len(first_variants)}  正则命中={len(r1)}')
        print(f'  {"指标":<22} {"方案①":<18} {"方案②":<18}')
        print(f'  {"─"*60}')
        print(f'  {"总耗时":<22} {t1*1000:<18.2f}ms {t2*1000:<18.2f}ms')
        print(f'  {"解压 block 次数":<22} {s1["cache_misses"]:<18} {s2["cache_misses"]:<18}')
        print(f'  {"解压耗时(模拟)":<22} {s1["decompress_ms"]:<18.1f}ms {s2["decompress_ms"]:<18.1f}ms')
        print(f'  {"不同 block 数":<22} {s1["unique_blocks"]:<18} {s2["unique_blocks"]:<18}')
        print(f'  {"block 总访问":<22} {s1["total_accesses"]:<18} {s2["total_accesses"]:<18}')
        print(f'  {"重复解压次数":<22} {repeats_1:<18} {repeats_2:<18}')
        print(f'  {"decode条目数":<22} {s1.get("checked","-"):<18} {s2.get("collected","-"):<18}')
        if t2 > 0:
            print(f'  {"方案①/② 耗时比":<22} {"":<18} {t1/t2:.2f}x')


if __name__ == '__main__':
    benchmark()
