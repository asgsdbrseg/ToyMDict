#!/usr/bin/env python
# -*- coding: utf-8 -*-
# readmdict.py
# Octopus MDict Dictionary File (.mdx) and Resource File (.mdd) Analyser
#
# Copyright (C) 2012, 2013, 2015, 2022, 2023 Xiaoqiang Wang <xiaoqiangwang AT gmail DOT com>
#
# This program is a free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3 of the License.
#
# You can get a copy of GNU General Public License along this program
# But you can always get it from http://www.gnu.org/licenses/gpl.txt
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.

from struct import pack, unpack
from io import BytesIO
import re
import sys
import os
import json
import threading
import itertools
from collections import OrderedDict
from bisect import bisect_left, bisect_right

# 尝试相对导入或绝对导入
try:
    from .ripemd128 import ripemd128
    from .pureSalsa20 import Salsa20
except ImportError:
    from ripemd128 import ripemd128
    from pureSalsa20 import Salsa20

from threading import Lock

# zlib compression is used for engine version >=2.0
import zlib

# LZO compression is used for engine version < 2.0
try:
    import lzo
except ImportError:
    lzo = None

# xxhash is used for engine version >= 3.0
try:
    import xxhash
except ImportError:
    xxhash = None

# 2x3 compatible
if sys.hexversion >= 0x03000000:
    unicode = str

def _unescape_entities(text):
    """ unescape offending tags < > " & """
    text = text.replace(b'&lt;', b'<')
    text = text.replace(b'&gt;', b'>')
    text = text.replace(b'&quot;', b'"')
    text = text.replace(b'&amp;', b'&')
    return text

def _fast_decrypt(data, key):
    """ XOR decryption """
    b = bytearray(data)
    key = bytearray(key)
    previous = 0x36
    for i in range(len(b)):
        t = (b[i] >> 4 | b[i] << 4) & 0xff
        t = t ^ previous ^ (i & 0xff) ^ key[i % len(key)]
        previous = b[i]
        b[i] = t
    return bytes(b)

def _salsa_decrypt(ciphertext, encrypt_key):
    """ salsa20 (8 rounds) decryption """
    s20 = Salsa20(key=encrypt_key, IV=b"\x00"*8, rounds=8)
    return s20.encryptBytes(ciphertext)

def _decrypt_regcode_by_userid(reg_code, userid):
    userid_digest = ripemd128(userid)
    s20 = Salsa20(key=userid_digest, IV=b"\x00"*8, rounds=8)
    encrypt_key = s20.encryptBytes(reg_code)
    return encrypt_key

