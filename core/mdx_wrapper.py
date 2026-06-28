# -*- coding: utf-8 -*-
import os
import re
from typing import List, Dict, Optional
from libs.readmdict import CachedMDX, CachedMDD

class MdxWrapper:
    def __init__(self, mdx_path: str):
        self.mdx_path = mdx_path
        self.folder_path = os.path.dirname(mdx_path)
        self.path = mdx_path
        self.name = os.path.splitext(os.path.basename(mdx_path))[0]
        self.dict_id = os.path.abspath(mdx_path)
        
        self.mdx = None
        self.mdds: List[CachedMDD] = []  # 修改：支持多个 MDD
        self.loaded = False
        self.variant_handler = None
        self._cached_entry_count = None  # 缓存词条数

    def load(self, variant_handler=None) -> bool:
        # 新任务开始：空行分隔 + 显示路径
        print(f"\n[词典] {self.path}")
        try:
            self.mdx = CachedMDX(self.path, encoding='utf-8')
            
            # 加载主 MDD：name.mdd
            mdd_path = os.path.join(self.folder_path, self.name + '.mdd')
            if os.path.exists(mdd_path):
                self.mdds.append(CachedMDD(mdd_path, encoding='utf-8'))
                
            # 加载分卷 MDD：name.1.mdd, name.2.mdd ...
            seq = 1
            while True:
                seq_mdd_path = os.path.join(self.folder_path, f"{self.name}.{seq}.mdd")
                if not os.path.exists(seq_mdd_path):
                    break
                self.mdds.append(CachedMDD(seq_mdd_path, encoding='utf-8'))
                seq += 1
                
            self.variant_handler = variant_handler
            self.loaded = True
            
            # 加载成功：显示解析后的详细信息（不含路径）
            self._print_dict_info()
            
            # 输出 MDD 加载情况
            if self.mdds:
                print(f" 资源: 检测到 {len(self.mdds)} 个 MDD 文件 (主文件 + {len(self.mdds)-1} 个分卷)" if len(self.mdds) > 1 else f" 资源: 检测到 1 个 MDD 文件")
            
            return True
        except Exception as e:
            print(f"[失败] {e}")
            return False

    def _print_dict_info(self):
        """输出词典的 Header 元数据信息"""
        try:
            if not self.mdx or not hasattr(self.mdx, 'base_mdx'):
                return
            header = self.mdx.base_mdx.header

            # 获取标题并判断来源
            title = self._get_title(header)
            title_source = self._get_title_source(header)

            # 显示标题（带来源标识）
            if title_source == 'header':
                print(f"  {title} (来自Header)")
            else:
                print(f"  {title} (文件名)")

            # 描述
            description = self._get_description(header)
            if description:
                if len(description) > 100:
                    print(f"  描述: {description[:100]}...")
                else:
                    print(f"  描述: {description}")

            # 技术参数（单行紧凑显示）
            version = self._decode_field(header.get(b'GeneratedByEngineVersion', b''))
            creation_date = self._decode_field(header.get(b'CreationDate', b''))
            encoding = self._decode_field(header.get(b'Encoding', b''))
            tech_parts = []
            if version:
                tech_parts.append(f"MDX v{version}")
            if encoding:
                tech_parts.append(encoding)
            if creation_date:
                tech_parts.append(creation_date)
            if tech_parts:
                print(f"  参数: {' | '.join(tech_parts)}")

            # 词条统计
            num_entries = self._get_entry_count()
            print(f"  词条: {num_entries:,} 条")
        except Exception as e:
            print(f"[警告] 读取词典元数据失败: {e}")

    def _get_title_source(self, header: dict) -> str:
        """判断标题来源：'header' 或 'filename'"""
        title_raw = header.get(b'Title', b'')
        if isinstance(title_raw, bytes):
            title = title_raw.decode('utf-8', errors='ignore').strip()
        else:
            title = str(title_raw).strip()

        # 如果Title为空、异常值或与文件名相同，则来源为filename
        if (not title or 'No HTML code allowed' in title or title.lower() == 'title' or title == self.name):
            return 'filename'
        return 'header'

    def _get_title(self, header: dict) -> str:
        """提取并清理标题"""
        title_raw = header.get(b'Title', b'')
        if isinstance(title_raw, bytes):
            title = title_raw.decode('utf-8', errors='ignore').strip()
        else:
            title = str(title_raw).strip()

        # 清理异常值
        if not title or 'No HTML code allowed' in title or title.lower() == 'title':
            return self.name
        return title if title else self.name

    def _get_description(self, header: dict) -> str:
        """提取描述信息"""
        desc_raw = header.get(b'Description', b'')
        if isinstance(desc_raw, bytes):
            return desc_raw.decode('utf-8', errors='ignore').strip()
        return str(desc_raw).strip()

    @staticmethod
    def _decode_field(raw_value) -> str:
        """解码字段值（统一处理 bytes/str）"""
        if isinstance(raw_value, bytes):
            return raw_value.decode('utf-8', errors='ignore').strip()
        return str(raw_value).strip() if raw_value else ''

    def _get_entry_count(self) -> int:
        """获取词典的词条总数（缓存优化）"""
        if self._cached_entry_count is not None:
            return self._cached_entry_count
        try:
            if hasattr(self.mdx, '_key_blocks_meta') and self.mdx._key_blocks_meta:
                total = sum(meta.get("count", 0) for meta in self.mdx._key_blocks_meta)
                if total > 0:
                    self._cached_entry_count = total
                    return total

            base_num = getattr(self.mdx.base_mdx, '_num_entries', 0)
            if base_num > 0:
                self._cached_entry_count = base_num
                return base_num

            self._cached_entry_count = 0
            return 0
        except Exception as e:
            print(f"[调试] 获取词条数量失败: {e}")
            self._cached_entry_count = 0
            return 0

    def search(self, keyword: str, use_variants: bool) -> list:
        if not self.loaded or not keyword:
            return []
        results = []
        seen_idx = set()

        # 无异体字或不需要展开：普通前缀搜索
        if not (use_variants and self.variant_handler):
            for key, idx in self.mdx.search_prefix(keyword, max_results=50):
                if idx not in seen_idx:
                    seen_idx.add(idx)
                    results.append((key, idx))
            return results

        # ===== 异体字搜索：两步方案 =====
        # 单字搜索词：直接展开所有异体字组合做前缀搜索
        if len(keyword) == 1:
            for v_kw in self.variant_handler.generate_combinations(keyword):
                for key, idx in self.mdx.search_prefix(v_kw, max_results=50):
                    if idx not in seen_idx:
                        seen_idx.add(idx)
                        results.append((key, idx))
                if len(results) >= 50:
                    break
            return results

        # 多字搜索词（len > 1）：两步方案
        # 第一步：用第一个字的异体字做前缀搜索（高效，有 block 级 skip + 长度预过滤）
        # 第二步：在第一步的结果集上用正则过滤剩余字符的异体字组合
        regex = self.variant_handler.build_full_regex(keyword, exact=False)
        if regex is None:
            # 构建失败，回退到普通搜索
            for key, idx in self.mdx.search_prefix(keyword, max_results=50):
                if idx not in seen_idx:
                    seen_idx.add(idx)
                    results.append((key, idx))
            return results

        first_char_variants = sorted(self.variant_handler.get_variants(keyword[0]))
        min_len = len(keyword)  # 长度预过滤：短于搜索词长度的 key 直接跳过

        for first_variant in first_char_variants:
            for key, idx in self.mdx.search_prefix(first_variant, max_results=5000, min_len=min_len):
                if idx in seen_idx:
                    continue
                seen_idx.add(idx)
                # 第二步：内存中用正则精确匹配
                if regex.match(key):
                    results.append((key, idx))
                    if len(results) >= 50:
                        return results
        return results

    def get_content(self, key: str, idx: int = None, _link_depth: int = 0) -> str:
        if idx is not None:
            try:
                c = self.mdx.get_by_index(idx)
            except Exception as e:
                return f'<div style="padding:8px;color:red;">⚠ 内容解析失败：{e}</div>'
            if not c:
                return ""

            c_stripped = c.strip() if isinstance(c, str) else c.decode('utf-8', errors='ignore').strip()
            if c_stripped.startswith("@@@LINK="):
                # 递归深度限制，防止循环引用导致栈溢出
                if _link_depth >= 10:
                    return f'<div style="padding:8px;color:#888;">⚠ 参见层级过深：<b>{key}</b></div>'
                target_word = c_stripped.replace("@@@LINK=", "").strip()
                if target_word:
                    target_html = self.get_content(target_word, _link_depth=_link_depth + 1)
                    return target_html if target_html else f'<div style="padding:8px;color:#888;">🔗 参见词条：<b>{target_word}</b></div>'
            return c_stripped

        # 兼容旧调用：如果没有传 idx，退回只用 key 查询的逻辑
        search_res = self.mdx.search_prefix(key, max_results=1)
        if not search_res:
            return ""
        matched_key, idx = search_res[0]
        if matched_key != key:
            return ""
        return self.get_content(key, idx)

    def get_resource(self, path: str) -> bytes:
        """按顺序在多个 MDD 中查找资源"""
        for mdd in self.mdds:
            try:
                data = mdd.get(path)
                if data is not None:
                    if isinstance(data, str):
                        try:
                            return data.encode('utf-8')
                        except UnicodeEncodeError:
                            try:
                                return data.encode('gbk', errors='ignore')
                            except Exception as e:
                                print(f"[警告] 编码转换失败: {e}")
                                return data.encode('utf-8', errors='ignore')
                    return data
            except Exception:
                continue
        return None

    def close(self):
        if self.mdx:
            self.mdx.close()
        for mdd in self.mdds:
            mdd.close()
        self.mdds.clear()
