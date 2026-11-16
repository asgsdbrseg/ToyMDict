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
import warnings
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


# =============================================================================
# MDX 格式兼容层
# -----------------------------------------------------------------------------
# 对照 raymanzhang/mdx（Rust libmdx）的 storage_block.rs / compression.rs /
# encryption.rs / key_block_index_unit.rs / meta_unit.rs，把 V1/V2/V3 在
# 「块头位布局、压缩算法、加密与 CRC 归属、索引头 CRC、缺省编码」上的分歧
# 集中在这一处处理。解析流程只调用本层函数，不再散落硬编码魔数与版本判断。
#
#   #1 key_block_info 块头按位解析（00/01/02/12… 不再硬匹配 02 00 00 00）
#   #2 数据块 adler32 按「是否加密」判定归属，而非按引擎版本
#   #3 压缩方法 0..5：None / LZO / Deflate / LZMA / Bzip2 / LZ4
#   #4 缺 GeneratedByEngineVersion 时回退 RequiredEngineVersion / 结构探测
#   #5 40 字节索引头后的 4 字节 CRC 校验
#   #6 缺省 Encoding：V1/V2 → UTF-16LE，V3 → UTF-8
# =============================================================================

# 压缩方法编号与 Rust CompressionMethod 一致
_COMPAT_COMP_NAMES = {0: 'none', 1: 'LZO', 2: 'deflate/zlib',
                      3: 'LZMA', 4: 'Bzip2', 5: 'LZ4'}


def _compat_head(head4):
    """【#1】按位拆解 4 字节块头。

    Rust storage_block::decode_block 把它看成：
    byte0 低 4 位 = 压缩方法、高 4 位 = 加密方法，byte1 = 加密长度，
    byte2-3 = 保留。旧实现只认 ``02 00 00 00`` / ``00 00 00 00`` 两个魔数，
    于是 LZO(01)、带加密标记(12) 的词典直接报「魔数不匹配」。
    """
    if len(head4) < 4:
        raise ValueError("MDX 块头长度不足 4 字节")
    value = unpack('<L', head4[:4])[0]
    return {
        'compression': value & 0x0F,
        'encryption': (value >> 4) & 0x0F,
        'encrypted_size': (value >> 8) & 0xFF,
        'raw': value,
    }


def _compat_decompress(method, data, expected_size=None):
    """【#3】解压一个块，支持 0..5 六种方法（Rust CompressionMethod）。"""
    if method == 0:
        out = data
    elif method == 1:
        if lzo is None:
            raise RuntimeError("LZO 压缩块需要 lzo 模块（pip install python-lzo）")
        if not expected_size:
            raise ValueError("LZO 压缩块缺少解压后大小")
        out = lzo.decompress(b'\xf0' + pack('>I', expected_size) + data)
    elif method == 2:
        out = zlib.decompress(data)
    elif method == 3:
        import lzma
        try:
            out = lzma.decompress(data, format=lzma.FORMAT_ALONE)
        except lzma.LZMAError:
            out = lzma.decompress(data)
    elif method == 4:
        import bz2
        out = bz2.decompress(data)
    elif method == 5:
        try:
            import lz4.frame
        except ImportError as exc:
            raise RuntimeError("LZ4 压缩块需要 lz4 模块（pip install lz4）") from exc
        out = lz4.frame.decompress(data)
    else:
        raise ValueError("MDX 未知压缩方法 %d（支持 0..5）" % method)
    if expected_size and len(out) != expected_size:
        raise ValueError("MDX 块解压大小不匹配: 期望 %d, 实际 %d"
                         % (expected_size, len(out)))
    return out


def _compat_crc_ok(expected, *candidates):
    """adler32 是否命中任一候选数据。"""
    return any((zlib.adler32(c) & 0xffffffff) == expected for c in candidates)


def _compat_verify_block_crc(expected, decrypted_block, decompressed_block, encrypted):
    """【#2】校验数据块 adler32。

    Rust：``crc_is_for_compressed_data = encryption_method != None``，
    即加密块校验「解密后但未解压」的数据，未加密块校验「解压后」的数据。
    旧实现按 ``_version >= 3`` 分派，V2 加密块 / V3 非加密块会误判。

    这里按加密标记选主对象，同时允许另一种写法通过（不同打包器实现有差异），
    两者都不匹配才判定为数据损坏。
    """
    primary = decrypted_block if encrypted else decompressed_block
    secondary = decompressed_block if encrypted else decrypted_block
    if not _compat_crc_ok(expected, primary, secondary):
        raise ValueError("MDX 数据块校验失败 (adler32): 期望 %#x, "
                         "压缩数据 %#x, 解压数据 %#x"
                         % (expected, zlib.adler32(decrypted_block) & 0xffffffff,
                            zlib.adler32(decompressed_block) & 0xffffffff))


def _compat_verify_index_crc(idx_para, crc_bytes):
    """【#5】校验 40 字节索引头之后的 4 字节 adler32。

    Rust key_block_index_unit::read_idx_para_v1_v2 会严格校验这段 CRC；
    旧实现用 ``f.read(4)`` 直接丢弃，索引头损坏时无法发现。
    个别打包器把 CRC 算在加密前的字节上，因此这里只告警不中断加载，
    避免把「兼容性修复」变成新的加载失败。
    """
    if not crc_bytes or len(crc_bytes) < 4:
        warnings.warn("MDX 索引头缺少 CRC 字段", RuntimeWarning)
        return False
    # 规范为大端，但个别打包器按小端写入；两种都接受，避免误报
    expected_be = unpack('>I', crc_bytes[:4])[0]
    expected_le = unpack('<I', crc_bytes[:4])[0]
    actual = zlib.adler32(idx_para) & 0xffffffff
    if actual in (expected_be, expected_le):
        return True
    warnings.warn("MDX 索引头 CRC 校验失败: 声明 %#x, 实际 %#x"
                  % (expected_be, actual), RuntimeWarning)
    return False


