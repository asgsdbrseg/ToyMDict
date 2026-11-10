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
})();
