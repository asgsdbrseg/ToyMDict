/* 词条 iframe 脚本：高度自适应 + 词条内链接/音频 + 图片双击预览。
 * 由 /entry/ 路由通过 <script src="/static/entry.js"> 引入，不再内嵌进每个词条 HTML。
 * iframe id 从 <body data-iframe-id="..."> 读取。
 */
(function () {
    'use strict';

    var iframeId = document.body && document.body.getAttribute('data-iframe-id') || 'dict-iframe-0';

    // ---------- 高度自适应 ----------
    var last = -1, raf = 0;
    function measure() {
        raf = 0;
        var d = document.getElementById('mdx-content');
        if (!d) return;
        var h = d.offsetHeight;
        if (h === last) return;              // 同高去重
        last = h;
        window.parent.postMessage({ type: 'resize', id: iframeId, height: h }, '*');
    }
    function schedule() {
        if (!raf) raf = requestAnimationFrame(measure);
    }
    var content = document.getElementById('mdx-content');
    if (window.ResizeObserver && content) {
        new ResizeObserver(schedule).observe(content);
    } else {
        window.addEventListener('load', schedule);
    }
    window.addEventListener('message', function (e) {
        if (e.data === 'calcHeight') schedule();
    });

    // ---------- 音频 ----------
    var currentAudio = null;
    function extractAudioUrl(onclick) {
        if (!onclick) return null;
        // 反向引用保证同种引号闭合；URL 允许含反引号（紧凑样式表遗留）
        var m = onclick.match(/new\s+Audio\(\s*(['"`])(.*?)\1\s*\)/);
        if (!m) return null;
        var url = m[2].trim().replace(/^`+|`+$/g, '');
        return url || null;
    }
    function playAudio(url) {
        if (!url) return;
        try {
            if (currentAudio) { currentAudio.pause(); currentAudio = null; }
            currentAudio = new Audio(url);
            currentAudio.play().catch(function (err) {
                console.error('音频播放失败:', err, 'URL:', url);
            });
        } catch (err) {
            console.error('音频播放异常:', err);
        }
    }
    function safeDecode(s) {
        try { return decodeURIComponent(s); } catch (e) { return s; }
    }

    document.addEventListener('click', function (e) {
        var a = e && e.target && e.target.closest && e.target.closest('a');
        if (!a) return;
        var href = (a.getAttribute('href') || '').trim();

        // HTML 重写阶段识别出的音频链接
        var dataAudioUrl = a.getAttribute('data-audio-url');
        if (dataAudioUrl) {
            e.preventDefault(); e.stopPropagation();
            playAudio(dataAudioUrl);
            return;
        }
        // 内联 onclick 中 new Audio() 播放
        var audioUrl = extractAudioUrl(a.getAttribute('onclick') || '');
        if (audioUrl) {
            e.preventDefault(); e.stopPropagation();
            playAudio(audioUrl);
            return;
        }

        if (href.toLowerCase().indexOf('entry://') === 0) {
            e.preventDefault(); e.stopPropagation();
            var word = safeDecode(href.substring(8));
            if (word.indexOf('#') !== -1) word = word.split('#')[0];
            if (word) window.parent.postMessage({ type: 'entry-link', word: word }, '*');
        } else if (href.toLowerCase().indexOf('sound://') === 0) {
            e.preventDefault(); e.stopPropagation();
            var soundPath = safeDecode(href.substring(8));
            if (soundPath.indexOf('/') === 0) soundPath = soundPath.substring(1);
            playAudio(document.baseURI + soundPath);
        } else if (!href) {
            e.preventDefault();
        }
    }, true);

    // ---------- 图片双击 ----------
    document.addEventListener('dblclick', function (e) {
        var img = e && e.target && e.target.closest && e.target.closest('img');
        if (!img) return;
        var src = '';
        try { src = img.currentSrc || img.src || ''; } catch (err) { src = img.src || ''; }
        if (src) window.parent.postMessage({ type: 'open-image', url: src }, '*');
    }, true);

    // ---------- Esc 转发 ----------
    // 焦点在 iframe 内时，主窗口的 document 收不到键盘事件，图片查看器
    // 的 Esc 就失效了。这里把 Esc 转发给主窗口，由 app.js 在浮层显示时关闭。
    // 主窗口只在浮层已显示时响应，因此不会影响其它弹窗的 Esc 行为。
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' || e.key === 'Esc' || e.keyCode === 27) {
            window.parent.postMessage({ type: 'esc' }, '*');
        }
    }, true);

    // ---------- 全文搜索命中高亮 ----------
    // /entry/ 的 hl 参数是「展开后的正则」，与后端扫描用的是同一份表达式。
    // 在文本节点上拼出与后端 strip_html 等价的纯文本：标签不占位、连续空白
    // 压成一个空格、去首尾空白。因此 <u>南朝</u><u>宋</u> 会被当作连续的
    // 「南朝宋」，跨标签的命中按文本节点切分后分别包 <mark>，标签结构保持合法。
    function buildHighlightRegex(source) {
        try {
            return new RegExp(source, 'gis');   // i≈IGNORECASE, s≈DOTALL, g=全局
        } catch (e) {
            // Python 专有语法（如 (?P<name>...)）JS 无法编译，退化为字面量高亮
            try {
                return new RegExp(source.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi');
            } catch (e2) {
                return null;
            }
        }
    }

    function highlightInEntry() {
        var source;
        try { source = new URLSearchParams(location.search).get('hl'); } catch (e) { return; }
        if (!source) return;
        var root = document.getElementById('mdx-content');
        var regex = buildHighlightRegex(source);
        if (!root || !regex) return;

        var SKIP = {SCRIPT: 1, STYLE: 1, NOSCRIPT: 1, TEXTAREA: 1,
                    MARK: 1, SELECT: 1, OPTION: 1};
        var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
            acceptNode: function (node) {
                if (!node.nodeValue || !node.nodeValue.length) return NodeFilter.FILTER_REJECT;
                for (var p = node.parentNode; p && p !== root; p = p.parentNode) {
                    if (SKIP[String(p.nodeName).toUpperCase()]) return NodeFilter.FILTER_REJECT;
                }
                return NodeFilter.FILTER_ACCEPT;
            }
        });

        // text 是要匹配的字符串；chars[j] 记录 text[j] 落在哪个文本节点的哪个偏移
        var text = '', chars = [], ni = 0, pendingSpace = false, n;
        while ((n = walker.nextNode())) {
            var v = n.nodeValue;
            for (var i = 0; i < v.length; i++) {
                var c = v.charAt(i);
                if (/\s/.test(c)) {
                    if (!pendingSpace && text.length > 0) {
                        text += ' ';
                        chars.push({node: n, off: i, ni: ni});
                        pendingSpace = true;
                    }
                } else {
                    text += c;
                    chars.push({node: n, off: i, ni: ni});
                    pendingSpace = false;
                }
            }
            ni++;
        }
        // 与后端 strip() 对齐
        while (text.charAt(0) === ' ') { text = text.slice(1); chars.shift(); }
        while (text.charAt(text.length - 1) === ' ') { text = text.slice(0, -1); chars.pop(); }
        if (!text || !chars.length) return;

        var MAX_MARKS = 300, pieces = [], m, count = 0;
        regex.lastIndex = 0;
        while (count < MAX_MARKS && (m = regex.exec(text)) !== null) {
            if (!m[0].length) { regex.lastIndex++; continue; }
            var s = m.index, e = s + m[0].length, cur = null;
            for (var j = s; j < e; j++) {
                var rec = chars[j];
                if (!rec) continue;
                if (!cur || cur.node !== rec.node) {
                    cur = {node: rec.node, ni: rec.ni, start: rec.off, end: rec.off + 1};
                    pieces.push(cur);
                } else {
                    cur.end = rec.off + 1;
                }
            }
            count++;
        }
        if (!pieces.length) return;

        // 从后往前包裹：splitText 只影响当前节点后半段，不破坏前面片段的偏移
        pieces.sort(function (a, b) { return b.ni - a.ni || b.start - a.start; });
        pieces.forEach(function (p) {
            try {
                var range = document.createRange();
                range.setStart(p.node, p.start);
                range.setEnd(p.node, p.end);
                var mark = document.createElement('mark');
                mark.className = 'adv-hl';
                range.surroundContents(mark);
            } catch (err) {
                // 极端嵌套标签时跳过该片段，不影响词条正文本身
            }
        });
        schedule();   // <mark> 可能改变行高，重新量一次高度
    }

    try { highlightInEntry(); } catch (e) {
        // 高亮是增强功能，任何异常都不能影响词条正常显示
    }
})();