def _compat_decode_key_block_info(raw, data_encrypted, expected_size=None):
    """【#1/#3】解码 V2/V3 的 key_block_info，返回解压后的索引字节。"""
    if len(raw) < 8:
        raise ValueError("MDX key block info 长度不足 8 字节")
    head = _compat_head(raw[:4])
    crc = unpack('>I', raw[4:8])[0]

    body = bytearray(raw)
    # 数据区加密（Encrypted=2 / RegisterBy）：官方 MdxBuilder 解密范围是 [8:]
    if data_encrypted:
        key = ripemd128(raw[4:8] + pack('<L', 0x3695))
        body[8:] = _fast_decrypt(bytes(body[8:]), key)
    # 块头自身声明的块级加密：只解前 encrypted_size 字节
    if head['encryption'] and head['encrypted_size']:
        n = min(head['encrypted_size'], len(body) - 8)
        if n > 0:
            key = ripemd128(raw[4:8])
            body[8:8 + n] = _fast_decrypt(bytes(body[8:8 + n]), key)

    payload = bytes(body[8:])
    info = _compat_decompress(head['compression'], payload, expected_size)
    _compat_verify_block_crc(crc, payload, info,
                             bool(head['encryption'] or data_encrypted))
    return info


# 版本字段优先级：部分打包器只写其中一个，个别老词典两个都没有
_COMPAT_VERSION_KEYS = (b'GeneratedByEngineVersion',
                        b'RequiredEngineVersion',
                        b'EngineVersion',
                        b'Version')


def _compat_parse_version(value):
    """【#4】把 header 里的版本值解析成 float。

    容忍 ``2.0.1`` / ``v3`` / ``3`` / 空值等写法；只接受 1.x/2.x/3.x，
    明显脏数据返回 None 交给结构探测，避免 float() 抛 ValueError / KeyError。
    """
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode('ascii', errors='ignore')
    match = re.search(r'\d+(?:\.\d+)?', str(value).strip())
    if not match:
        return None
    try:
        version = float(match.group(0))
    except ValueError:
        return None
    return version if 1.0 <= version < 4.0 else None


def _compat_probe_version(fname, offset, encrypted_index=False):
    """【#4】header 未给出可用版本号时，按索引头结构推断 V1/V2/V3。

    - V3：``offset`` 起是 section 头（4 字节 type + 8 字节 size）；
    - V2：40 字节 5×u64 索引头 + 4 字节 adler32（复用 #5 的判据）；
    - V1：16 字节 4×u32 索引头，无 CRC。

    三种布局的前 8 字节特征互斥：V2 的高 32 位恒为 0，V1 的前 8 字节
    按 u64 读必然 >= 2**32，V3 的 section size 高 32 位恒为 0。
    猜不出一律返回 None，由调用方回退默认值，不把兼容修复变成加载失败。
    """
    if encrypted_index:
        # Encrypted=1 的索引头是 salsa 密文，_read_header 阶段密钥尚未算出
        return None
    try:
        file_size = os.path.getsize(fname)
        with open(fname, 'rb') as f:
            f.seek(offset)
            block = f.read(44)
    except OSError:
        return None
    if len(block) < 16:
        return None
    remain = max(0, file_size - offset)

    # --- V3 section 标记 ---
    first_be = unpack('>I', block[:4])[0]
    first_le = unpack('<I', block[:4])[0]
    section_like = (first_be in (1, 2, 3, 4, 0x01000000, 0x02000000,
                                 0x03000000, 0x04000000)
                    or first_le in (1, 2, 3, 4))
    # section size 的高 32 位为 0，借此与 V1 的 num_entries 区分开
    if section_like and block[4:8] == b'\x00\x00\x00\x00':
        size_be = unpack('>I', block[8:12])[0] if len(block) >= 12 else 0
        size_le = unpack('<I', block[8:12])[0] if len(block) >= 12 else 0
        if 0 < size_be < remain or 0 < size_le < remain:
            return 3.0

    # --- V2：40 字节索引头 + CRC ---
    if len(block) >= 44:
        crc_be = unpack('>I', block[40:44])[0]
        crc_le = unpack('<I', block[40:44])[0]
        actual = zlib.adler32(block[:40]) & 0xffffffff
        if actual in (crc_be, crc_le):
            return 2.0
        # CRC 写错或缺失时用 5×u64 结构特征兜底：V1 的 q[0] 必然 >= 2**32
        q = unpack('>5Q', block[:40])
        if all(x < 0x100000000 for x in q) and q[0] > 0 and q[1] >= q[0] \
                and q[2] > 0 and q[3] > 0 and q[4] > 0 \
                and q[3] + q[4] + 44 <= remain:
            return 2.0

    # --- V1：16 字节 4×u32 ---
    v1 = unpack('>4I', block[:16])
    if v1[0] > 0 and v1[1] >= v1[0] and v1[2] > 0 and v1[3] > 0 \
            and v1[2] + v1[3] + 16 <= remain:
        return 1.2
    return None