class MDict(object):
    """ Base class which reads in header and key block. """
    def __init__(self, fname, encoding='', passcode=None, build_index=True):
        self._fname = fname
        self._encoding = encoding.upper()
        self._encrypted_key = None
        self.header = self._read_header()
        # decrypt regcode to get the encrypted key
        if passcode is not None:
            regcode, userid = passcode
            if isinstance(userid, unicode): userid = userid.encode('utf8')
            self._encrypted_key = _decrypt_regcode_by_userid(regcode, userid)
        # MDict 3.0 encryption key derives from UUID if present
        elif self._version >= 3.0:
            uuid = self.header.get(b'UUID')
            if uuid:
                if xxhash is None: raise RuntimeError('xxhash module is needed to read MDict 3.0 format')
                mid = (len(uuid) + 1) // 2
                self._encrypted_key = xxhash.xxh64_digest(uuid[:mid]) + xxhash.xxh64_digest(uuid[mid:])

        # 【关键修复点】：通过 build_index 控制是否在启动时全量加载 keys
        if build_index:
            self._key_list = self._read_keys()
        else:
            self._key_list = []

    def __len__(self):
        return self._num_entries

    def __iter__(self):
        return self.keys()

    def keys(self):
        """ Return an iterator over dictionary keys. """
        return (key_value for key_id, key_value in self._key_list)

    def _read_number(self, f):
        return unpack(self._number_format, f.read(self._number_width))[0]

    def _read_int32(self, f):
        return unpack('>I', f.read(4))[0]

    def _parse_header(self, header):
        """ extract attributes from <Dict attr="value" ... > """
        taglist = re.findall(rb'(\w+)="(.*?)"', header, re.DOTALL)
        tagdict = {}
        for key, value in taglist:
            tagdict[key] = _unescape_entities(value)
        return tagdict

    def _decode_block(self, block, decompressed_size):
        info = unpack('<L', block[:4])[0]
        compression_method = info & 0xf
        encryption_method = (info >> 4) & 0xf
        encryption_size = (info >> 8) & 0xff
        adler32 = unpack('>I', block[4:8])[0]
        encrypted_key = self._encrypted_key if self._encrypted_key is not None else ripemd128(block[4:8])
        data = block[8:]

        if encryption_method == 0: decrypted_block = data
        elif encryption_method == 1: decrypted_block = _fast_decrypt(data[:encryption_size], encrypted_key) + data[encryption_size:]
        elif encryption_method == 2: decrypted_block = _salsa_decrypt(data[:encryption_size], encrypted_key) + data[encryption_size:]
        else: raise Exception('encryption method %d not supported' % encryption_method)

        if self._version >= 3: assert(hex(adler32) == hex(zlib.adler32(decrypted_block) & 0xffffffff))

        if compression_method == 0: decompressed_block = decrypted_block
        elif compression_method == 1:
            if lzo is None: raise RuntimeError("LZO compression is not supported")
            header = b'\xf0' + pack('>I', decompressed_size)
            decompressed_block = lzo.decompress(header + decrypted_block)
        elif compression_method == 2: decompressed_block = zlib.decompress(decrypted_block)
        else: raise Exception('compression method %d not supported' % compression_method)

        if self._version < 3: assert(hex(adler32) == hex(zlib.adler32(decompressed_block) & 0xffffffff))
        return decompressed_block

    def _decode_key_block_info(self, key_block_info_compressed):
        if self._version >= 2:
            assert(key_block_info_compressed[:4] == b'\x02\x00\x00\x00')
            if self._encrypt & 0x02:
                key = ripemd128(key_block_info_compressed[4:8] + pack(b'<L', 0x3695))
                key_block_info_compressed = key_block_info_compressed[:8] + _fast_decrypt(key_block_info_compressed[8:], key)
            key_block_info = zlib.decompress(key_block_info_compressed[8:])
            adler32 = unpack('>I', key_block_info_compressed[4:8])[0]
            assert(adler32 == zlib.adler32(key_block_info) & 0xffffffff)
        else:
            key_block_info = key_block_info_compressed

        key_block_info_list = []
        num_entries = 0
        i = 0
        if self._version >= 2: byte_format, byte_width, text_term = '>H', 2, 1
        else: byte_format, byte_width, text_term = '>B', 1, 0

        while i < len(key_block_info):
            num_entries += unpack(self._number_format, key_block_info[i:i+self._number_width])[0]
            i += self._number_width
            text_head_size = unpack(byte_format, key_block_info[i:i+byte_width])[0]
            i += byte_width
            i += (text_head_size + text_term) * (2 if self._encoding == 'UTF-16' else 1)
            text_tail_size = unpack(byte_format, key_block_info[i:i+byte_width])[0]
            i += byte_width
            i += (text_tail_size + text_term) * (2 if self._encoding == 'UTF-16' else 1)
            key_block_compressed_size = unpack(self._number_format, key_block_info[i:i+self._number_width])[0]
            i += self._number_width
            key_block_decompressed_size = unpack(self._number_format, key_block_info[i:i+self._number_width])[0]
            i += self._number_width
            key_block_info_list += [(key_block_compressed_size, key_block_decompressed_size)]
        return key_block_info_list

    def _decode_key_block(self, key_block_compressed, key_block_info_list):
        key_list = []
        i = 0
        for compressed_size, decompressed_size in key_block_info_list:
            key_block = self._decode_block(key_block_compressed[i:i+compressed_size], decompressed_size)
            key_list += self._split_key_block(key_block)
            i += compressed_size
        return key_list

    def _split_key_block(self, key_block):
        key_list = []
        key_start_index = 0
        while key_start_index < len(key_block):
            key_id = unpack(self._number_format, key_block[key_start_index:key_start_index+self._number_width])[0]
            delimiter = b'\x00\x00' if self._encoding == 'UTF-16' else b'\x00'
            width = 2 if self._encoding == 'UTF-16' else 1
            i = key_start_index + self._number_width
            while i < len(key_block):
                if key_block[i:i+width] == delimiter: key_end_index = i; break
                i += width
            key_text = key_block[key_start_index+self._number_width:key_end_index].decode(self._encoding, errors='ignore').encode('utf-8').strip()
            key_start_index = key_end_index + width
            key_list += [(key_id, key_text)]
        return key_list

    def _read_header(self):
        with open(self._fname, 'rb') as f:
            header_bytes_size = unpack('>I', f.read(4))[0]
            header_bytes = f.read(header_bytes_size)
            adler32 = unpack('<I', f.read(4))[0]
            assert(adler32 == zlib.adler32(header_bytes) & 0xffffffff)
            self._key_block_offset = f.tell()

        if header_bytes[-2:] == b'\x00\x00': header_text = header_bytes[:-2].decode('utf-16').encode('utf-8')
        else: header_text = header_bytes[:-1]
        header_tag = self._parse_header(header_text)

        if not self._encoding:
            encoding = header_tag.get(b'Encoding', b'utf-8')
            if sys.hexversion >= 0x03000000: encoding = encoding.decode('utf-8')
            if encoding in ['GBK', 'GB2312']: encoding = 'GB18030'
            self._encoding = encoding

        if b'Encrypted' not in header_tag or header_tag[b'Encrypted'] == b'No': self._encrypt = 0
        elif header_tag[b'Encrypted'] == b'Yes': self._encrypt = 1
        else: self._encrypt = int(header_tag[b'Encrypted'])

        self._stylesheet = {}
        if header_tag.get(b'StyleSheet'):
            lines = header_tag[b'StyleSheet'].splitlines()
            for i in range(0, len(lines), 3): self._stylesheet[lines[i]] = (lines[i+1], lines[i+2])

        self._version = float(header_tag[b'GeneratedByEngineVersion'])
        if self._version < 2.0: self._number_width, self._number_format = 4, '>I'
        else: self._number_width, self._number_format = 8, '>Q'
        if self._version >= 3: self._encoding = 'UTF-8'
        return header_tag

    def _read_keys(self):
        if self._version >= 3: return self._read_keys_v3()
        else:
            if (self._encrypt & 0x01) and self._encrypted_key is None:
                print("Try Brutal Force on Encrypted Key Blocks")
                return self._read_keys_brutal()
            else: return self._read_keys_v1v2()

    def _read_keys_v3(self):
        with open(self._fname, 'rb') as f:
            f.seek(self._key_block_offset)
            while True:
                block_type = self._read_int32(f)
                block_size = self._read_number(f)
                block_offset = f.tell()
                if block_type == 0x01000000: self._record_block_offset = block_offset
                elif block_type == 0x02000000: self._record_index_offset = block_offset
                elif block_type == 0x03000000: self._key_data_offset = block_offset
                elif block_type == 0x04000000: self._key_index_offset = block_offset
                else: raise RuntimeError("Unknown block type %d" % block_type)
                f.seek(block_size, 1)
                if f.read(4): f.seek(-4, 1)
                else: break
            f.seek(self._key_data_offset)
            number = self._read_int32(f)
            total_size = self._read_number(f)
            key_list = []
            for i in range(number):
                decompressed_size = self._read_int32(f)
                compressed_size = self._read_int32(f)
                block_data = f.read(compressed_size)
                decompressed_block_data = self._decode_block(block_data, decompressed_size)
                key_list.extend(self._split_key_block(decompressed_block_data))
        self._num_entries = len(key_list)
        return key_list

    def _read_keys_v1v2(self):
        with open(self._fname, 'rb') as f:
            f.seek(self._key_block_offset)
            num_bytes = 8 * 5 if self._version >= 2.0 else 4 * 4
            block = f.read(num_bytes)
            if self._encrypt & 1: block = _salsa_decrypt(block, self._encrypted_key)
            sf = BytesIO(block)
            num_key_blocks = self._read_number(sf)
            self._num_entries = self._read_number(sf)

            if self._version >= 2.0:
                key_block_info_decomp_size = self._read_number(sf)
                key_block_info_size = self._read_number(sf)
                key_block_size = self._read_number(sf)
                f.read(4)
            else:
                key_block_info_size = self._read_number(sf)
                key_block_size = self._read_number(sf)

            key_block_info = f.read(key_block_info_size)
            key_block_info_list = self._decode_key_block_info(key_block_info)
            key_block_compressed = f.read(key_block_size)
            key_list = self._decode_key_block(key_block_compressed, key_block_info_list)
            self._record_block_offset = f.tell()
        self._num_entries = len(key_list)
        return key_list

    def _read_keys_brutal(self):
        with open(self._fname, 'rb') as f:
            f.seek(self._key_block_offset)
            num_bytes = 8 * 5 + 4 if self._version >= 2.0 else 4 * 4
            key_block_type = b'\x02\x00\x00\x00' if self._version >= 2.0 else b'\x01\x00\x00\x00'
            block = f.read(num_bytes)
            key_block_info = f.read(8)
            if self._version >= 2.0: assert key_block_info[:4] == b'\x02\x00\x00\x00'
            while True:
                fpos = f.tell()
                t = f.read(1024)
                index = t.find(key_block_type)
                if index != -1: key_block_info += t[:index]; f.seek(fpos + index); break
                else: key_block_info += t
            key_block_info_list = self._decode_key_block_info(key_block_info)
            key_block_size = sum(list(zip(*key_block_info_list))[0])
            key_block_compressed = f.read(key_block_size)
            key_list = self._decode_key_block(key_block_compressed, key_block_info_list)
            self._record_block_offset = f.tell()
        self._num_entries = len(key_list)
        return key_list

    def items(self):
        return self._read_records()

    def _read_records(self):
        if self._version >= 3: yield from self._read_records_v3()
        else: yield from self._read_records_v1v2()

    def _read_records_v3(self):
        record_index = self._read_record_index()
        with open(self._fname, 'rb') as f:
            f.seek(self._record_block_offset)
            offset = 0; i = 0; size_counter = 0
            num_record_blocks = self._read_int32(f)
            num_bytes = self._read_number(f)
            for j in range(num_record_blocks):
                decompressed_size = self._read_int32(f)
                compressed_size = self._read_int32(f)
                if (compressed_size + 8, decompressed_size) != record_index[j]:
                    compressed_size = record_index[j][0] - 8
                    decompressed_size = record_index[j][1]
                    print('Skip (potentially) damaged record block')
                    f.read(compressed_size); continue
                record_block = self._decode_block(f.read(compressed_size), decompressed_size)
                while i < len(self._key_list):
                    record_start, key_text = self._key_list[i]
                    if record_start - offset >= len(record_block): break
                    record_end = self._key_list[i+1][0] if i < len(self._key_list)-1 else len(record_block) + offset
                    i += 1
                    yield key_text, self._treat_record_data(record_block[record_start-offset:record_end-offset])
                offset += len(record_block); size_counter += compressed_size

    def _read_records_v1v2(self):
        with open(self._fname, 'rb') as f:
            f.seek(self._record_block_offset)
            num_record_blocks = self._read_number(f)
            num_entries = self._read_number(f)
            assert(num_entries == self._num_entries)
            record_block_info_size = self._read_number(f)
            record_block_size = self._read_number(f)
            record_block_info_list = []
            size_counter = 0
            for i in range(num_record_blocks):
                compressed_size = self._read_number(f)
                decompressed_size = self._read_number(f)
                record_block_info_list += [(compressed_size, decompressed_size)]
                size_counter += self._number_width * 2
            assert(size_counter == record_block_info_size)
            offset = 0; i = 0; size_counter = 0
            for compressed_size, decompressed_size in record_block_info_list:
                record_block = self._decode_block(f.read(compressed_size), decompressed_size)
                while i < len(self._key_list):
                    record_start, key_text = self._key_list[i]
                    if record_start - offset >= len(record_block): break
                    record_end = self._key_list[i+1][0] if i < len(self._key_list)-1 else len(record_block) + offset
                    i += 1
                    yield key_text, self._treat_record_data(record_block[record_start-offset:record_end-offset])
                offset += len(record_block); size_counter += compressed_size
            assert(size_counter == record_block_size)

    def _read_record_index(self):
        with open(self._fname, 'rb') as f:
            f.seek(self._record_index_offset)
            num_record_blocks = self._read_int32(f)
            num_bytes = self._read_number(f)
            record_index = []
            for i in range(num_record_blocks):
                decompressed_size = self._read_int32(f)
                compressed_size = self._read_int32(f)
                record_block = self._decode_block(f.read(compressed_size), decompressed_size)
                if len(record_block) % 16 != 0: raise Exception('record index block has invalid size %d' % len(record_block))
                j = 0
                while j < len(record_block):
                    block_size, decompressed_size = unpack('>QQ', record_block[j:j+16])
                    record_index.append((block_size, decompressed_size))
                    j += 16
        return record_index

    def _treat_record_data(self, data):
        return data

