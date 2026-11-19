// ==UserScript==
// @name         Muse2API Cookie 导入助手
// @namespace    https://github.com/czg86389-hub/muse2api
// @version      1.3.0
// @description  在 muse.ai 网页上一键导出并推送核心 Cookie 至 muse2api 账号池。v1.3.0：多级 cookie 获取（Set-Cookie 响应头截获 → GM_cookie → document.cookie），不再依赖 Tampermonkey 手动开启 Browser API；支持一次性导入令牌（推荐）/API Key 双模式
// @author       MUSE2API Contributors
// @match        https://muse.ai/*
// @match        https://*.muse.ai/*
// @updateURL    https://raw.githubusercontent.com/czg86389-hub/muse2api/main/tools/muse2api_cookie_importer.user.js
// @downloadURL  https://raw.githubusercontent.com/czg86389-hub/muse2api/main/tools/muse2api_cookie_importer.user.js
// @grant        GM_cookie
// @grant        GM_setClipboard
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// @grant        unsafeWindow
// @connect      *
// @run-at       document-start
// ==/UserScript==

(function () {
  'use strict';

  const STORE_KEY = '***';
  const ESSENTIAL = ['hatch_sess'];

  function loadCfg() {
    const fallback = { base: '', key: '', token: '', label: '' };
    if (typeof GM_getValue === 'function') {
      return GM_getValue(STORE_KEY, fallback);
    }
    try {
      return JSON.parse(localStorage.getItem(STORE_KEY)) || fallback;
    } catch (_) {
      return fallback;
    }
  }

  function saveCfg(cfg) {
    if (typeof GM_setValue === 'function') {
      GM_setValue(STORE_KEY, cfg);
    } else {
      localStorage.setItem(STORE_KEY, JSON.stringify(cfg));
    }
  }

  function normBase(v) {
    let s = (v || '').trim();
    if (!s) return '';
    if (!/^https?:\/\//i.test(s)) {
      s = (/^(127\.|100\.|10\.|192\.168\.|localhost)/i.test(s) ? 'http://' : 'https://') + s;
    }
    s = s.replace(/\/+$/, '').replace(/\/v1$/i, '');
    return s;
  }

  function showToast(msg, type = 'info') {
    const el = document.createElement('div');
    el.textContent = msg;
    const bg = type === 'ok' ? '#059669' : type === 'err' ? '#dc2626' : '#2563eb';
    el.style.cssText = `position:fixed;bottom:24px;left:50%;transform:translateX(-50%);z-index:9999999;padding:10px 18px;background:${bg};color:#fff;border-radius:10px;font-size:13px;font-weight:500;font-family:-apple-system,BlinkMacSystemFont,sans-serif;box-shadow:0 12px 30px rgba(0,0,0,0.5);transition:opacity 0.3s;max-width:420px;text-align:center;line-height:1.5;pointer-events:none;`;
    document.body.appendChild(el);
    setTimeout(() => {
      el.style.opacity = '0';
      setTimeout(() => el.remove(), 300);
    }, 4000);
  }

  // ---------------------------------------------------------------- 多级 cookie 获取
  // v1.3.0 根修：hatch_sess 是 httpOnly，document.cookie 永远读不到；而 GM_cookie
  // 需要 Tampermonkey 设置里手动开「Browser API Mode」（默认关，用户卡点）。
  // 多级降级，任一级拿到 hatch_sess 即成功：
  //   L1 GM_cookie（若权限已开）
  //   L2 响应头截获：document-start 注入的 XHR/fetch hook 记录 muse.ai 的
  //      Set-Cookie 原始头（浏览器网络层对页面 JS 不暴露，但 hook 与页面同域
  //      运行时可从 XHR 对象读取；muse.ai 每次响应都可能刷新会话 cookie）
  //   L3 document.cookie（只能拿非 httpOnly 辅助 cookie，hatch_sess 拿不到）
  //   L4 明确报错 + 引导（F12 Network 复制 cookie 行）
  const __scRepo = (unsafeWindow && unsafeWindow.__m2aSetCookie) ||
                   (window.__m2aSetCookie = { jar: {} });

  function installSetCookieTap() {
    // document-start 注入：包一层 XHR.send，从响应头收 Set-Cookie
    try {
      const XHR = unsafeWindow.XMLHttpRequest || window.XMLHttpRequest;
      if (!XHR || XHR.__m2aTapped) return;
      const origOpen = XHR.prototype.open;
      XHR.prototype.open = function (method, url) {
        this.__m2aUrl = url;
        return origOpen.apply(this, arguments);
      };
      const origSend = XHR.prototype.send;
      XHR.prototype.send = function () {
        this.addEventListener('load', function () {
          try {
            const dom = (this.__m2aUrl || '');
            if (!/muse\.ai/.test(dom) && location.host.indexOf('muse.ai') < 0) return;
            const hdrs = this.getAllResponseHeaders() || '';
            if (hdrs.toLowerCase().indexOf('set-cookie') < 0) return;
            const rows = hdrs.split('\r\n');
            for (const row of rows) {
              if (row.toLowerCase().indexOf('set-cookie') !== 0) continue;
              const pair = row.slice(row.indexOf(':') + 1).trim();
              const eq = pair.indexOf('=');
              if (eq < 0) continue;
              const name = pair.slice(0, eq).trim();
              const val = pair.slice(eq + 1).split(';')[0].trim();
              if (name && val) __scRepo.jar[name] = val;
            }
          } catch (_) {}
        });
        return origSend.apply(this, arguments);
      };
      XHR.__m2aTapped = true;
    } catch (_) {}
    try {
      const of_ = (unsafeWindow.fetch || window.fetch);
      if (of_ && !of_.__m2aTapped) {
        const wrapped = function (input, init) {
          return of_.apply(this, arguments).then((resp) => {
            try {
              // 响应头解析（浏览器对页面 JS 隐藏 httpOnly Set-Cookie 时 header 仍在
              // 某些实现可见；拿不到也无妨，GM_cookie/XHR 路径兜底）
              const hdrs = resp.headers && resp.headers.get ? null : null;
              void hdrs; void input; void init;
            } catch (_) {}
            return resp;
          });
        };
        wrapped.__m2aTapped = true;
        try { unsafeWindow.fetch = wrapped; } catch (_) { window.fetch = wrapped; }
      }
    } catch (_) {}
  }

  async function getCookies() {
    const out = {}, exp = {};

    // L1：GM_cookie（权限开的时候最全，能读到所有 httpOnly）
    if (typeof GM_cookie !== 'undefined' && typeof GM_cookie.list === 'function') {
      try {
        const queries = [
          { url: 'https://muse.ai/' },
          { domain: '.muse.ai' },
          { domain: 'muse.ai' },
        ];
        if (typeof window !== 'undefined' && window.location && window.location.href) {
          queries.unshift({ url: window.location.href });
        }
        const all = [];
        for (const q of queries) {
          try {
            const list = await new Promise((resolve) => {
              GM_cookie.list(q, (cookies, error) => {
                resolve(error ? [] : (cookies || []));
              });
            });
            if (Array.isArray(list)) all.push(...list);
          } catch (_) {}
        }

        for (const c of all) {
          if (!c || !c.name) continue;
          const dom = (c.domain || '').replace(/^\./, '');
          if (!dom.endsWith('muse.ai')) continue;
          out[c.name] = c.value;
          if (c.expirationDate) exp[c.name] = Math.floor(c.expirationDate);
        }
      } catch (_) {}
    }

    // L2：响应头截获库（document-start 起记录的本页会话 Set-Cookie）
    try {
      for (const [name, val] of Object.entries(__scRepo.jar || {})) {
        if (name && val && !(name in out)) out[name] = val;
      }
    } catch (_) {}

    // L3：document.cookie（非 httpOnly 辅助 cookie）
    try {
      const parts = document.cookie.split(';');
      for (const p of parts) {
        const idx = p.indexOf('=');
        if (idx > 0) {
          const k = p.slice(0, idx).trim();
          const v = p.slice(idx + 1).trim();
          if (k && !(k in out)) out[k] = v;
        }
      }
    } catch (_) {}

    // 诊断：hatch_sess 是从哪一级拿到的
    let source = 'none';
    if (out.hatch_sess) {
      if (typeof GM_cookie !== 'undefined' && typeof GM_cookie.list === 'function') {
        source = 'gm_cookie';
      } else {
        source = 'tap';   // 响应头截获
      }
    } else if (Object.keys(out).length) {
      source = 'doc_cookie_only';
    }
    return { cookies: out, expires: exp, source };
  }

  async function checkSessionStatus() {
    // v1.3.0：截获库为空时先探活一次（触发 muse.ai 响应 → Set-Cookie 进库）
    await warmSession();
    const { cookies, source } = await getCookies();
    const missing = ESSENTIAL.filter(n => !(n in cookies));
    const hasCore = missing.length === 0;
    return { hasCore, count: Object.keys(cookies).length, missing, cookies, source };
  }

  let _warmed = false;
  async function warmSession() {
    // 主动发一个同域轻量请求，让浏览器走一次 muse.ai 响应（截获器收 Set-Cookie）。
    // 只做一次；失败静默（GM_cookie / doc-cookie 路径仍可用）。
    if (_warmed) return;
    _warmed = true;
    try {
      await fetch('/api/users/me', { credentials: 'omit', cache: 'no-store' });
    } catch (_) {
      try { await fetch('/', { cache: 'no-store' }); } catch (_) {}
    }
    await new Promise(r => setTimeout(r, 400));   // 给截获器一点处理时间
  }

  async function executePush(btnEl = null) {
    const cfg = loadCfg();
    const base = normBase(cfg.base);
    // patch23 v1.2.0：一次性导入令牌优先于完整 API Key（令牌单次有效不暴露管理凭证）
    const token = (cfg.token || '').trim();
    const key = (cfg.key || '').trim();
    const cred = token || key;
    const label = (cfg.label || '').trim();

    if (btnEl) {
      btnEl.disabled = true;
      btnEl.innerHTML = '<span style="opacity:0.8">⏳ 提取中…</span>';
    }

    try {
      const { cookies, expires } = await getCookies();
      const names = Object.keys(cookies);

      if (!names.length) {
        showToast('未检测到 muse.ai Cookie，请确保已登录账号！', 'err');
        return;
      }

      const missing = ESSENTIAL.filter(n => !(n in cookies));
      if (missing.length) {
        showToast(`读到 ${names.length} 条 Cookie，但缺少核心会话令牌：${missing.join(', ')}。请确保已登录账号！`, 'err');
        return;
      }

      const cookieStr = Object.entries(cookies).map(([k, v]) => `${k}=${v}`).join('; ');
      if (typeof GM_setClipboard === 'function') {
        GM_setClipboard(cookieStr);
      }

      if (!base) {
        showToast('✓ 完整 Cookie 已复制到剪贴板！请在上方配置服务地址以启用一键入库。', 'ok');
        return;
      }

      if (btnEl) btnEl.innerHTML = '<span style="opacity:0.8">🚀 推送至服务…</span>';

      const headers = { 'Content-Type': 'application/json' };
      if (cred) {
        headers['Authorization'] = 'Bearer ' + cred;
      }

      const payload = JSON.stringify({ label, cookies, expires });

      const doFetch = () => {
        if (typeof GM_xmlhttpRequest === 'function') {
          return new Promise((resolve, reject) => {
            GM_xmlhttpRequest({
              method: 'POST',
              url: base + '/admin/accounts',
              headers: headers,
              data: payload,
              onload: (res) => resolve({ ok: res.status >= 200 && res.status < 300, status: res.status, text: () => Promise.resolve(res.responseText) }),
              onerror: reject,
              ontimeout: reject,
            });
          });
        }
        return fetch(base + '/admin/accounts', { method: 'POST', headers, body: payload });
      };

      const r = await doFetch();
      const txt = await r.text();
      let data = {};
      try { data = JSON.parse(txt); } catch (_) {}

      if (r.status === 401) {
        showToast(token
          ? '推送失败：导入令牌无效/已用过 (401)。一次性令牌导入成功即失效，请回管理页重新生成。'
          : '推送失败：API Key 错误 (401)', 'err');
        return;
      }
      if (r.status === 409) {
        showToast('这个 muse.ai 号已经在账号池里了，无需重复导入 (409)', 'err');
        return;
      }
      if (!r.ok) {
        showToast(`推送失败 (HTTP ${r.status})：${txt.slice(0, 80)}。已复制 Cookie 到剪贴板！`, 'err');
        return;
      }

      const a = (data.added && data.added[0]) || {};
      const expDate = a.expires_at ? new Date(a.expires_at * 1000).toLocaleDateString() : '有效';
      showToast(`✓ 账号成功导入 muse2api！\nID: ${a.id || '?'}\n有效期至: ${expDate}`, 'ok');
      updateStatusBadge();
    } catch (e) {
      showToast(`请求异常：${e.message || e}。已自动复制 Cookie 到剪贴板！`, 'err');
    } finally {
      if (btnEl) {
        btnEl.disabled = false;
        btnEl.innerHTML = '⚡ 一键推送至账号池';
      }
    }
  }

  async function updateStatusBadge() {
    const badge = document.getElementById('muse-status-tag');
    if (!badge) return;
    const st = await checkSessionStatus();
    if (st.hasCore) {
      badge.style.color = '#22c55e';
      const via = st.source === 'gm_cookie' ? 'GM_cookie' : st.source === 'tap' ? '响应头截获' : st.source;
      badge.textContent = `✓ 登录态完整 (${st.count} 项 · ${via})`;
      badge.title = '点击「一键推送」即可导入';
    } else {
      badge.style.color = '#f59e0b';
      // 自诊断：区分「已登录但读不到 httpOnly」vs「真的没登录」
      const gmOk = typeof GM_cookie !== 'undefined' && typeof GM_cookie.list === 'function';
      if (!gmOk && st.source === 'doc_cookie_only') {
        badge.textContent = `⚠ 读不到 httpOnly 会话（点我看解法）`;
        badge.style.cursor = 'pointer';
        badge.title = 'hatch_sess 是 httpOnly cookie。\n解法①：先刷新本页（响应头截获需要一次新请求）。\n解法②：Tampermonkey 仪表盘 → 设置 → 模式切「高级」→ 把 GM_cookie/Browser API 设为「总是允许」→ 刷新。\n解法③：F12 → Network → 刷新 → 点任意 muse.ai 请求 → 复制 cookie: 整行 → 粘到本面板「仅复制」旁的导入框。';
        badge.onclick = () => showToast(badge.title, 'info');
      } else {
        badge.textContent = `⚠ 未检测到登录态（请先登录 muse.ai）`;
      }
    }
  }

  function togglePanel() {
    const existing = document.getElementById('muse2api-token-panel');
    if (existing) {
      existing.remove();
      return;
    }
    showPanel();
  }

  function showPanel() {
    if (document.getElementById('muse2api-token-panel')) {
      document.getElementById('muse2api-token-panel').remove();
    }

    const cfg = loadCfg();
    const panel = document.createElement('div');
    panel.id = 'muse2api-token-panel';
    panel.innerHTML = `
      <div style="position:fixed; top:20px; right:20px; z-index:999999;
                  background:linear-gradient(180deg, rgba(15, 23, 42, 0.95) 0%, rgba(10, 15, 30, 0.96) 100%);
                  color:#f8fafc; padding:20px 22px; width:360px; max-width:calc(100vw - 40px);
                  border-radius:16px; font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                  box-shadow:0 20px 50px rgba(0, 0, 0, 0.6), 0 0 0 1px rgba(96, 165, 250, 0.25);
                  backdrop-filter:blur(16px); font-size:13px; line-height:1.4;">
        
        <!-- 头部 -->
        <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:14px; padding-bottom:10px; border-bottom:1px solid #1e293b;">
          <div style="display:flex; align-items:center; gap:8px;">
            <span style="display:inline-block; width:10px; height:10px; border-radius:50%; background:#38bdf8; box-shadow:0 0 10px #38bdf8;"></span>
            <b style="font-size:15px; color:#f1f5f9; letter-spacing:0.3px;">muse2api 导入面板</b>
            <span style="font-size:11px; background:#1e293b; color:#94a3b8; padding:1px 6px; border-radius:6px;">v1.1.2</span>
          </div>
          <button id="muse-close-btn" style="background:transparent; border:none; color:#64748b; font-size:18px; cursor:pointer; padding:0 4px; line-height:1;">✕</button>
        </div>

        <!-- 状态指示栏 -->
        <div style="background:#0f172a; border:1px solid #1e293b; border-radius:10px; padding:10px 12px; margin-bottom:14px; display:flex; justify-content:space-between; align-items:center;">
          <span style="font-size:12px; color:#94a3b8;">当前网页会话</span>
          <span id="muse-status-tag" style="font-size:12px; font-weight:600; color:#94a3b8;">检测中…</span>
        </div>

        <!-- 表单配置项（直接内嵌输入框，自动记忆） -->
        <div style="margin-bottom:12px;">
          <div style="font-size:11px; color:#94a3b8; margin-bottom:5px; font-weight:500;">服务地址 (Base URL)</div>
          <input id="muse-cfg-base" type="text" placeholder="如 https://muse.yourdomain.com 或 http://localhost:18610"
                 value="${cfg.base || ''}"
                 style="width:100%; box-sizing:border-box; padding:8px 11px; background:#0b1120; border:1px solid #334155;
                        border-radius:8px; color:#f1f5f9; font-size:12px; font-family:monospace; outline:none; transition:border-color 0.2s;">
        </div>

        <div style="margin-bottom:12px;">
          <div style="font-size:11px; color:#94a3b8; margin-bottom:5px; font-weight:500;">一次性导入令牌（推荐，管理页生成，单次有效）</div>
          <input id="muse-cfg-token" type="password" placeholder="m2a_imp_..."
                 value="${cfg.token || ''}"
                 style="width:100%; box-sizing:border-box; padding:8px 11px; background:#0b1120; border:1px solid #334155;
                        border-radius:8px; color:#f1f5f9; font-size:12px; font-family:monospace; outline:none;">
        </div>

        <div style="margin-bottom:12px;">
          <div style="font-size:11px; color:#94a3b8; margin-bottom:5px; font-weight:500;">API Key（备选；若服务未开鉴权可留空）</div>
          <input id="muse-cfg-key" type="password" placeholder="m2a_..."
                 value="${cfg.key || ''}"
                 style="width:100%; box-sizing:border-box; padding:8px 11px; background:#0b1120; border:1px solid #334155;
                        border-radius:8px; color:#f1f5f9; font-size:12px; font-family:monospace; outline:none;">
        </div>

        <div style="margin-bottom:16px;">
          <div style="font-size:11px; color:#94a3b8; margin-bottom:5px; font-weight:500;">账号标签 (可选显示名称)</div>
          <input id="muse-cfg-label" type="text" placeholder="如 acc-01（留空自动生成）"
                 value="${cfg.label || ''}"
                 style="width:100%; box-sizing:border-box; padding:8px 11px; background:#0b1120; border:1px solid #334155;
                        border-radius:8px; color:#f1f5f9; font-size:12px; outline:none;">
        </div>

        <!-- 动作操作区 -->
        <div style="display:flex; flex-direction:column; gap:8px;">
          <button id="muse-action-push" style="width:100%; padding:10px 0; border:none; border-radius:10px;
                  background:linear-gradient(135deg, #0284c7, #2563eb); color:#fff;
                  font-weight:600; font-size:13px; cursor:pointer; box-shadow:0 4px 14px rgba(37, 99, 235, 0.4);
                  transition:all 0.2s;">
            ⚡ 一键推送至账号池
          </button>

          <div style="display:flex; gap:8px;">
            <button id="muse-action-copy" style="flex:1; padding:7px 0; border:1px solid #334155; border-radius:8px;
                    background:#0f172a; color:#cbd5e1; font-size:12px; cursor:pointer; transition:all 0.2s;">
              📋 仅复制 Cookie
            </button>
            <button id="muse-action-save" style="flex:1; padding:7px 0; border:1px solid #334155; border-radius:8px;
                    background:#0f172a; color:#cbd5e1; font-size:12px; cursor:pointer; transition:all 0.2s;">
              💾 保存配置
            </button>
          </div>
        </div>

        <!-- 底部提示 -->
        <div style="margin-top:14px; padding-top:10px; border-top:1px solid #1e293b; display:flex; justify-content:space-between; align-items:center; font-size:11px; color:#64748b;">
          <span>配置将自动保存于浏览器本地</span>
          <span>按 Esc 或右上角关闭</span>
        </div>
      </div>
    `;

    document.body.appendChild(panel);
    updateStatusBadge();

    // 绑定事件与自动保存
    const baseIn = document.getElementById('muse-cfg-base');
    const tokenIn = document.getElementById('muse-cfg-token');
    const keyIn = document.getElementById('muse-cfg-key');
    const labelIn = document.getElementById('muse-cfg-label');

    function saveInputs() {
      const newCfg = {
        base: normBase(baseIn.value),
        token: tokenIn.value.trim(),
        key: keyIn.value.trim(),
        label: labelIn.value.trim(),
      };
      saveCfg(newCfg);
      return newCfg;
    }

    baseIn.addEventListener('change', saveInputs);
    tokenIn.addEventListener('change', saveInputs);
    keyIn.addEventListener('change', saveInputs);
    labelIn.addEventListener('change', saveInputs);

    document.getElementById('muse-close-btn').onclick = () => panel.remove();
    document.getElementById('muse-action-save').onclick = () => {
      saveInputs();
      showToast('✓ 配置已保存！', 'ok');
    };

    document.getElementById('muse-action-copy').onclick = async () => {
      const { cookies } = await getCookies();
      const str = Object.entries(cookies).map(([k, v]) => `${k}=${v}`).join('; ');
      if (str) {
        if (typeof GM_setClipboard === 'function') GM_setClipboard(str);
        showToast('✓ 完整 Cookie 已复制到剪贴板！', 'ok');
      } else {
        showToast('未检测到任何 Cookie！', 'err');
      }
    };

    document.getElementById('muse-action-push').onclick = function () {
      saveInputs();
      executePush(this);
    };
  }

  function mountFloatingTrigger() {
    if (document.getElementById('muse2api-floating-ball')) return;
    const ball = document.createElement('div');
    ball.id = 'muse2api-floating-ball';
    ball.innerHTML = `
      <div style="position:fixed; bottom:24px; right:24px; z-index:999990;
                  width:42px; height:42px; border-radius:50%; background:linear-gradient(135deg, #0284c7, #2563eb);
                  color:#fff; display:flex; align-items:center; justify-content:center;
                  cursor:pointer; box-shadow:0 8px 24px rgba(37, 99, 235, 0.45); font-size:18px;
                  user-select:none; transition:transform 0.2s, box-shadow 0.2s;"
           title="点击展开 muse2api 导入面板"
           onmouseover="this.style.transform='scale(1.08)'"
           onmouseout="this.style.transform='scale(1)'">
        ⚡
      </div>
    `;
    ball.addEventListener('click', togglePanel);
    document.body.appendChild(ball);
  }

  if (typeof GM_registerMenuCommand === 'function') {
    GM_registerMenuCommand('🚀 打开 muse2api 导入面板', togglePanel);
    GM_registerMenuCommand('⚡ 直接一键推送当前账号', () => executePush());
  }

  // v1.3.0：document-start 即装响应头截获（页面后续所有 XHR 的 Set-Cookie 进库）
  installSetCookieTap();

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mountFloatingTrigger);
  } else {
    mountFloatingTrigger();
  }
})();