def _compat_engine_version(header_tag, fname, key_block_offset,
                           encrypted_index=False):
    """【#4】解析引擎版本，缺 GeneratedByEngineVersion 不再 KeyError。

    顺序：GeneratedByEngineVersion → RequiredEngineVersion（对齐 Rust
    meta_unit 的取值口径）→ 按索引头结构探测 → 默认 2.0。
    返回 ``(version, source)``，source 仅供诊断。
    """
    for key in _COMPAT_VERSION_KEYS:
        version = _compat_parse_version(header_tag.get(key))
        if version is not None:
            return version, key.decode('ascii')
    probed = _compat_probe_version(fname, key_block_offset, encrypted_index)
    if probed is not None:
        return probed, '索引头结构探测'
    warnings.warn(
        "MDX header 缺少 GeneratedByEngineVersion/RequiredEngineVersion，"
        "且索引头结构无法判定，按 2.0 处理", RuntimeWarning)
    return 2.0, '默认值'


def _compat_default_encoding(version):
    """【#6】Rust meta_unit：缺 Encoding 时 V3 = utf-8，V1/V2 = utf-16le。

    旧实现一律按 utf-8 处理，未声明编码的 V1/V2 词典会按单字节切 key，
    导致词条名乱码、索引错位。
    """
    return 'UTF-8' if version >= 3 else 'UTF-16LE'


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
        head = _compat_head(block[:4])
        compression_method = head['compression']
        encryption_method = head['encryption']
        encryption_size = head['encrypted_size']
        adler32 = unpack('>I', block[4:8])[0]
        encrypted_key = self._encrypted_key if self._encrypted_key is not None else ripemd128(block[4:8])
        data = block[8:]

        if encryption_method == 0: decrypted_block = data
        elif encryption_method == 1: decrypted_block = _fast_decrypt(data[:encryption_size], encrypted_key) + data[encryption_size:]
        elif encryption_method == 2: decrypted_block = _salsa_decrypt(data[:encryption_size], encrypted_key) + data[encryption_size:]
        else: raise Exception('encryption method %d not supported' % encryption_method)

        # 【#3】压缩方法 0..5 统一交给兼容层；【#2】CRC 归属按本块是否加密判定
        decompressed_block = _compat_decompress(compression_method, decrypted_block, decompressed_size)
        _compat_verify_block_crc(adler32, decrypted_block, decompressed_block,
                                 encryption_method != 0)
        return decompressed_block

    def _decode_key_block_info(self, key_block_info_compressed, with_text=False,
                               expected_size=None):
        """解析 key_block_info。
        - 默认(with_text=False)：返回 [(comp_size, decomp_size), ...]，供 MDict 自身建完整索引；
        - with_text=True：额外捕获每块的首词/尾词文本字节，返回
          [(num_entries, head_text, tail_text, comp_size, decomp_size), ...]，
          供 CachedMDD 只建"块级索引 + 首/尾词"，无需解压任何 key block；
        - expected_size：索引头里声明的解压后大小（V1 无此字段，传 None）。
        """
        if self._version >= 2:
            # 【#1/#2/#3/#5】块头位布局、加密、压缩方法 0..5、CRC 归属
            # 全部由兼容层统一处理，不再硬匹配 02 00 00 00 魔数。
            key_block_info = _compat_decode_key_block_info(
                key_block_info_compressed,
                bool(self._encrypt & 0x02),
                expected_size,
            )
        else:
            key_block_info = key_block_info_compressed

        key_block_info_list = []
        num_entries = 0
        i = 0
        if self._version >= 2: byte_format, byte_width, text_term = '>H', 2, 1
        else: byte_format, byte_width, text_term = '>B', 1, 0

        while i < len(key_block_info):
            block_num = unpack(self._number_format, key_block_info[i:i+self._number_width])[0]
            i += self._number_width
            num_entries += block_num
            # ![0] head word text（每块首词）
            text_head_size = unpack(byte_format, key_block_info[i:i+byte_width])[0]
            i += byte_width
            head_w = (text_head_size + text_term) * (2 if self._is_utf16 else 1)
            head_text = key_block_info[i:i+head_w] if with_text else None
            i += head_w
            # ![1] tail word text（每块尾词）
            text_tail_size = unpack(byte_format, key_block_info[i:i+byte_width])[0]
            i += byte_width
            tail_w = (text_tail_size + text_term) * (2 if self._is_utf16 else 1)
            tail_text = key_block_info[i:i+tail_w] if with_text else None
            i += tail_w
            key_block_compressed_size = unpack(self._number_format, key_block_info[i:i+self._number_width])[0]
            i += self._number_width
            key_block_decompressed_size = unpack(self._number_format, key_block_info[i:i+self._number_width])[0]
            i += self._number_width
            if with_text:
                key_block_info_list.append((block_num, head_text, tail_text,
                                           key_block_compressed_size, key_block_decompressed_size))
            else:
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
            delimiter = b'\x00\x00' if self._is_utf16 else b'\x00'
            width = 2 if self._is_utf16 else 1
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
            actual = zlib.adler32(header_bytes) & 0xffffffff
            if adler32 != actual:
                raise ValueError(f"MDX 头部校验失败 (adler32): 期望 {adler32:#x}, 实际 {actual:#x}")
            self._key_block_offset = f.tell()

        if header_bytes[-2:] == b'\x00\x00': header_text = header_bytes[:-2].decode('utf-16').encode('utf-8')
        else: header_text = header_bytes[:-1]
        header_tag = self._parse_header(header_text)

        # 加密标记需先于版本解析：Encrypted=1 的索引头是密文，无法做结构探测
        if b'Encrypted' not in header_tag or header_tag[b'Encrypted'] == b'No': self._encrypt = 0
        elif header_tag[b'Encrypted'] == b'Yes': self._encrypt = 1
        else: self._encrypt = int(header_tag[b'Encrypted'])

        # 【#4】缺 GeneratedByEngineVersion 不再 KeyError；版本须先于编码解析（#6）
        self._version, self._version_source = _compat_engine_version(
            header_tag, self._fname, self._key_block_offset,
            encrypted_index=bool(self._encrypt & 1))
        if self._version < 2.0: self._number_width, self._number_format = 4, '>I'
        else: self._number_width, self._number_format = 8, '>Q'

        if not self._encoding:
            default_enc = _compat_default_encoding(self._version).encode('utf-8')
            encoding = header_tag.get(b'Encoding', default_enc) or default_enc
            if sys.hexversion >= 0x03000000: encoding = encoding.decode('utf-8')
            if encoding in ['GBK', 'GB2312']: encoding = 'GB18030'
            self._encoding = encoding
        if self._version >= 3: self._encoding = 'UTF-8'

        self._stylesheet = {}
        if header_tag.get(b'StyleSheet'):
            lines = header_tag[b'StyleSheet'].splitlines()
            for i in range(0, len(lines), 3): self._stylesheet[lines[i]] = (lines[i+1], lines[i+2])
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
                # 【#5】校验 40 字节索引头之后的 CRC，不再 f.read(4) 丢弃
                _compat_verify_index_crc(block, f.read(4))
            else:
                key_block_info_decomp_size = None
                key_block_info_size = self._read_number(sf)
                key_block_size = self._read_number(sf)

            key_block_info = f.read(key_block_info_size)
            key_block_info_list = self._decode_key_block_info(
                key_block_info, expected_size=key_block_info_decomp_size)
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
            if self._version >= 2.0:
                # 【#1】按位解析头，不再硬匹配 02 00 00 00
                if _compat_head(key_block_info[:4])['compression'] not in _COMPAT_COMP_NAMES:
                    raise ValueError("MDX 暴力解析: key block info 头非法")
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
            if num_entries != self._num_entries:
                raise ValueError(f"MDX 记录数不匹配: 期望 {self._num_entries}, 实际 {num_entries}")
            record_block_info_size = self._read_number(f)
            record_block_size = self._read_number(f)
            record_block_info_list = []
            size_counter = 0
            for i in range(num_record_blocks):
                compressed_size = self._read_number(f)
                decompressed_size = self._read_number(f)
                record_block_info_list += [(compressed_size, decompressed_size)]
                size_counter += self._number_width * 2
            if size_counter != record_block_info_size:
                raise ValueError(f"MDX record block info 大小不匹配: 期望 {record_block_info_size}, 实际 {size_counter}")
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
            if size_counter != record_block_size:
                raise ValueError(f"MDX record block 大小不匹配: 期望 {record_block_size}, 实际 {size_counter}")

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

    @property
    def _is_utf16(self):
        # MDX header 的 Encoding 可能写 UTF-16 / UTF-16LE / utf-16 等
        return bool(self._encoding) and 'UTF-16' in self._encoding.upper()

