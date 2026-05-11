# -*- coding: utf-8 -*-
import os
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
        self.mdd = None
        self.loaded = False
        self.variant_handler = None
        self._entry_cache: Dict[tuple, str] = {}  # 修改：缓存键改为 (key, idx) 元组

    def load(self, variant_handler=None) -> bool:
        # 新任务开始：空行分隔 + 显示路径
        print(f"\n[词典] {self.path}")
        
        try:
            self.mdx = CachedMDX(self.path, encoding='utf-8')
            mdd_path = os.path.join(self.folder_path, self.name + '.mdd')
            if os.path.exists(mdd_path):
                self.mdd = CachedMDD(mdd_path, encoding='utf-8')
            self.variant_handler = variant_handler
            self.loaded = True

            # 加载成功：显示解析后的详细信息（不含路径）
            self._print_dict_info()
            
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
        if (not title or 
            'No HTML code allowed' in title or 
            title.lower() == 'title' or
            title == self.name):
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
        """
        获取词典的词条总数
        
        优先级：
        1. 从 CachedMDX 的 _key_blocks_meta 缓存中计算（最准确）
        2. 从 base_mdx._num_entries 获取（可能在某些情况下为0）
        3. 返回 0 作为兜底
        """
        try:
            # 方法1：从缓存的 key blocks 元数据计算（推荐）
            if hasattr(self.mdx, '_key_blocks_meta') and self.mdx._key_blocks_meta:
                total = sum(meta.get("count", 0) for meta in self.mdx._key_blocks_meta)
                if total > 0:
                    return total

            # 方法2：从 base_mdx 获取（可能为0如果使用了缓存加载）
            base_num = getattr(self.mdx.base_mdx, '_num_entries', 0)
            if base_num > 0:
                return base_num

            # 方法3：通过 len() 获取（如果实现了 __len__）
            try:
                return len(self.mdx.base_mdx)
            except:
                pass

            return 0
        except Exception as e:
            print(f"[调试] 获取词条数量失败: {e}")
            return 0

    def get_resource(self, path: str) -> Optional[bytes]:
        if not self.mdd:
            return None
        return self.mdd.get(path)

    def search(self, keyword: str, use_variants: bool) -> list:
        if not self.loaded or not keyword:
            return []
        self._entry_cache.clear()
        results = []
        seen_idx = set()  # 修改：使用 idx 去重，避免异体字搜索重复返回同一词条
        for key, idx in self.mdx.search_prefix(keyword, max_results=50):
            if idx not in seen_idx:
                seen_idx.add(idx)
                results.append((key, idx))
        if use_variants and self.variant_handler and self.variant_handler.should_expand(keyword):
            for v_kw in self.variant_handler.expand_keyword(keyword):
                for key, idx in self.mdx.search_prefix(v_kw, max_results=50):
                    if idx not in seen_idx:
                        seen_idx.add(idx)
                        results.append((key, idx))
        return results

    def get_content(self, key: str, idx: int = None) -> str:  # 修改：增加 idx 参数
        # 优先使用 idx 获取，解决同名词条只加载第一条的问题
        if idx is not None:
            if (key, idx) in self._entry_cache:
                return self._entry_cache[(key, idx)]
            c = self.mdx.get_by_index(idx)
            if not c:
                return ""
            c_stripped = c.strip() if isinstance(c, str) else c.decode('utf-8', errors='ignore').strip()
            if c_stripped.startswith("@@@LINK="):
                target_word = c_stripped.replace("@@@LINK=", "").strip()
                if target_word:
                    target_html = self.get_content(target_word)
                    return target_html if target_html else f'<div style="padding:8px;color:#888;">🔗 参见词条：<b>{target_word}</b></div>'
            self._entry_cache[(key, idx)] = c_stripped
            return c_stripped

        # 兼容旧调用：如果没有传 idx，退回只用 key 查询的逻辑
        if key in self._entry_cache:
            return self._entry_cache[key]
        search_res = self.mdx.search_prefix(key, max_results=1)
        if not search_res:
            return ""
        matched_key, idx = search_res[0]
        if matched_key != key:
            return ""
        return self.get_content(key, idx)
    
    def close(self):
        if self.mdx:
            self.mdx.close()
        if self.mdd:
            self.mdd.close()
