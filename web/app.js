/* ToyMDict 主窗口逻辑（前后端分离，由 index.html 通过 /static/app.js 加载） */
'use strict';

// 词条 iframe 的 origin，用于校验 postMessage 来源；由 index.html 注入
var ENTRY_ORIGIN = window.TOYMDICT_ORIGIN || '';

function isTrustedMessage(event) {
    // 主窗口由 pywebview 以 html= 加载，origin 可能为 null；
    // 词条 iframe 来自本地资源服务，其 origin 应等于 ENTRY_ORIGIN。
    return !!ENTRY_ORIGIN && event.origin === ENTRY_ORIGIN;
}

// ---------- 通用工具 ----------
function escapeHtml(text) {
    // 完整转义引号：分组名会被放进 value="..." 属性中
    return String(text == null ? '' : text)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function buildOptions(groups, current) {
    var frag = document.createDocumentFragment();
    (groups || []).forEach(function (g) {
        var opt = document.createElement('option');
        opt.value = g.name;
        opt.textContent = g.name;
        if (g.name === current) opt.selected = true;
        frag.appendChild(opt);
    });
    return frag;
}

function updateUI(data) {
    ['groupSelect', 'groupSelectGroupView'].forEach(function (id) {
        var sel = document.getElementById(id);
        if (!sel) return;
        sel.innerHTML = '';
        sel.appendChild(buildOptions(data.groups, data.current));
    });
}

// ---------- 菜单 / 搜索 ----------
function toggleMenu(id) {
    document.querySelectorAll('.dropdown').forEach(function (el) {
        if (el.id !== id) el.classList.remove('show');
    });
    document.getElementById(id).classList.toggle('show');
}

document.addEventListener('click', function (e) {
    if (!e.target.closest('.menu-container')) {
        document.querySelectorAll('.dropdown').forEach(function (el) {
            el.classList.remove('show');
        });
    }
});

document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
        var openModal = Array.prototype.slice.call(document.querySelectorAll('.modal'))
            .find(function (el) { return el.style.display === 'flex'; });
        if (openModal) {
            if (openModal.id === 'errorModal') closeErrorModal();
            else closeModals();
        }
    }
});

function handleSearch(e) {
    if (e.key === 'Enter') triggerSearch();
}
function triggerSearch() {
    var keyword = document.getElementById('searchInput').value.trim();
    var useVariants = document.getElementById('variantCheck').checked;
    if (keyword) pywebview.api.search(keyword, useVariants);
}
function externalSearch(keyword) {
    keyword = (keyword || '').trim();
    if (!keyword) return;
    var input = document.getElementById('searchInput');
    if (input) input.value = keyword;
    triggerSearch();
}

// ---------- 结果列表 ----------
function updateResults(results) {
    var list = document.getElementById('resultList');
    list.innerHTML = '';
    (results || []).forEach(function (r, i) {
        var item = document.createElement('div');
        item.className = 'result-item';
        item.onclick = function () { showEntry(i); };
        var name = document.createElement('span');
        name.style.cssText = 'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;';
        name.textContent = r.key;
        var meta = document.createElement('span');
        meta.className = 'result-meta';
        meta.textContent = String(r.sources.length);
        item.appendChild(name);
        item.appendChild(meta);
        list.appendChild(item);
    });
}