class MDD(MDict):
    def __init__(self, fname, passcode=None, build_index=True):
        MDict.__init__(self, fname, encoding='UTF-16', passcode=passcode, build_index=build_index)

class MDX(MDict):
    def __init__(self, fname, encoding='', substyle=False, passcode=None, build_index=True):
        MDict.__init__(self, fname, encoding, passcode, build_index)
        # 【#8】Compact/StyleSheet 还原：header 带 compact 样式表时自动生效，
        # 不再依赖调用方显式传 substyle=True。CachedMDX 过去以
        # MDX(fname, build_index=False) 构造时没传该参数，正文里的 `` `N` ``
        # 占位符会原样输出、词条样式全部丢失。
        self._stylesheet = self._normalize_stylesheet(getattr(self, '_stylesheet', {}))
        self._substyle = bool(substyle) or self._is_compact_stylesheet()

    @staticmethod
    def _normalize_stylesheet(stylesheet):
        """把 header 三行一组的 StyleSheet 规整为 ``{b'0': (prefix, suffix)}``。

        对照 Rust MdxReader::load_compact_stylesheet：token 是 0..255 的数字，
        prefix/suffix 需要还原 HTML 实体。token 不是数字时说明这是普通 CSS
        样式表（供前端使用），不应参与 compact 还原。
        """
        normalized = {}
        for token, pair in (stylesheet or {}).items():
            try:
                t = token.strip()
                if not t.isdigit() or int(t) > 255:
                    continue
                prefix, suffix = pair
                normalized[str(int(t)).encode('ascii')] = (
                    _unescape_entities(prefix), _unescape_entities(suffix))
            except (TypeError, ValueError, AttributeError):
                continue
        return normalized

    def _is_compact_stylesheet(self):
        """规范化后仍非空即说明是 token/prefix/suffix 形式的 compact 样式表。

        纯 CSS 样式表的 token 行不是数字，已在 _normalize_stylesheet 被剔除；
        旧版把 Compact 拼成 Compat 的词典同样按此特征识别。
        """
        return bool(self._stylesheet)

    def _substitute_stylesheet(self, txt):
        """还原 `` `N`内容` `` 占位符（对齐 Rust MdxReader::reformat）。

        相比旧版 re.split 实现：未知 token 原样保留而不是抛 KeyError，
        内容只取到下一个反引号，不会把跨段文本错误地一起包裹；
        同时修正了旧版拿 bytes 的 p[-1] 与 str 的 '\\n' 比较（恒为假）的问题。
        """
        out = []
        i, n = 0, len(txt)
        while i < n:
            if txt[i:i + 1] != b'`':
                out.append(txt[i:i + 1])
                i += 1
                continue
            j = i + 1
            k = j
            while k < n and txt[k:k + 1].isdigit():
                k += 1
            if k > j and k < n and txt[k:k + 1] == b'`':
                token = txt[j:k]
                style = self._stylesheet.get(str(int(token)).encode('ascii'))
                i = k + 1
                e = i
                while e < n and txt[e:e + 1] != b'`':
                    e += 1
                body = txt[i:e]
                # 下一个反引号后若不是数字 token，它是本段样式的闭合符，
                # 直接消费（compact 正文里反引号是控制字符，不应透出到页面）。
                closed = False
                if e < n and not txt[e + 1:e + 2].isdigit():
                    e += 1
                    closed = True
                i = e  # 否则留给下一轮，作为新 token 的起点
                if style is None:
                    out.append(b'`' + token + b'`')
                    out.append(body)
                    if closed: out.append(b'`')
                    continue
                if body.endswith(b'\n'):
                    out.append(style[0])
                    out.append(body.rstrip(b'\r\n'))
                    out.append(style[1])
                    out.append(b'\r\n')
                else:
                    out.append(style[0])
                    out.append(body)
                    out.append(style[1])
            else:
                out.append(b'`')
                i += 1
        return b''.join(out)

    def _treat_record_data(self, data):
        data = data.decode(self._encoding, errors='ignore').strip(u'\x00').encode('utf-8')
        if self._substyle and self._stylesheet: data = self._substitute_stylesheet(data)
        return data


