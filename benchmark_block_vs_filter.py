# -*- coding: utf-8 -*-
"""
对比两种异体字搜索方案：

方案①：遍历首字异体定位区块，正则匹配区块内条目
       - 正则在 block 扫描时就应用，不匹配的 key 跳过 decode
       - search_prefix 内部集成正则过滤

方案②：遍历首字获取全部条目，正则过滤条目（当前实现）
       - search_prefix 返回所有前缀匹配的条目（已 decode）
       - 在外层用正则过滤
"""

import re
import timeit
import random
from typing import List, Set, Dict, Tuple


# ============================================================
# 异体字映射表
# ============================================================
VARIANT_MAP: Dict[str, Set[str]] = {
    '丘': {'丘', '坵'},
    '里': {'里', '裏', '裡'},
    '歌': {'歌', '謌'},
    '燕': {'燕', '鷰', '䴏'},
    '舞': {'舞', '儛'},
    '应': {'应', '應'},
    '急': {'急'},
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
# 模拟 key block 数据结构
# ============================================================
def generate_key_blocks(total_entries, keyword, first_char_variants, match_ratio=0.1):
    """模拟已排序的 key blocks

    返回: [(rec_offset, key_bytes), ...] 列表
    """
    keys = []
    match_count = int(total_entries * match_ratio)

    # 匹配首字的条目
    for i in range(match_count):
        first = random.choice(first_char_variants)
        rest_len = random.randint(2, 6)
        rest = ''.join(random.choice(
            '里裏裡歌謌燕鷰舞儛应應急春萅旾秋秌龝山河风雨'
        ) for _ in range(rest_len))
        keys.append((i, (first + rest).encode('utf-8')))

    # 不匹配首字的条目
    other_first = list('天气人山水地火木金日月星风云雨雪abcdefghijklmnopqrstuvwxyz')
    for i in range(total_entries - match_count):
        rest_len = random.randint(2, 6)
        first = random.choice(other_first)
        rest = ''.join(random.choice(
            'abcdefg候歌山河风雨春花秋月人事知'
        ) for _ in range(rest_len))
        keys.append((match_count + i, (first + rest).encode('utf-8')))

    # 按 key_bytes 排序（模拟词典排序）
    keys.sort(key=lambda x: x[1])
    return keys


# ============================================================
# 方案①：正则在 block 扫描时应用（不匹配跳过 decode）
# ============================================================
def search_regex_in_block(keys_block, prefix_lower, regex):
    """
    在 block 内扫描时，先用字节级前缀匹配，再用正则过滤，最后才 decode
    """
    prefix_bytes = prefix_lower.encode('utf-8')
    results = []
    for rec_offset, key_bytes in keys_block:
        key_lower_bytes = key_bytes.lower()
        if key_lower_bytes.startswith(prefix_bytes):
            # 先 decode（正则需要字符串）
            key_str = key_bytes.decode('utf-8', errors='ignore')
            if regex.match(key_str):
                results.append((key_str, rec_offset))
        elif key_lower_bytes > prefix_bytes:
            break
    return results


def approach_1(keys_block, keyword, first_char_variants):
    """方案①：遍历首字异体，正则在 block 内匹配"""
    regex = build_full_regex(keyword)
    if regex is None:
        return []
    results = []
    seen = set()
    for first_v in first_char_variants:
        for key, idx in search_regex_in_block(keys_block, first_v.lower(), regex):
            if idx not in seen:
                seen.add(idx)
                results.append((key, idx))
    return results


# ============================================================
# 方案②：先获取全部前缀匹配条目，再正则过滤（当前实现）
# ============================================================
def search_prefix_only(keys_block, prefix_lower):
    """只做前缀匹配，返回所有命中条目（已 decode）"""
    prefix_bytes = prefix_lower.encode('utf-8')
    results = []
    for rec_offset, key_bytes in keys_block:
        key_lower_bytes = key_bytes.lower()
        if key_lower_bytes.startswith(prefix_bytes):
            key_str = key_bytes.decode('utf-8', errors='ignore')
            results.append((key_str, rec_offset))
        elif key_lower_bytes > prefix_bytes:
            break
    return results


def approach_2(keys_block, keyword, first_char_variants):
    """方案②：先获取全部前缀匹配条目，再正则过滤"""
    regex = build_full_regex(keyword)
    if regex is None:
        return []
    results = []
    seen = set()
    for first_v in first_char_variants:
        for key, idx in search_prefix_only(keys_block, first_v.lower()):
            if idx not in seen:
                seen.add(idx)
                if regex.match(key):
                    results.append((key, idx))
    return results


# ============================================================
# 方案①的优化版：正则预编译为字节级匹配，完全跳过 decode
# ============================================================
def build_parallel_keys_bytes(keyword):
    """将正则字符集预编译为字节列表"""
    parallel = []
    for ch in keyword:
        variants = sorted(get_variants(ch))
        parallel.append([v.encode('utf-8') for v in variants])
    return parallel


def bytes_match(key_bytes, parallel_keys):
    """字节级匹配，等效于正则但无需 decode"""
    offset = 0
    for pos_variants in parallel_keys:
        matched = False
        for vb in pos_variants:
            if key_bytes.startswith(vb, offset):
                offset += len(vb)
                matched = True
                break
        if not matched:
            return False
    return True


def approach_1_bytes(keys_block, keyword, first_char_variants):
    """方案①优化版：字节级匹配，完全跳过 decode"""
    parallel_keys = build_parallel_keys_bytes(keyword)
    first_variant_bytes = [v.encode('utf-8') for v in first_char_variants]
    results = []
    seen = set()

    for fvb in first_variant_bytes:
        prefix_bytes = fvb.lower()
        for rec_offset, key_bytes in keys_block:
            key_lower = key_bytes.lower()
            if key_lower.startswith(prefix_bytes):
                if bytes_match(key_bytes, parallel_keys):
                    if rec_offset not in seen:
                        seen.add(rec_offset)
                        results.append((key_bytes.decode('utf-8', errors='ignore'), rec_offset))
            elif key_lower > prefix_bytes:
                break
    return results


# ============================================================
# 基准测试
# ============================================================
def benchmark():
    print('=' * 75)
    print('方案①（正则在 block 内匹配） vs 方案②（先全取再正则过滤）')
    print('=' * 75)

    test_cases = [
        # (keyword, total_entries, match_ratio, desc)
        ('丘里', 10000, 0.05, '10000条，首字匹配率5%'),
        ('丘里', 10000, 0.10, '10000条，首字匹配率10%'),
        ('丘里', 10000, 0.30, '10000条，首字匹配率30%'),
        ('丘里', 50000, 0.10, '50000条，首字匹配率10%'),
        ('歌燕舞', 10000, 0.10, '3字词，首字匹配率10%'),
        ('春秋', 10000, 0.10, '2字词，异体字多（春×3秋×3）'),
    ]

    for keyword, total, match_ratio, desc in test_cases:
        first_char_variants = sorted(get_variants(keyword[0]))
        keys_block = generate_key_blocks(total, keyword, first_char_variants, match_ratio)

        # 验证结果一致
        r1 = approach_1(keys_block, keyword, first_char_variants)
        r2 = approach_2(keys_block, keyword, first_char_variants)
        r1b = approach_1_bytes(keys_block, keyword, first_char_variants)
        assert len(r1) == len(r2) == len(r1b), \
            f'结果不一致! ①={len(r1)} ②={len(r2)} ①bytes={len(r1b)}'

        # 统计前缀匹配数 vs 正则命中数
        prefix_matches = 0
        for fv in first_char_variants:
            prefix_matches += len(search_prefix_only(keys_block, fv.lower()))
        regex_hits = len(r1)
        filter_ratio = regex_hits / prefix_matches if prefix_matches > 0 else 0

        n = 200
        t1 = timeit.timeit(lambda: approach_1(keys_block, keyword, first_char_variants), number=n)
        t2 = timeit.timeit(lambda: approach_2(keys_block, keyword, first_char_variants), number=n)
        t1b = timeit.timeit(lambda: approach_1_bytes(keys_block, keyword, first_char_variants), number=n)

        print(f'\n【{desc}】关键词={keyword}')
        print(f'  首字匹配: {prefix_matches} 条 | 正则命中: {regex_hits} 条 | 过滤比: {filter_ratio:.1%}')
        print(f'  方案①(正则在内):   {t1:.3f}s ({t1/n*1000:.3f}ms/次)')
        print(f'  方案②(先取后过滤): {t2:.3f}s ({t2/n*1000:.3f}ms/次)')
        print(f'  方案①(bytes优化):  {t1b:.3f}s ({t1b/n*1000:.3f}ms/次)')
        if t2 > 0:
            print(f'  ① vs ②: {t1/t2:.2f}x  |  ①bytes vs ②: {t1b/t2:.2f}x')

    # 极端场景
    print('\n' + '=' * 75)
    print('极端场景')
    print('=' * 75)

    # 首字匹配率极高（如搜"一"开头）
    keyword = '春秋'
    first_char_variants = sorted(get_variants(keyword[0]))
    keys_block = generate_key_blocks(10000, keyword, first_char_variants, 0.5)

    r1 = approach_1(keys_block, keyword, first_char_variants)
    r2 = approach_2(keys_block, keyword, first_char_variants)
    assert len(r1) == len(r2)

    prefix_matches = sum(len(search_prefix_only(keys_block, fv.lower())) for fv in first_char_variants)
    regex_hits = len(r1)

    n = 100
    t1 = timeit.timeit(lambda: approach_1(keys_block, keyword, first_char_variants), number=n)
    t2 = timeit.timeit(lambda: approach_2(keys_block, keyword, first_char_variants), number=n)
    t1b = timeit.timeit(lambda: approach_1_bytes(keys_block, keyword, first_char_variants), number=n)

    print(f'\n【首字匹配率50%】关键词={keyword}')
    print(f'  首字匹配: {prefix_matches} 条 | 正则命中: {regex_hits} 条')
    print(f'  方案①: {t1/n*1000:.3f}ms | 方案②: {t2/n*1000:.3f}ms | ①bytes: {t1b/n*1000:.3f}ms')
    print(f'  ① vs ②: {t1/t2:.2f}x | ①bytes vs ②: {t1b/t2:.2f}x')


if __name__ == '__main__':
    benchmark()