function setContent(dictDataArray) {
    var container = document.getElementById('contentArea');
    container.innerHTML = '';

    // 消息处理器按 iframe id 查找，幂等，只需注册一次
    if (!window._iframeMsgHandler) {
        window._iframeMsgHandler = function (event) {
            if (!isTrustedMessage(event)) return;   // 只接受本地词条页消息
            var data = event.data || {};
            if (data.type === 'resize') {
                var iframe = document.getElementById(data.id);
                if (iframe && typeof data.height === 'number' &&
                    parseInt(iframe.style.height, 10) !== data.height) {
                    iframe.style.height = data.height + 'px';
                }
            }
        };
        window.addEventListener('message', window._iframeMsgHandler);
    }

    (dictDataArray || []).forEach(function (item, index) {
        var details = document.createElement('details');
        details.className = 'dict-block';
        if (index === 0) details.open = true;

        var summary = document.createElement('summary');
        summary.className = 'dict-summary';
        summary.innerText = '📖 ' + item.dict_name;
        details.appendChild(summary);

        var wrapper = document.createElement('div');
        wrapper.style.cssText = 'padding: 0; margin: 0;';

        var iframe = document.createElement('iframe');
        var iframeId = 'dict-iframe-' + index;
        iframe.id = iframeId;
        iframe.style.cssText = 'width: 100%; border: none; height: 0px; overflow: hidden !important;';
        iframe.setAttribute('frameborder', '0');
        iframe.setAttribute('scrolling', 'no');
        // 不启用 sandbox：allow-scripts + allow-same-origin 组合可被 iframe 自行
        // 解除，不加 allow-same-origin 又会让 @font-face 字体加载失败、且
        // postMessage 的 origin 变成 "null" 导致父窗口校验失效。这里以父窗口
        // 的 origin 校验作为隔离手段，CSP/sandbox 留待后续专项处理。
        iframe.src = item.url;

        wrapper.appendChild(iframe);
        details.appendChild(wrapper);

        details.addEventListener('toggle', function () {
            if (details.open) {
                setTimeout(function () {
                    try { iframe.contentWindow.postMessage('calcHeight', '*'); } catch (e) {}
                }, 100);
            }
        });
        container.appendChild(details);
    });
}

function showEntry(index) {
    var items = document.querySelectorAll('.result-item');
    if (index < 0 || index >= items.length) return;
    items.forEach(function (el) { el.classList.remove('active'); });
    items[index].classList.add('active');
    pywebview.api.show_entry(index);
}

// ---------- 分组管理 ----------
function showGroupView() {
    document.getElementById('groupView').style.display = 'flex';
    pywebview.api.init_group_view();
}
function showMainView() {
    document.getElementById('groupView').style.display = 'none';
}

function renderGroupView(allDicts, groups, currentGroup) {
    ['groupSelect', 'groupSelectGroupView'].forEach(function (id) {
        var sel = document.getElementById(id);
        if (!sel) return;
        sel.innerHTML = '';
        sel.appendChild(buildOptions(groups, currentGroup));
    });
    var currentGroupData = (groups || []).find(function (g) { return g.name === currentGroup; });

    var allList = document.getElementById('allDictsList');
    allList.innerHTML = '';
    (allDicts || []).forEach(function (d) {
        if (d.status === 'active') return;
        var li = document.createElement('li');
        if (d.status === 'excluded') li.className = 'dict-excluded';

        var span = document.createElement('span');
        span.innerText = d.name;
        span.style.flex = '1';
        span.ondblclick = function () { pywebview.api.get_dict_info(d.id); };

        var btnGroup = document.createElement('div');
        btnGroup.className = 'btn-group';

        if (d.status === 'excluded') {
            var btn = document.createElement('span');
            btn.innerText = '↻';
            btn.className = 'action-btn sort-btn';
            btn.title = '恢复并重新加载';
            btn.onclick = function (e) {
                e.stopPropagation(); pywebview.api.reload_excluded_dict(d.id);
            };
            btnGroup.appendChild(btn);
        } else {
            var btn1 = document.createElement('span');
            btn1.innerText = '✕';
            btn1.className = 'action-btn remove-btn';
            btn1.title = '排除并卸载';
            btn1.style.marginRight = '6px';
            btn1.onclick = function (e) {
                e.stopPropagation(); pywebview.api.exclude_dict(d.id);
            };
            btnGroup.appendChild(btn1);

            var btn2 = document.createElement('span');
            btn2.innerText = '+';
            btn2.className = 'action-btn add-btn';
            btn2.title = '添加至分组';
            btn2.onclick = function (e) {
                e.stopPropagation(); pywebview.api.add_dict_to_group(d.id);
            };
            btnGroup.appendChild(btn2);
        }

        li.appendChild(span);
        li.appendChild(btnGroup);
        allList.appendChild(li);
    });

    var filterInput = document.getElementById('allDictFilter');
    if (filterInput) filterAllDicts(filterInput.value);

    var groupList = document.getElementById('groupDictsList');
    groupList.innerHTML = '';
    if (currentGroupData) {
        currentGroupData.dicts.forEach(function (d) {
            var li = document.createElement('li');
            var span = document.createElement('span');
            span.innerText = d.name;
            span.style.flex = '1';
            span.ondblclick = function () { pywebview.api.get_dict_info(d.id); };

            var btnGroup = document.createElement('div');
            btnGroup.className = 'btn-group';
            [{ text: '⤒', action: 'top', title: '置顶' },
             { text: '↑', action: 'up', title: '上移' },
             { text: '↓', action: 'down', title: '下移' },
             { text: '⤓', action: 'bottom', title: '置底' }].forEach(function (a) {
                var b = document.createElement('span');
                b.innerText = a.text;
                b.title = a.title;
                b.className = 'action-btn sort-btn';
                b.onclick = function (e) {
                    e.stopPropagation(); pywebview.api.move_dict(d.id, a.action);
                };
                btnGroup.appendChild(b);
            });

            var removeBtn = document.createElement('span');
            removeBtn.innerText = '✕';
            removeBtn.className = 'remove-btn';
            removeBtn.title = '从分组移除';
            removeBtn.onclick = function (e) {
                e.stopPropagation(); pywebview.api.remove_dict_from_group(d.id);
            };
            btnGroup.appendChild(removeBtn);

            li.appendChild(span);
            li.appendChild(btnGroup);
            groupList.appendChild(li);
        });
    }
}