class MDD(MDict):
    def __init__(self, fname, passcode=None, build_index=True):
        MDict.__init__(self, fname, encoding='UTF-16', passcode=passcode, build_index=build_index)

class MDX(MDict):
    def __init__(self, fname, encoding='', substyle=False, passcode=None, build_index=True):
        MDict.__init__(self, fname, encoding, passcode, build_index)
        self._substyle = substyle

    def _substitute_stylesheet(self, txt):
        txt_list = re.split(rb'`\d+`', txt)
        txt_tag = re.findall(rb'`\d+`', txt)
        parts = [txt_list[0]]
        for j, p in enumerate(txt_list[1:]):
            style = self._stylesheet[txt_tag[j][1:-1]]
            if p and p[-1] == '\n':
                parts.append(style[0])
                parts.append(p.rstrip())
                parts.append(style[1])
                parts.append(b'\r\n')
            else:
                parts.append(style[0])
                parts.append(p)
                parts.append(style[1])
        return b''.join(parts)

    def _treat_record_data(self, data):
        data = data.decode(self._encoding, errors='ignore').strip(u'\x00').encode('utf-8')
        if self._substyle and self._stylesheet: data = self._substitute_stylesheet(data)
        return data


# ========== 高性能缓存版 MDX 类 ==========
CACHE_VERSION = 3  # 递增使旧缓存自动失效