# ========== 高性能缓存版 MDX 类 ==========
CACHE_VERSION = 3  # 递增使旧缓存自动失效

class CachedMDX:
    # 缓存上限与常驻内存直接相关：解压后的 record block 单块可达数 MB，
    # 已加载 N 本词典就是 N 份缓存。顺序访问下小缓存命中也很少失效。
    MAX_RECORD_CACHE = 2
    MAX_KEY_CACHE = 4

    def __init__(self, fname, encoding='utf-8', build_index=True):
        self.fname = fname
        self.base_mdx = MDX(fname, build_index=False)
        self.encoding = encoding

        self._key_blocks_meta = []
        self._record_blocks_meta = []
        self._record_block_offset = 0

        self._key_count_prefix = [0]
        self._rec_decomp_prefix = [0]

        self._record_cache = OrderedDict()
        self._key_cache = OrderedDict()
        self._file_lock = threading.RLock()
        # 持久文件句柄，避免每次读取都 open/close
        self._fh = open(self.fname, 'rb')
        if build_index:
            # 完整模式：读取并解压全部 key/record 块，构建可搜索索引
            self._load_or_build_index()
        else:
            # 仅读 header 模式（如查看词典信息）：跳过全量索引构建，
            # 此时 self.base_mdx.header 已可用，仅需构造空前缀和
            self._build_prefix_sums()

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
        """根据 _key_blocks_meta 和 _record_blocks_meta 构建前缀和数组，
        并构建 key block 二分定位所需的排序辅助数组。"""
        self._key_count_prefix = [0]
        for meta in self._key_blocks_meta:
            self._key_count_prefix.append(self._key_count_prefix[-1] + meta["count"])
        self._rec_decomp_prefix = [0]
        for meta in self._record_blocks_meta:
            self._rec_decomp_prefix.append(self._rec_decomp_prefix[-1] + meta["decomp"])
        # 用于 key block 二分定位：各块首/尾 key 的小写形式
        # first 用于精确查找时提前终止，last 用于定位前缀搜索的起始块
        self._key_first_lower = [m["first"].lower() for m in self._key_blocks_meta]
        self._key_last_lower = [m["last"].lower() for m in self._key_blocks_meta]
        # 【#7】bisect_left 要求 _key_last_lower 单调递增。中文拼音 / ICU
        # collation 词典按 sort key 排序，转成 str 后往往非单调，二分会静默漏词。
        self._key_sorted = all(
            self._key_last_lower[i] <= self._key_last_lower[i + 1]
            for i in range(len(self._key_last_lower) - 1)
        )

    def _key_block_order(self, start, keep_order=True):
        """【#7】返回待扫描的 key block 索引序列。

        块序可靠时沿用二分起点；不可靠时退化为全块扫描（对齐 CachedMDD.get()
        已有的兜底思路）。search_prefix 需要保持结果顺序（keep_order=True），
        lookup_key 只要命中即可（先扫二分起点之后，再补齐之前）。
        """
        n = len(self._key_blocks_meta)
        if getattr(self, '_key_sorted', True):
            return range(start, n)
        if keep_order:
            return range(0, n)
        return list(range(start, n)) + list(range(0, start))

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
                            "first": keys_in_block[0][1].decode(m._encoding, errors='ignore'),
                            "last": keys_in_block[-1][1].decode(m._encoding, errors='ignore'),
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
                    kb_info_decomp_size = m._read_number(sf)
                    kb_info_size = m._read_number(sf)
                    kb_size = m._read_number(sf)
                    # 【#5】校验索引头 CRC
                    _compat_verify_index_crc(block, f.read(4))
                else:
                    kb_info_decomp_size = None
                    kb_info_size = m._read_number(sf)
                    kb_size = m._read_number(sf)

                kb_info = f.read(kb_info_size)
                kb_info_list = m._decode_key_block_info(
                    kb_info, expected_size=kb_info_decomp_size)
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
            self._fh.seek(meta["offset"])
            kb_data = self.base_mdx._decode_block(self._fh.read(meta["comp"]), meta["decomp"])
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
            self._fh.seek(meta["offset"])
            block_data = self.base_mdx._decode_block(self._fh.read(meta["comp"]), meta["decomp"])
            self._record_cache[rec_idx] = block_data
            if len(self._record_cache) > self.MAX_RECORD_CACHE:
                self._record_cache.popitem(last=False)
            return block_data

    def iter_keys(self):
        """按绝对索引顺序遍历全部词条名，产出 ``(abs_idx, key_str)``。

        高级搜索的条目/全文扫描需要访问所有词条；这里按 key block 顺序
        逐个解压（缓存会自动复用最近 10 块），避免调用方依赖私有字段。
        """
        for i in range(len(self._key_blocks_meta)):
            keys_block = self._get_key_block(i)
            base_abs_idx = self._key_count_prefix[i]
            for local_idx, (_rec_offset, key_bytes) in enumerate(keys_block):
                yield base_abs_idx + local_idx, key_bytes.decode('utf-8', errors='ignore')

    def search_prefix(self, prefix):
        """前缀搜索（字节级匹配，不匹配的 key 跳过 decode）。

        先用二分定位首个可能命中的 key block（首块之前的块，其尾 key 均小于
        prefix，不可能包含匹配项），再线性扫描后续块，块首 key 越界即终止。

        Args:
            prefix: 搜索前缀
        """
        results = []
        prefix_lower = prefix.lower()
        prefix_bytes = prefix_lower.encode('utf-8')
        metas = self._key_blocks_meta
        if not metas:
            return results
        # 二分：第一个 last.lower() >= prefix_lower 的块（其前的块所有 key 均 < prefix）
        sorted_blocks = getattr(self, '_key_sorted', True)
        start = bisect_left(self._key_last_lower, prefix_lower)
        for idx in self._key_block_order(start, keep_order=True):
            meta = metas[idx]
            # 【#7】有序假设只在块序可靠时成立
            if sorted_blocks:
                # 块首 key 已大于前缀且不以前缀开头 → 后续块更大，不可能再匹配
                if meta["first"].lower() > prefix_lower and not meta["first"].lower().startswith(prefix_lower):
                    break
            keys_block = self._get_key_block(idx)
            base_abs_idx = self._key_count_prefix[idx]  # O(1) 替代 O(n) 的 sum
            stop_outer = False
            for local_idx, (rec_offset, key_bytes) in enumerate(keys_block):
                # 字节级匹配：bytes.lower() 只处理 ASCII 大小写，比 decode 快
                key_lower_bytes = key_bytes.lower()
                if key_lower_bytes.startswith(prefix_bytes):
                    # 只有命中才 decode
                    key_str = key_bytes.decode('utf-8', errors='ignore')
                    results.append((key_str, base_abs_idx + local_idx))
                elif sorted_blocks and key_lower_bytes > prefix_bytes:
                    # key 已排序，后续 key 都更大，不可能再匹配
                    stop_outer = True
                    break
            if stop_outer:
                break
        return results

    def lookup_key(self, key):
        """精确查找 key（大小写不敏感），返回绝对索引 idx 或 None。

        先用二分定位候选 key block（含可能匹配的块），再在块内做精确比较。
        相比 search_prefix 取首条再 != 判等，这里用二分 + 相等比较，
        更稳健高效，且大小写不敏感与搜索语义一致。

        Args:
            key: 待查找的词条文本
        """
        metas = self._key_blocks_meta
        if not metas:
            return None
        key_lower = key.lower()
        # 二分：第一个 last.lower() >= key_lower 的块（其前的块所有 key 均 < key）
        sorted_blocks = getattr(self, '_key_sorted', True)
        start = bisect_left(self._key_last_lower, key_lower)
        for idx in self._key_block_order(start, keep_order=False):
            meta = metas[idx]
            # 【#7】块首 key 大于目标即终止的前提同样是块序可靠
            if sorted_blocks and meta["first"].lower() > key_lower:
                break
            keys_block = self._get_key_block(idx)
            base_abs_idx = self._key_count_prefix[idx]
            for local_idx, (rec_offset, key_bytes) in enumerate(keys_block):
                key_str = key_bytes.decode('utf-8', errors='ignore')
                if key_str.lower() == key_lower:
                    return base_abs_idx + local_idx
        return None

    def get_by_index(self, abs_idx):
        with self._file_lock:
            # 用 bisect 在前缀和中定位 key block（O(log n) 替代 O(n) 线性扫描）
            i = bisect_right(self._key_count_prefix, abs_idx) - 1
            if i < 0 or i >= len(self._key_blocks_meta):
                return None
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

            # 计算结束偏移：取下一词条的起始偏移；若为最后一条则用 record_block 末尾
            if local_idx + 1 < len(keys_block):
                # 下一词条在同一 key_block 中
                end_offset = keys_block[local_idx + 1][0]
            elif i + 1 < len(self._key_blocks_meta):
                # 当前为 key_block 末条，取下一 key_block 的首条偏移
                next_block = self._get_key_block(i + 1)
                end_offset = next_block[0][0] if next_block else rb_end_offset
            else:
                # 已是最后一个 key_block 的最后一条
                end_offset = rb_end_offset

            # 安全保底：不超过当前 record_block 的物理边界
            if end_offset > rb_end_offset:
                end_offset = rb_end_offset

            data = rec_block[record_start_offset - rb_start_offset: end_offset - rb_start_offset]
            return self.base_mdx._treat_record_data(data)

    def close(self):
        self._record_cache.clear()
        self._key_cache.clear()
        try:
            self._fh.close()
        except Exception:
            pass