function filterAllDicts(keyword) {
    var kw = (keyword || '').trim().toLowerCase();
    var list = document.getElementById('allDictsList');
    if (!list) return;
    var items = list.getElementsByTagName('li');
    for (var i = 0; i < items.length; i++) {
        var span = items[i].querySelector('span');
        var name = span ? span.innerText.toLowerCase() : '';
        items[i].style.display = (!kw || name.indexOf(kw) !== -1) ? '' : 'none';
    }
}

// ---------- 弹窗 ----------
function showDictInfoModal(title, body) {
    document.getElementById('dictInfoTitle').innerText = title;
    document.getElementById('dictInfoBody').innerHTML = body;
    document.getElementById('dictInfoModal').style.display = 'flex';
}
function closeDictInfo() {
    document.getElementById('dictInfoModal').style.display = 'none';
}
function showAddGroupModal() {
    document.getElementById('addGroupModal').style.display = 'flex';
    document.getElementById('newGroupName').focus();
}
function handleAddGroupKey(e) {
    if (e.key === 'Enter') confirmAddGroup();
}
function closeModals() {
    document.querySelectorAll('.modal').forEach(function (el) {
        if (el.id !== 'errorModal') el.style.display = 'none';
    });
}
function confirmAddGroup() {
    var name = document.getElementById('newGroupName').value.trim();
    if (!name) {
        alert('请输入分组名称');
        document.getElementById('newGroupName').focus();
        return;
    }
    pywebview.api.add_group(name).then(function (result) {
        if (result && result.ok) {
            closeModals();
            document.getElementById('newGroupName').value = '';
        } else if (result && result.error) {
            showError('新建分组失败', result.error, 'warning');
            var input = document.getElementById('newGroupName');
            input.focus();
            input.select();
        }
    });
}
function deleteCurrentGroup() {
    if (confirm('确定删除当前分组？')) pywebview.api.delete_group();
}

// 后端通过 utils/error_reporter.py 调用 showError(title, message, level)
function showError(title, message, level) {
    var modal = document.getElementById('errorModal');
    var body = document.getElementById('errorModalBody');
    document.getElementById('errorModalTitle').innerText = title || '提示';
    var entry = document.createElement('div');
    entry.className = 'error-entry';
    entry.textContent = (level === 'warning' ? '⚠ ' : '✖ ') + String(message == null ? '' : message);
    body.appendChild(entry);
    body.scrollTop = body.scrollHeight;
    modal.style.display = 'flex';
}
function closeErrorModal() {
    document.getElementById('errorModal').style.display = 'none';
    document.getElementById('errorModalBody').innerHTML = '';
}

// 前端自身异常也弹窗，避免静默失败
window.addEventListener('error', function (e) {
    showError('前端错误', (e.message || '') + ' @ ' + (e.filename || '') + ':' + (e.lineno || 0), 'error');
});
window.addEventListener('unhandledrejection', function (e) {
    var reason = e.reason && e.reason.message ? e.reason.message : String(e.reason);
    showError('前端错误', reason, 'error');
});