class CachedMDX:
    MAX_RECORD_CACHE = 5
    MAX_KEY_CACHE = 10

    def __init__(self, fname, encoding='utf-8'):
        self.fname = fname
        self.base_mdx = MDX(fname, build_index=False)
        self.encoding = encoding

        self._key_blocks_meta = []
        self._record_blocks_meta = []
        self._record_block_offset = 0

        # 前缀和：_key_count_prefix[i] = 前 i 个 key block 的词条数总和
        # _rec_decomp_prefix[i] = 前 i 个 record block 的解压大小总和
        self._key_count_prefix = [0]
        self._rec_decomp_prefix = [0]

        self._record_cache = OrderedDict()
        self._key_cache = OrderedDict()
        self._file_lock = threading.RLock()
        self._load_or_build_index()

    def _get_cache_path(self):
        return self.fname + ".meta.cache.json"

    def _load_or_build_index(self):
        cpath = self._get_cache_path()
        loaded_from_cache = False
        if os.path.exists(cpath):
            try:
                with open(cpath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if data.get("ver") == CACHE_VERSION and data.get("fsize") == os.path.getsize(self.fname):
                    self._key_blocks_meta = data["key_meta"]
                    self._record_blocks_meta = data["rec_meta"]
                    loaded_from_cache = True
            except Exception:
                pass
        if not loaded_from_cache:
            self._build_index()
            try:
                with open(cpath, 'w', encoding='utf-8') as f:
                    json.dump({
                        "ver": CACHE_VERSION,
                        "fsize": os.path.getsize(self.fname),
                        "key_meta": self._key_blocks_meta,
                        "rec_meta": self._record_blocks_meta
                    }, f, ensure_ascii=False)
            except Exception:
                pass
        # 无论缓存命中与否，都构建前缀和（缓存中不保存前缀和，运行时构建）
        self._build_prefix_sums()

    def _build_prefix_sums(self):
        """根据 _key_blocks_meta 和 _record_blocks_meta 构建前缀和数组"""
        self._key_count_prefix = [0]
        for meta in self._key_blocks_meta:
            self._key_count_prefix.append(self._key_count_prefix[-1] + meta["count"])
        self._rec_decomp_prefix = [0]
        for meta in self._record_blocks_meta:
            self._rec_decomp_prefix.append(self._rec_decomp_prefix[-1] + meta["decomp"])

    def _build_v3_index(self, m):
        with open(self.fname, 'rb') as f:
            f.seek(m._key_block_offset)
            key_data_offset = None
            record_block_offset = None
            while True:
                block_type = m._read_int32(f)
                block_size = m._read_number(f)
                block_offset = f.tell()
                if block_type == 0x01000000:
                    record_block_offset = block_offset
                elif block_type == 0x03000000:
                    key_data_offset = block_offset
                f.seek(block_size, 1)
                if f.read(4):
                    f.seek(-4, 1)
                else:
                    break

        if key_data_offset is not None:
            with open(self.fname, 'rb') as f:
                f.seek(key_data_offset)
                num_kb = m._read_int32(f)
                _total_size = m._read_number(f)
                total_count = 0
                for i in range(num_kb):
                    decomp_size = m._read_int32(f)
                    comp_size = m._read_int32(f)
                    data_start = f.tell()
                    kb_compressed = f.read(comp_size)
                    kb_data = m._decode_block(kb_compressed, decomp_size)
                    keys_in_block = m._split_key_block(kb_data)
                    count = len(keys_in_block)
                    total_count += count
                    if keys_in_block:
                        self._key_blocks_meta.append({
                            "first": keys_in_block[0][1].decode('utf-8', errors='ignore'),
                            "last": keys_in_block[-1][1].decode('utf-8', errors='ignore'),
                            "count": count,
                            "offset": data_start,
                            "comp": comp_size,
                            "decomp": decomp_size
                        })
                m._num_entries = total_count

        if record_block_offset is not None:
            with open(self.fname, 'rb') as f:
                f.seek(record_block_offset)
                num_rb = m._read_int32(f)
                _total_size = m._read_number(f)
                for i in range(num_rb):
                    decomp_size = m._read_int32(f)
                    comp_size = m._read_int32(f)
                    data_start = f.tell()
                    self._record_blocks_meta.append({
                        "offset": data_start,
                        "comp": comp_size,
                        "decomp": decomp_size
                    })
            f.seek(comp_size, 1)

    def _build_index(self):
        with self._file_lock:
            m = self.base_mdx
            if m._version >= 3:
                self._build_v3_index(m)
                return

            with open(self.fname, 'rb') as f:
                f.seek(m._key_block_offset)
                num_bytes = 8 * 5 if m._version >= 2.0 else 4 * 4
                block = f.read(num_bytes)
                if m._encrypt & 1:
                    block = _salsa_decrypt(block, m._encrypted_key)
                sf = BytesIO(block)
                m._read_number(sf)
                m._num_entries = m._read_number(sf)

                if m._version >= 2.0:
                    m._read_number(sf)
                    kb_info_size = m._read_number(sf)
                    kb_size = m._read_number(sf)
                    f.read(4)
                else:
                    kb_info_size = m._read_number(sf)
                    kb_size = m._read_number(sf)

                kb_info = f.read(kb_info_size)
                kb_info_list = m._decode_key_block_info(kb_info)
                kb_data_start = f.tell()
                kb_compressed = f.read(kb_size)
                self._record_block_offset = f.tell()

                num_rb = m._read_number(f)
                m._read_number(f)
                m._read_number(f)
                m._read_number(f)

                rec_info_list = []
                for _ in range(num_rb):
                    c = m._read_number(f)
                    d = m._read_number(f)
                    rec_info_list.append((c, d))

                rec_data_start = f.tell()
                curr_rec_offset = rec_data_start
                for c, d in rec_info_list:
                    self._record_blocks_meta.append({"offset": curr_rec_offset, "comp": c, "decomp": d})
                    curr_rec_offset += c

            local_offset = 0
            for comp_size, decomp_size in kb_info_list:
                if comp_size == 0:
                    continue
                kb_data = m._decode_block(
                    kb_compressed[local_offset: local_offset + comp_size],
                    decomp_size
                )
                keys_in_block = m._split_key_block(kb_data)
                if keys_in_block:
                    # 【修复】_split_key_block 输出的是 utf-8 bytes，必须用 utf-8 解码
                    self._key_blocks_meta.append({
                        "first": keys_in_block[0][1].decode('utf-8', errors='ignore'),
                        "last": keys_in_block[-1][1].decode('utf-8', errors='ignore'),
                        "count": len(keys_in_block),
                        "offset": kb_data_start + local_offset,
                        "comp": comp_size,
                        "decomp": decomp_size
                    })
                local_offset += comp_size

    def _get_key_block(self, meta_idx):
        if meta_idx in self._key_cache:
            self._key_cache.move_to_end(meta_idx)
            return self._key_cache[meta_idx]
        with self._file_lock:
            meta = self._key_blocks_meta[meta_idx]
            with open(self.fname, 'rb') as f:
                f.seek(meta["offset"])
                kb_data = self.base_mdx._decode_block(f.read(meta["comp"]), meta["decomp"])
            keys = self.base_mdx._split_key_block(kb_data)
            self._key_cache[meta_idx] = keys
            if len(self._key_cache) > self.MAX_KEY_CACHE:
                self._key_cache.popitem(last=False)
            return keys

    def _get_record_block(self, rec_idx):
        if rec_idx in self._record_cache:
            self._record_cache.move_to_end(rec_idx)
            return self._record_cache[rec_idx]
        with self._file_lock:
            meta = self._record_blocks_meta[rec_idx]
            with open(self.fname, 'rb') as f:
                f.seek(meta["offset"])
                block_data = self.base_mdx._decode_block(f.read(meta["comp"]), meta["decomp"])
            self._record_cache[rec_idx] = block_data
            if len(self._record_cache) > self.MAX_RECORD_CACHE:
                self._record_cache.popitem(last=False)
            return block_data

    def search_prefix(self, prefix, max_results=100):
        results = []
        prefix_lower = prefix.lower()
        for idx, meta in enumerate(self._key_blocks_meta):
            if meta["last"].lower() < prefix_lower:
                continue
            if meta["first"].lower() > prefix_lower and not meta["first"].lower().startswith(prefix_lower):
                break
            keys_block = self._get_key_block(idx)
            base_abs_idx = self._key_count_prefix[idx]  # O(1) 替代 O(n) 的 sum
            for local_idx, (rec_offset, key_bytes) in enumerate(keys_block):
                # 【修复】_split_key_block 输出的是 utf-8 bytes，必须用 utf-8 解码
                key_str = key_bytes.decode('utf-8', errors='ignore')
                if key_str.lower().startswith(prefix_lower):
                    results.append((key_str, base_abs_idx + local_idx))
                    if len(results) >= max_results:
                        return results
                elif key_str.lower() > prefix_lower and not key_str.lower().startswith(prefix_lower):
                    break
        return results


    def get_by_index(self, abs_idx):
        with self._file_lock:
            # 用 bisect 在前缀和中定位 key block（O(log n) 替代 O(n) 线性扫描）
            i = bisect_right(self._key_count_prefix, abs_idx) - 1
            if i < 0 or i >= len(self._key_blocks_meta):
                return None
            meta = self._key_blocks_meta[i]
            keys_block = self._get_key_block(i)
            local_idx = abs_idx - self._key_count_prefix[i]
            if local_idx < 0 or local_idx >= len(keys_block):
                return None
            record_start_offset = keys_block[local_idx][0]

            # 用 bisect 在前缀和中定位 record block
            target_rb_idx = bisect_right(self._rec_decomp_prefix, record_start_offset) - 1
            if target_rb_idx < 0 or target_rb_idx >= len(self._record_blocks_meta):
                return None

            rec_block = self._get_record_block(target_rb_idx)
            rb_start_offset = self._rec_decomp_prefix[target_rb_idx]  # O(1) 替代 sum
            rb_end_offset = rb_start_offset + len(rec_block)

            # 【修复】：精准计算 end_offset，解决"加载下一区块内容"的问题
            if local_idx + 1 < len(keys_block):
                # 下一个词条在同一个 key_block 中
                end_offset = keys_block[local_idx + 1][0]
            else:
                # 当前词条是 key_block 的最后一条，需要去下一个 key_block 找下一个词条的起始偏移
                end_offset = rb_end_offset
                for next_i in range(i + 1, len(self._key_blocks_meta)):
                    next_keys_block = self._get_key_block(next_i)
                    if next_keys_block:
                        next_start = next_keys_block[0][0]
                        # 如果下一个词条的起始偏移还在当前 record_block 内，则用它作为结束边界
                        if next_start < rb_end_offset:
                            end_offset = next_start
                        break

            # 安全保底：绝不能超过 record_block 的物理边界
            if end_offset > rb_end_offset:
                end_offset = rb_end_offset

            data = rec_block[record_start_offset - rb_start_offset: end_offset - rb_start_offset]
            return self.base_mdx._treat_record_data(data)

    def close(self):
        self._record_cache.clear()
        self._key_cache.clear()


# ========== 高性能缓存版 MDD 类 ==========
class CachedMDD:
    MAX_CACHE = 10

    def __init__(self, fname, encoding='utf-8'):
        self.fname = fname
        self.base_mdd = MDD(fname, build_index=False)
        self.encoding = encoding
        self._record_blocks_meta = []
        self._record_cache = OrderedDict()
        self._file_lock = threading.RLock()

        self._normalized_map = {}
        self._offsets = []
        self._rec_decomp_prefix = [0]  # 前缀和
        self._build_path_index()

    def _normalize_path(self, path):
        return path.lower().replace('\\', '/').lstrip('/')

    def _build_path_index(self):
        m = self.base_mdd
        with open(self.fname, 'rb') as f:
            f.seek(m._key_block_offset)
            num_bytes = 8 * 5 if m._version >= 2.0 else 4 * 4
            block = f.read(num_bytes)
            if m._encrypt & 1:
                block = _salsa_decrypt(block, m._encrypted_key)
            sf = BytesIO(block)
            m._read_number(sf)
            m._read_number(sf)
            if m._version >= 2.0:
                m._read_number(sf)
                kb_info_size = m._read_number(sf)
                kb_size = m._read_number(sf)
                f.read(4)
            else:
                kb_info_size = m._read_number(sf)
                kb_size = m._read_number(sf)

            kb_info = f.read(kb_info_size)
            kb_info_list = m._decode_key_block_info(kb_info)

            kb_data_start = f.tell()
            kb_compressed = f.read(kb_size)

            num_rb = m._read_number(f)
            m._read_number(f)
            m._read_number(f)
            m._read_number(f)

            rec_info_list = []
            for _ in range(num_rb):
                c = m._read_number(f)
                d = m._read_number(f)
                rec_info_list.append((c, d))
            rec_data_start = f.tell()

            curr_rec_offset = rec_data_start
            for c, d in rec_info_list:
                self._record_blocks_meta.append({"offset": curr_rec_offset, "comp": c, "decomp": d})
                curr_rec_offset += c

        local_offset = 0
        for comp, decomp in kb_info_list:
            if comp == 0:
                continue
            kb_data = m._decode_block(
                kb_compressed[local_offset: local_offset + comp],
                decomp
            )
            keys = m._split_key_block(kb_data)
            for rec_offset, key_bytes in keys:
                # 【修复】_split_key_block 输出的是 utf-8 bytes，必须用 utf-8 解码
                raw_key = key_bytes.decode('utf-8', errors='ignore')
                self._normalized_map[self._normalize_path(raw_key)] = rec_offset
                self._offsets.append(rec_offset)
            local_offset += comp
        self._offsets.sort()
        # 构建前缀和
        self._rec_decomp_prefix = [0]
        for meta in self._record_blocks_meta:
            self._rec_decomp_prefix.append(self._rec_decomp_prefix[-1] + meta["decomp"])

    def get(self, path):
        record_start_offset = self._normalized_map.get(self._normalize_path(path))
        if record_start_offset is None:
            return None

        with self._file_lock:
            target_rb_idx = bisect_right(self._rec_decomp_prefix, record_start_offset) - 1
            if target_rb_idx < 0 or target_rb_idx >= len(self._record_blocks_meta):
                return None

            if target_rb_idx not in self._record_cache:
                meta = self._record_blocks_meta[target_rb_idx]
                with open(self.fname, 'rb') as f:
                    f.seek(meta["offset"])
                    self._record_cache[target_rb_idx] = self.base_mdd._decode_block(
                        f.read(meta["comp"]), meta["decomp"]
                    )
                if len(self._record_cache) > self.MAX_CACHE:
                    self._record_cache.popitem(last=False)
            else:
                self._record_cache.move_to_end(target_rb_idx)

            rec_block = self._record_cache[target_rb_idx]
            rb_start_offset = self._rec_decomp_prefix[target_rb_idx]  # O(1) 替代 sum

            idx_in_offsets = bisect_left(self._offsets, record_start_offset)
            record_end = (self._offsets[idx_in_offsets + 1]
                          if idx_in_offsets < len(self._offsets) - 1
                          else rb_start_offset + len(rec_block))

            return rec_block[record_start_offset - rb_start_offset: record_end - rb_start_offset]

    def close(self):
        self._record_cache.clear()
