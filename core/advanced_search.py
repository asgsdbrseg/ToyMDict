# -*- coding: utf-8 -*-
"""高级搜索核心逻辑。

与普通搜索（前缀匹配 + 结果合并排序）不同，高级搜索的语义是：

* 输入恒为**正则表达式**（默认开启，界面上不提供开关）；
* 「异体字」开关打开时，把正则里的字面汉字展开成 ``[本字异体1异体2...]``，
  正则元字符、转义序列与量词保持原样；
* 范围可选 ``entry``（只扫词条名）或 ``fulltext``（扫释义正文，先去 HTML 标签）；
* 逐本词典顺序扫描，命中即刻分批回调，**不合并、不排序、不等全部结果**；
* 扫描过程可随时取消（由调用方传入的 ``is_cancelled`` 决定）。

结果列表只回传词条名与索引；全文命中在词条正文里高亮，由前端 iframe 的
``entry.js`` 依据同一份展开后的正则完成，因此不在后端生成任何预览片段。

本模块只依赖标准库，便于单独测试；词典访问通过 ``MdxWrapper`` 暴露的
``iter_entries()`` / ``get_raw_content()`` 完成，不触碰 ``readmdict`` 私有属性。
"""
import html as html_module
import re

# 每本词典的命中上限，防止「.」之类表达式在超大词典上产生无限结果。
# 这是安全阀而非常规限制：正常查词几乎不会触及。
MAX_HITS_PER_DICT = 10000
DEFAULT_BATCH_SIZE = 30

# 注释与脚本/样式块不属于释义文本，整体剔除，避免标签内的源码被当成正文。
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_SCRIPT_RE = re.compile(r"<(script|style|noscript)\b[^>]*>.*?</\1\s*>", re.S | re.I)
# 所有标签一律**直接删除、不留空格**：MDX 常把词组拆进相邻的行内标签，
# 例如 <u>南朝</u><u>宋</u>。插入空格会让「南朝宋」永远匹配不到。
# 浏览器的 textContent 同样不含标签，因此这里与页内高亮所用的文本一致。
_TAG_RE = re.compile(r"<[^>]*>", re.S)
_WS_RE = re.compile(r"\s+")

# 正则元字符：这些字符在字符类之外必须原样保留，不能替换成异体字字符类
_META_CHARS = set(r"\.^$*+?{}[]|()")


def strip_html(raw) -> str:
    """把 MDX 记录（bytes 或 str）转成用于正则匹配的纯文本。

    处理顺序：去注释 → 去 script/style 块 → 删除全部标签 → HTML 实体还原
    → 压缩连续空白 → 去首尾空白。``@@@LINK=`` 重定向词没有正文，直接跳过。
    """
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="ignore")
    else:
        text = str(raw)
    text = text.strip()
    if text.startswith("@@@LINK="):
        return ""
    text = _COMMENT_RE.sub("", text)
    text = _SCRIPT_RE.sub(" ", text)
    text = _TAG_RE.sub("", text)
    text = html_module.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def _variants_of(char, variant_handler) -> list:
    """返回某个字符的异体字集合（含自身）；无映射或处理器为空时返回 [自身]。"""
    if variant_handler is None:
        return [char]
    try:
        variants = variant_handler.get_variants(char)
    except Exception:
        return [char]
    if not variants:
        return [char]
    # variants.json 里的值是 list；也可能被换成 set。去重后让「原字」排在首位，
    # 其余按码点排序，保证展开出的字符类稳定且可读（如 [钟鍾鐘]）
    unique = sorted({str(v) for v in variants} | {char})
    if char in unique:
        unique.remove(char)
        unique.insert(0, char)
    return unique if len(unique) > 1 else [char]


def expand_pattern_variants(pattern: str, variant_handler) -> str:
    """把正则中的字面字符按异体字表展开成字符类。

    例：``黄钟`` → ``[黄黃][钟鍾鐘]``；``^黄.*钟$`` 中的 ``^`` ``.*`` ``$`` 原样保留。

    处理规则：
    * ``\\x`` 转义序列整段保留，不展开；
    * ``[...]`` 字符类内部：把每个字面字符的异体字追加进该类；
    * 字符类外的元字符原样保留，其余字面字符有多异体时包成 ``[...]``；
    * 没有异体字的字符仅做 ``re.escape``，避免破坏正则语义。
    """
    if not pattern or variant_handler is None:
        return pattern

    out = []
    in_class = False
    i = 0
    length = len(pattern)
    while i < length:
        ch = pattern[i]

        # 转义序列整体保留（\d、\(、\u4e00 等）
        if ch == "\\" and i + 1 < length:
            out.append(pattern[i:i + 2])
            i += 2
            continue

        if ch == "[" and not in_class:
            in_class = True
            out.append(ch)
            i += 1
            continue
        if ch == "]" and in_class:
            in_class = False
            out.append(ch)
            i += 1
            continue

        variants = _variants_of(ch, variant_handler)
        if len(variants) > 1:
            escaped = "".join(re.escape(v) for v in variants)
            if in_class:
                # 字符类内：把异体字并进当前类，不额外套一层 []
                out.append(escaped)
            else:
                out.append("[" + escaped + "]")
        else:
            if in_class:
                out.append(re.escape(ch))
            elif ch in _META_CHARS:
                out.append(ch)
            else:
                out.append(re.escape(ch))
        i += 1
    return "".join(out)


def compile_advanced_pattern(pattern: str, use_variants: bool,
                             variant_handler=None):
    """编译高级搜索正则，返回 ``(compiled, expanded_pattern)``。

    统一启用 ``IGNORECASE``（英文大小写不敏感）与 ``DOTALL``（``.`` 可跨行，
    便于全文匹配去标签后仍带换行的正文）。非法正则抛 ``re.error`` 由上层提示。

    ``expanded_pattern`` 同时供前端 iframe 高亮使用（JS 侧以 ``gis`` 编译），
    保证「搜到的」和「标黄的」是同一个表达式。
    """
    if not pattern or not pattern.strip():
        raise re.error("表达式不能为空")
    pattern = pattern.strip()
    expanded = (expand_pattern_variants(pattern, variant_handler)
                if use_variants else pattern)
    return re.compile(expanded, re.IGNORECASE | re.DOTALL), expanded


def scan_dictionary(wrapper, regex, scope: str, on_batch, is_cancelled=None,
                    batch_size: int = DEFAULT_BATCH_SIZE,
                    max_hits: int = MAX_HITS_PER_DICT) -> int:
    """顺序扫描单本词典，命中分批交给 ``on_batch(list[dict])``，返回命中总数。

    结果顺序 = 词典内词条的原始索引顺序，不排序；不同词典之间由调用方
    （各自线程完成顺序）决定追加先后，同样不排序。

    ``on_batch`` 收到的每项为 ``{"key", "idx"}``：结果列表只显示词条名，
    全文命中在打开词条后由前端在正文里高亮，不再生成预览片段。
    """
    if scope not in ("entry", "fulltext"):
        raise ValueError(f"未知的搜索范围: {scope}")

    hits = []
    total = 0
    for idx, key in wrapper.iter_entries():
        if is_cancelled is not None and is_cancelled():
            break
        text = key if scope == "entry" else strip_html(wrapper.get_raw_content(idx))
        match = regex.search(text) if text else None
        if not match:
            continue

        hits.append({"key": key, "idx": idx})
        total += 1
        if len(hits) >= batch_size:
            on_batch(hits)
            hits = []
        if total >= max_hits:
            break

    if hits:
        on_batch(hits)
    return total