// ---------- 词条页消息 ----------
window.addEventListener('message', function (event) {
    if (!isTrustedMessage(event)) return;
    var data = event.data || {};
    if (data.type === 'entry-link' && data.word) {
        var input = document.getElementById('searchInput');
        input.value = data.word;
        input.focus();
        var useVariants = document.getElementById('variantCheck').checked;
        pywebview.api.search(data.word, useVariants);
    } else if (data.type === 'open-image' && data.url) {
        if (typeof window.showImageOverlay === 'function') window.showImageOverlay(data.url);
    }
});

// ---------- 图片预览浮层 ----------
(function () {
    var overlay = document.getElementById('imageOverlay');
    var img = document.getElementById('imageOverlayImg');
    var zoomLabel = document.getElementById('imgZoomLabel');
    overlay.setAttribute('tabindex', '-1');
    var scale = 1, posX = 0, posY = 0, dragging = false, startX = 0, startY = 0;

    function applyTransform() {
        img.style.transform = 'translate(' + posX + 'px,' + posY + 'px) scale(' + scale + ')';
        zoomLabel.textContent = Math.round(scale * 100) + '%';
    }
    window.showImageOverlay = function (url) {
        if (!url) return;
        img.src = url;
        overlay.classList.add('show');
        overlay.focus();
        scale = 1; posX = 0; posY = 0; applyTransform();
    };
    function closeOverlay() {
        overlay.classList.remove('show');
        img.removeAttribute('src');   // 置空 src 会把当前页面 URL 当作图片地址，故用 removeAttribute
        scale = 1; posX = 0; posY = 0;
    }
    function zoomBy(factor) {
        scale = Math.max(0.1, Math.min(scale * factor, 10));
        applyTransform();
    }
    overlay.querySelector('.img-overlay-toolbar').addEventListener('click', function (e) {
        var act = e.target.getAttribute('data-act');
        if (!act) return;
        if (act === 'zoom-in') zoomBy(1.2);
        else if (act === 'zoom-out') zoomBy(1 / 1.2);
        else if (act === 'reset') { scale = 1; posX = 0; posY = 0; applyTransform(); }
        else if (act === 'close') closeOverlay();
    });
    overlay.addEventListener('wheel', function (e) {
        e.preventDefault();
        zoomBy(e.deltaY < 0 ? 1.1 : 1 / 1.1);
    }, { passive: false });
    img.addEventListener('mousedown', function (e) {
        dragging = true; startX = e.clientX - posX; startY = e.clientY - posY;
        img.classList.add('grabbing');
    });
    window.addEventListener('mousemove', function (e) {
        if (!dragging) return;
        posX = e.clientX - startX; posY = e.clientY - startY; applyTransform();
    });
    window.addEventListener('mouseup', function () {
        if (dragging) { dragging = false; img.classList.remove('grabbing'); }
    });
    img.addEventListener('dblclick', function (e) {
        e.stopPropagation(); scale = 1; posX = 0; posY = 0; applyTransform();
    });
    overlay.addEventListener('click', function (e) {
        if (e.target === overlay) closeOverlay();
    });

    // Esc 关闭：页面顶部的全局 keydown 只处理 .modal（display:flex），
    // 而本浮层用 .show 控制显示，所以必须单独监听。用捕获阶段保证
    // 优先于其它键盘逻辑；浮层显示时 Esc 只关图片，不误关背后的弹窗。
    function isEscKey(e) {
        // 部分 WebView 内核（含 IE/MSHTML）没有 e.key，只能用 keyCode
        return e.key === 'Escape' || e.key === 'Esc' || e.keyCode === 27;
    }
    document.addEventListener('keydown', function (e) {
        if (!isEscKey(e) || !overlay.classList.contains('show')) return;
        e.preventDefault();
        e.stopPropagation();
        closeOverlay();
    }, true);

    // 词条正文在 iframe 内，焦点可能停留在 iframe 里，顶层窗口收不到
    // 键盘事件。由 entry.js 把 Esc 转发成 'esc' 消息，这里统一处理。
    window.addEventListener('message', function (event) {
        if (!isTrustedMessage(event)) return;
        var data = event.data || {};
        if (data.type === 'esc' && overlay.classList.contains('show')) {
            closeOverlay();
        }
    });
})();