class CachedMDD:
    # MDD 的 record block 多为压缩资源（图片/CSS），解压后很大；图片查看等
    # 场景一次只会用一两块，缓存 2 块足够。
    MAX_CACHE = 2

    def __init__(self, fname, encoding='utf-8'):
        self.fname = fname
        self.base_mdd = MDD(fname, build_index=False)   # 只读 header，不碰资源路径
        self.encoding = encoding
        self._key_blocks_meta = []
        self._record_blocks_meta = []
        self._key_cache = OrderedDict()
        self._record_cache = OrderedDict()
        self._file_lock = threading.RLock()
        self._key_count_prefix = [0]
        self._rec_decomp_prefix = [0]
        self._fh = open(self.fname, 'rb')
        # 加载时只读 key_block_info 的首/尾词建立块级索引，不落盘、不解压任何 key block
        self._build_block_index()
        self._build_prefix_sums()

    def _normalize_path(self, path):
        return path.lower().replace('\\', '/').lstrip('/')

    def _build_prefix_sums(self):
        self._key_count_prefix = [0]
        for m in self._key_blocks_meta:
            self._key_count_prefix.append(self._key_count_prefix[-1] + m["count"])
        self._rec_decomp_prefix = [0]
        for m in self._record_blocks_meta:
            self._rec_decomp_prefix.append(self._rec_decomp_prefix[-1] + m["decomp"])
        # 【关键修复】块边界必须与查询词走同一套归一化（lower + \→/ + 去前导/）。
        # MDD 的原始 key 形如 "\template\a.css"，反斜杠(0x5C) < 小写字母(0x61)，
        # 若不做归一化，bisect_left 对 "template/a.css" 这类查询必然落空 → 资源全部 404。
        self._key_first_lower = [self._normalize_path(m["first"]) for m in self._key_blocks_meta]
        self._key_last_lower = [self._normalize_path(m["last"]) for m in self._key_blocks_meta]

    def _build_block_index(self):
        """只读 key_block_info 索引块的【首词/尾词】建立块级索引。
        不落盘、不读取/不解码任何 key block（与 mdict / PlainDictionaryAPP 对齐）：
        key_block_info 本身已含每块的首词与尾词文本，二分定位只需要它；
        命中后由 _get_key_block 按需解压【单块】即可。
        """
        m = self.base_mdd
        if m._version >= 3:
            # 【#9】MDD 3.0 走独立的 section 布局
            self._build_v3_block_index(m)
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
                kb_info_decomp_size = m._read_number(sf)
                kb_info_size = m._read_number(sf)
                kb_size = m._read_number(sf)
                # 【#5】校验索引头 CRC
                _compat_verify_index_crc(block, f.read(4))
            else:
                kb_info_decomp_size = None
                kb_info_size = m._read_number(sf)
                kb_size = m._read_number(sf)
            kb_info = f.read(kb_info_size)
            # 读取每块的首词/尾词文本（不再解压 key block 数据）
            kb_info_list = m._decode_key_block_info(
                kb_info, with_text=True, expected_size=kb_info_decomp_size)
            kb_data_start = f.tell()               # key block 区起点（用于命中后按需解压单块）
            f.seek(kb_data_start + kb_size)        # 跳过 key block 区，直接到 record block 区
            num_rb = m._read_number(f)
            m._read_number(f); m._read_number(f); m._read_number(f)
            rec_info_list = []
            for _ in range(num_rb):
                c = m._read_number(f); d = m._read_number(f)
                rec_info_list.append((c, d))
            rec_data_start = f.tell()
            curr = rec_data_start
            for c, d in rec_info_list:
                self._record_blocks_meta.append({"offset": curr, "comp": c, "decomp": d})
                curr += c
        local_offset = 0
        for block_num, head_text, tail_text, comp, decomp in kb_info_list:
            if comp == 0:
                continue
            first = head_text.decode(m._encoding, errors='ignore').strip('\x00') if head_text else ""
            last = tail_text.decode(m._encoding, errors='ignore').strip('\x00') if tail_text else ""
            self._key_blocks_meta.append({
                "first": first,
                "last": last,
                "count": block_num,
                "offset": kb_data_start + local_offset,
                "comp": comp,
                "decomp": decomp
            })
            local_offset += comp

    def _build_v3_block_index(self, m):
        """【#9】构建 MDD 3.0 的块级索引。

        V3 与 V1/V2 布局完全不同：header 之后是一串
        ``block_type(4B 大端) + block_size(8B >Q) + section 数据``，
        其中 0x01000000 是 record block 区、0x03000000 是 key data 区。
        旧实现只有 V1/V2 分支，遇到 MDD 3.0 会按 40 字节索引头去读，
        偏移全错 → 资源一律 404。这里与 CachedMDX._build_v3_index 对齐。
        """
        with open(self.fname, 'rb') as f:
            f.seek(m._key_block_offset)
            key_data_offset = None
            record_block_offset = None
            while True:
                block_type = m._read_int32(f)
                block_size = m._read_number(f)
                section_start = f.tell()
                if block_type == 0x01000000:
                    record_block_offset = section_start
                elif block_type == 0x03000000:
                    key_data_offset = section_start
                f.seek(section_start + block_size)
                if f.read(4):
                    f.seek(-4, 1)
                else:
                    break

            if key_data_offset is not None:
                f.seek(key_data_offset)
                num_kb = m._read_int32(f)
                m._read_number(f)  # key data 区总长度
                for _ in range(num_kb):
                    decomp_size = m._read_int32(f)
                    comp_size = m._read_int32(f)
                    data_start = f.tell()
                    kb_data = m._decode_block(f.read(comp_size), decomp_size)
                    keys = m._split_key_block(kb_data)
                    if keys:
                        # _split_key_block 输出的 key 已统一为 utf-8 bytes
                        self._key_blocks_meta.append({
                            "first": keys[0][1].decode('utf-8', errors='ignore'),
                            "last": keys[-1][1].decode('utf-8', errors='ignore'),
                            "count": len(keys),
                            "offset": data_start,
                            "comp": comp_size,
                            "decomp": decomp_size,
                        })
                    f.seek(data_start + comp_size)
                m._num_entries = sum(x["count"] for x in self._key_blocks_meta)

            if record_block_offset is not None:
                f.seek(record_block_offset)
                num_rb = m._read_int32(f)
                m._read_number(f)  # record 区总长度
                for _ in range(num_rb):
                    decomp_size = m._read_int32(f)
                    comp_size = m._read_int32(f)
                    data_start = f.tell()
                    self._record_blocks_meta.append({
                        "offset": data_start,
                        "comp": comp_size,
                        "decomp": decomp_size,
                    })
                    f.seek(data_start + comp_size)

    def _get_key_block(self, idx):
        if idx in self._key_cache:
            self._key_cache.move_to_end(idx)
            return self._key_cache[idx]
        with self._file_lock:
            meta = self._key_blocks_meta[idx]
            self._fh.seek(meta["offset"])
            kb_data = self.base_mdd._decode_block(self._fh.read(meta["comp"]), meta["decomp"])
            keys = self.base_mdd._split_key_block(kb_data)
            self._key_cache[idx] = keys
            if len(self._key_cache) > self.MAX_CACHE:
                self._key_cache.popitem(last=False)
            return keys

    def _get_record_block(self, rec_idx):
        if rec_idx in self._record_cache:
            self._record_cache.move_to_end(rec_idx)
            return self._record_cache[rec_idx]
        with self._file_lock:
            meta = self._record_blocks_meta[rec_idx]
            self._fh.seek(meta["offset"])
            block_data = self.base_mdd._decode_block(self._fh.read(meta["comp"]), meta["decomp"])
            self._record_cache[rec_idx] = block_data
            if len(self._record_cache) > self.MAX_CACHE:
                self._record_cache.popitem(last=False)
            return block_data

    def _scan_block(self, idx, norm):
        """在单个 key block 内逐 key 匹配，命中则返回数据，否则 None。"""
        keys_block = self._get_key_block(idx)
        base_abs = self._key_count_prefix[idx]
        for local_idx, (rec_offset, key_bytes) in enumerate(keys_block):
            if self._normalize_path(key_bytes.decode('utf-8', errors='ignore')) == norm:
                return self._get_data_by_abs_index(base_abs + local_idx)
        return None

    def get(self, path):
        """按路径取资源（raw bytes）。块级二分定位 block → 按需解压单 block → 块内精确匹配。"""
        norm = self._normalize_path(path)
        metas = self._key_blocks_meta
        if not metas:
            return None
        start = bisect_left(self._key_last_lower, norm)
        for idx in range(start, len(metas)):
            if self._key_first_lower[idx] > norm:
                break
            data = self._scan_block(idx, norm)
            if data is not None:
                return data
        # 兜底：个别 MDD 的 key 排序与归一化后字典序不一致时，全块元数据扫描
        # （只比对 first/last，命中区间才解压，开销 O(块数)，可忽略）
        for idx in range(len(metas)):
            lo, hi = self._key_first_lower[idx], self._key_last_lower[idx]
            # 【#9】V3 / collation 排序下块内首词可能"大于"尾词，
            # 直接 first <= norm <= last 会恒为假，资源照样 404。
            if lo > hi: lo, hi = hi, lo
            if lo <= norm <= hi:
                data = self._scan_block(idx, norm)
                if data is not None:
                    return data
        return None

    def _get_data_by_abs_index(self, abs_idx):
        i = bisect_right(self._key_count_prefix, abs_idx) - 1
        if i < 0 or i >= len(self._key_blocks_meta):
            return None
        keys_block = self._get_key_block(i)
        local_idx = abs_idx - self._key_count_prefix[i]
        if local_idx < 0 or local_idx >= len(keys_block):
            return None
        record_start_offset = keys_block[local_idx][0]
        target_rb_idx = bisect_right(self._rec_decomp_prefix, record_start_offset) - 1
        if target_rb_idx < 0 or target_rb_idx >= len(self._record_blocks_meta):
            return None
        rec_block = self._get_record_block(target_rb_idx)
        rb_start_offset = self._rec_decomp_prefix[target_rb_idx]
        rb_end_offset = rb_start_offset + len(rec_block)
        if local_idx + 1 < len(keys_block):
            end_offset = keys_block[local_idx + 1][0]
        elif i + 1 < len(self._key_blocks_meta):
            next_block = self._get_key_block(i + 1)
            end_offset = next_block[0][0] if next_block else rb_end_offset
        else:
            end_offset = rb_end_offset
        if end_offset > rb_end_offset:
            end_offset = rb_end_offset
        return rec_block[record_start_offset - rb_start_offset: end_offset - rb_start_offset]

    def close(self):
        self._record_cache.clear()
        self._key_cache.clear()
        try:
            self._fh.close()
        except Exception:
            pass
