/* web/static/app.js — Shared utilities */

function escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

let _toastTimer = null;
function showToast(msg) {
  let el = document.getElementById('app-toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'app-toast';
    el.className = 'app-toast';
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.remove('show'), 1500);
}

function fallbackCopy(text, done) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  try { document.execCommand('copy'); if (done) done(); }
  catch (e) { alert('复制失败，请从悬浮提示手动复制'); }
  document.body.removeChild(ta);
}

function copyCid(cid) {
  if (!cid) return;
  const ok = () => showToast('已复制 condition ID');
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(cid).then(ok).catch(() => fallbackCopy(cid, ok));
  } else {
    fallbackCopy(cid, ok);
  }
}

// 市场单元格:名称(有链接则 <a>,无则纯文本;名称缺失显示截断 condition_id)
// + 📋 复制完整 condition_id 按钮。
function marketCell(name, conditionId, url) {
  const cid = conditionId || '';
  const label = name || (cid ? cid.slice(0, 8) + '...' + cid.slice(-6) : '');
  const safe = escapeHtml(label);
  const inner = url
    ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener">${safe}</a>`
    : safe;
  const copyBtn = cid
    ? ` <button class="btn-xs" title="复制完整 condition ID"` +
      ` onclick="copyCid('${escapeHtml(cid)}')">📋</button>`
    : '';
  return `<span title="${escapeHtml(cid)}">${inner}${copyBtn}</span>`;
}

// 全局扫描指示器:每个页面都轮询后端扫描状态,显示在侧边栏 —— 扫描是后端后台线程,
// 切页不会停;这里让进度在任意页面都可见、切页也不丢(点击回到市场发现页)。
(function () {
  function tick() {
    fetch('/api/engine/scan-status').then(r => r.json()).then(d => {
      const el = document.getElementById('scan-indicator');
      if (!el) return;
      if (d && d.scan_status === 'scanning') {
        const c = d.scan_checked || 0, t = d.scan_total || 0;
        el.style.display = 'block';
        el.textContent = t > 0
          ? `⟳ 扫描中 ${c}/${t} · 已找到 ${d.found || 0}`
          : `⟳ 扫描中 · 已找到 ${d.found || 0}`;
      } else {
        el.style.display = 'none';
      }
    }).catch(() => {});
  }
  setInterval(tick, 3000);
  tick();
})();

// 全局拦截:任一 fetch 被重定向到登录/设置页(会话失效,典型场景=自动更新重启后
// 旧标签页仍开着)时,直接跳登录,避免页面静默停更 + .json() 解析登录 HTML 报错(F12)。
(function () {
  const _origFetch = window.fetch.bind(window);
  window.fetch = function (...args) {
    return _origFetch(...args).then((resp) => {
      if (resp && resp.redirected && /\/(login|setup)(\?|$)/.test(resp.url)) {
        window.location.href = '/login';
      }
      return resp;
    });
  };
})();