// ---------- 启动期后端错误 ----------
// create_window() 之后页面尚未加载，后端在此期间调用 evaluate_js 会失败，
// 错误被暂存在 utils/error_reporter.py。页面就绪后主动拉取并弹窗。
var _pendingRetries = 0;
function pullPendingErrors() {
    if (!window.pywebview || !pywebview.api || !pywebview.api.get_pending_errors) {
        if (_pendingRetries++ < 25) setTimeout(pullPendingErrors, 200);
        return;
    }
    try {
        pywebview.api.get_pending_errors().then(function (items) {
            (items || []).forEach(function (item) {
                showError(item[0], item[1], item[2]);
            });
        });
    } catch (e) {
        if (_pendingRetries++ < 25) setTimeout(pullPendingErrors, 200);
    }
}
window.addEventListener('pywebviewready', pullPendingErrors);
document.addEventListener('DOMContentLoaded', pullPendingErrors);

// ==================== 高级搜索 ====================
// 结果不合并、不排序：后端按「词典完成顺序 + 词典内索引顺序」流式追加，
// 前端只负责把每批结果 append 到主界面结果列表，且索引与后端 _current_results 对齐。
var _advState = { dicts: [], selected: {}, scope: 'entry' };
var _advResultCount = 0;

function openAdvancedSearch() {
    var modal = document.getElementById('advancedSearchModal');
    if (!modal) return;
    modal.style.display = 'flex';
    onAdvScopeChange();
    updateAdvSelectedCount();
    if (!window.pywebview || !pywebview.api || !pywebview.api.get_advanced_search_dicts) {
        renderAdvDicts();
        return;
    }
    pywebview.api.get_advanced_search_dicts().then(function (dicts) {
        _advState.dicts = dicts || [];
        // 默认全不选；切换分组后新出现的词典也默认不勾选，已有勾选状态保留
        _advState.dicts.forEach(function (d) {
            if (!(d.id in _advState.selected)) _advState.selected[d.id] = false;
        });
        renderAdvDicts();
        updateAdvSelectedCount();
    });
    setTimeout(function () {
        var el = document.getElementById('advPattern');
        if (el) el.focus();
    }, 50);
}

function onAdvScopeChange() {
    var checked = document.querySelector('input[name="advScope"]:checked');
    _advState.scope = checked ? checked.value : 'entry';
    document.querySelectorAll('.adv-scope').forEach(function (label) {
        var input = label.querySelector('input');
        label.classList.toggle('active', !!input && input.checked);
    });
    var pattern = document.getElementById('advPattern');
    if (pattern) pattern.placeholder = _advState.scope === 'entry'
        ? '条目正则，例如 ^黄.* 或 説|說'
        : '全文正则，例如 黄钟.{0,10}大吕';
}

function renderAdvDicts() {
    var list = document.getElementById('advDictList');
    if (!list) return;
    var filter = document.getElementById('advDictFilter');
    var kw = ((filter && filter.value) || '').trim().toLowerCase();
    list.innerHTML = '';
    if (!_advState.dicts.length) {
        var empty = document.createElement('div');
        empty.className = 'adv-empty';
        empty.textContent = '当前分组没有词典，请先在「词典分组」中添加。';
        list.appendChild(empty);
        return;
    }
    _advState.dicts.forEach(function (d) {
        var label = document.createElement('label');
        label.className = 'adv-dict-item';
        // 过滤只影响显示，不改变勾选状态：仍然「只搜索选中的词典」
        if (kw && String(d.name).toLowerCase().indexOf(kw) === -1) {
            label.style.display = 'none';
        }
        var cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.checked = !!_advState.selected[d.id];
        cb.onchange = function () {
            _advState.selected[d.id] = cb.checked;
            updateAdvSelectedCount();
        };
        var name = document.createElement('span');
        name.className = 'adv-dict-name';
        name.textContent = d.name;
        name.title = d.id;
        label.appendChild(cb);
        label.appendChild(name);
        list.appendChild(label);
    });
}

function advSelectAll(value) {
    _advState.dicts.forEach(function (d) { _advState.selected[d.id] = value; });
    renderAdvDicts();
    updateAdvSelectedCount();
}

function updateAdvSelectedCount() {
    var n = _advState.dicts.filter(function (d) { return _advState.selected[d.id]; }).length;
    var el = document.getElementById('advSelectedCount');
    if (el) el.textContent = '已选 ' + n + ' / ' + _advState.dicts.length + ' 本';
}

function handleAdvPatternKey(e) {
    if (e.key === 'Enter') startAdvancedSearch();
}

function startAdvancedSearch() {
    var patternEl = document.getElementById('advPattern');
    var variantEl = document.getElementById('advVariantCheck');
    var pattern = (patternEl && patternEl.value || '').trim();
    var useVariants = !!(variantEl && variantEl.checked);
    var ids = _advState.dicts
        .filter(function (d) { return _advState.selected[d.id]; })
        .map(function (d) { return d.id; });

    if (!pattern) { showError('高级搜索', '请输入搜索表达式', 'warning'); return; }
    if (!ids.length) { showError('高级搜索', '请至少勾选一本词典', 'warning'); return; }
    if (!window.pywebview || !pywebview.api || !pywebview.api.advanced_search) {
        showError('高级搜索', '后端接口不可用', 'error');
        return;
    }
    pywebview.api.advanced_search(pattern, _advState.scope, useVariants, ids)
        .then(function (res) {
            if (!res || !res.ok) {
                showError('高级搜索', (res && res.error) || '启动失败', 'warning');
                return;
            }
            // 结果回到主界面左侧结果列表，收起二级界面以便实时查看
            closeModals();
        });
}

// ---------- 流式结果列表（由后端 evaluate_js 调用） ----------
function clearAdvancedResults(expanded) {
    var list = document.getElementById('resultList');
    if (!list) return;
    list.innerHTML = '';
    _advResultCount = 0;

    var status = document.createElement('div');
    status.id = 'advStatus';
    status.className = 'adv-status';
    var first = document.createElement('div');
    first.textContent = '高级搜索中… 正在扫描词典';
    var expr = document.createElement('div');
    expr.style.cssText = 'font-size:12px;opacity:.8;';
    expr.textContent = '实际表达式：' + (expanded || '');
    var stop = document.createElement('span');
    stop.className = 'adv-stop';
    stop.textContent = '停止';
    stop.onclick = function () {
        if (window.pywebview && pywebview.api && pywebview.api.cancel_advanced_search) {
            pywebview.api.cancel_advanced_search();
        }
    };
    status.appendChild(first);
    status.appendChild(expr);
    status.appendChild(stop);
    list.appendChild(status);
}

function appendAdvancedResults(items) {
    var list = document.getElementById('resultList');
    if (!list || !items || !items.length) return;
    items.forEach(function (r) {
        var item = document.createElement('div');
        item.className = 'result-item adv-result';
        var idx = _advResultCount++;
        item.onclick = function () { showEntry(idx); };

        // 结果列表只显示词条名：不显示词典名，也不显示全文预览片段。
        // 全文命中在打开词条后由 iframe 内的 entry.js 在正文里高亮。
        var key = document.createElement('span');
        key.className = 'adv-result-key';
        key.textContent = r.key;
        item.appendChild(key);
        list.appendChild(item);
    });
}

function updateAdvancedStatus(done, total, hits) {
    var status = document.getElementById('advStatus');
    if (!status) return;
    var finished = done >= total;
    status.classList.toggle('done', finished);
    if (status.firstChild) {
        status.firstChild.textContent = finished
            ? '高级搜索完成：' + done + ' 本词典，命中 ' + hits + ' 条'
            : '高级搜索中… 已完成 ' + done + ' / ' + total + ' 本词典，命中 ' + hits + ' 条';
    }
    var stop = status.querySelector('.adv-stop');
    if (stop) stop.style.display = finished ? 'none' : '';
}

function finishAdvancedStatus() {
    var status = document.getElementById('advStatus');
    if (!status) return;
    status.classList.add('done');
    if (status.firstChild) status.firstChild.textContent = '高级搜索已停止';
    var stop = status.querySelector('.adv-stop');
    if (stop) stop.style.display = 'none';
}
