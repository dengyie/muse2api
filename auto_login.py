"""容器内隔离浏览器自动登录器（支持邮箱+密码、邮箱+OTP/Magic Link 及 TOTP 2FA）。

设计原则：
1. 隔离上下文：复用 engine.browser，但每次登录创建独立 BrowserContext，
   强制绑定账号原代理（HTTPProxyRelay）与原指纹（engine._apply_fingerprint），
   绝不回退共享上下文或直连。
2. 风控熔断：检测到 checkpoint / CAPTCHA / 未知人机挑战时立即中止，
   返回 needs_manual=True，由人工通过 /admin/remote-login 接管，严禁重试轰炸。
3. 零凭据泄漏：密码、TOTP、One-Mail JWT 与 hatch_sess 绝不写入日志或异常信息。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from cdp import CDP
from credentials import fetch_onemail_otp, generate_totp, unseal_secret
from proxy import HTTPProxyRelay

SITE = "https://muse.ai/"

_RISK_KEYWORDS = (
    "checkpoint",
    "verify you are human",
    "security check",
    "unusual activity",
    "account has been locked",
    "account suspended",
    "captcha",
    "recaptcha",
    "hcaptcha",
    "turnstile",
    "arkose",
)


@dataclass
class AutoLoginResult:
    ok: bool
    cookies: dict[str, str] = field(default_factory=dict)
    cookies_exp: dict[str, int] = field(default_factory=dict)
    error: str = ""
    needs_manual: bool = False
    stage: str = ""


def _allowed_muse_url(url: str) -> bool:
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme in ("http", "https") and (host == "muse.ai" or host.endswith(".muse.ai"))


def _click_point(page: CDP, x: float, y: float) -> None:
    page.send("Input.dispatchMouseEvent", {
        "type": "mouseMoved", "x": x, "y": y, "button": "none", "clickCount": 0,
    })
    page.send("Input.dispatchMouseEvent", {
        "type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1,
    })
    page.send("Input.dispatchMouseEvent", {
        "type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1,
    })


def _click_button_matching(page: CDP, patterns: list[str]) -> str | None:
    js = f"""(() => {{
        const pats = {json.dumps(patterns)};
        const vis = e => e.getClientRects().length > 0
            && getComputedStyle(e).visibility !== 'hidden'
            && !e.disabled;
        const btns = [...document.querySelectorAll('button,a,[role=button]')].filter(vis);
        for (const p of pats) {{
            const re = new RegExp(p, 'i');
            const hit = btns.find(e => re.test((e.innerText || e.getAttribute('aria-label') || '').trim()));
            if (hit) {{
                hit.scrollIntoView({{block: 'center'}});
                const r = hit.getBoundingClientRect();
                return JSON.stringify({{
                    x: Math.round(r.x + r.width / 2),
                    y: Math.round(r.y + r.height / 2),
                    text: (hit.innerText || hit.getAttribute('aria-label') || '').trim().slice(0, 60)
                }});
            }}
        }}
        return null;
    }})()"""
    raw = page.js(js)
    if not raw:
        return None
    info = json.loads(raw)
    _click_point(page, info["x"], info["y"])
    return info.get("text") or ""


def _fill_visible_input(page: CDP, selectors: list[str], value: str) -> bool:
    js = f"""(() => {{
        const sels = {json.dumps(selectors)};
        const vis = e => e.getClientRects().length > 0
            && getComputedStyle(e).visibility !== 'hidden'
            && !e.disabled && !e.readOnly;
        for (const s of sels) {{
            const el = [...document.querySelectorAll(s)].find(vis);
            if (el) {{
                el.scrollIntoView({{block: 'center'}});
                el.focus();
                if (el.value) el.select && el.select();
                const r = el.getBoundingClientRect();
                return JSON.stringify({{x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2)}});
            }}
        }}
        return null;
    }})()"""
    raw = page.js(js)
    if not raw:
        return False
    pt = json.loads(raw)
    _click_point(page, pt["x"], pt["y"])
    # Select all & clear existing value before inserting text
    page.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": "a", "code": "KeyA", "modifiers": 2})
    page.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": "a", "code": "KeyA", "modifiers": 2})
    page.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Backspace", "code": "Backspace"})
    page.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Backspace", "code": "Backspace"})
    page.send("Input.insertText", {"text": value})
    # Verify input received value (fallback to React nativeInputValueSetter if needed)
    filled_ok = page.js(f"""(() => {{
        const sels = {json.dumps(selectors)};
        const vis = e => e.getClientRects().length > 0
            && getComputedStyle(e).visibility !== 'hidden'
            && !e.disabled && !e.readOnly;
        for (const s of sels) {{
            const el = [...document.querySelectorAll(s)].find(vis);
            if (el) {{
                if ((el.value || '').length > 0) return true;
                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                if (setter) {{
                    setter.call(el, {json.dumps(value)});
                    el.dispatchEvent(new Event('input', {{bubbles: true}}));
                    el.dispatchEvent(new Event('change', {{bubbles: true}}));
                    return (el.value || '').length > 0;
                }}
            }}
        }}
        return false;
    }})()""")
    return bool(filled_ok)


def _submit_form(page: CDP) -> None:
    clicked = _click_button_matching(page, [
        r"^(continue|next|log\s*in|sign\s*in|verify|submit|confirm)$",
        r"continue|log\s*in|sign\s*in|verify|submit",
    ])
    if not clicked:
        page.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Enter", "code": "Enter"})
        page.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Enter", "code": "Enter"})


def _harvest_cookies(page: CDP, context_id: str) -> tuple[dict[str, str], dict[str, int]]:
    res = page.send("Storage.getCookies", {"browserContextId": context_id})
    cookies: dict[str, str] = {}
    cookies_exp: dict[str, int] = {}
    for c in (res.get("result") or {}).get("cookies", []):
        domain = (c.get("domain") or "").lstrip(".")
        if "muse.ai" not in domain:
            continue
        name = c.get("name") or ""
        val = c.get("value") or ""
        if not name or not val:
            continue
        cookies[name] = val
        try:
            exp = int(float(c.get("expires", -1)))
            if exp > 0:
                cookies_exp[name] = exp
        except (TypeError, ValueError):
            pass
    return cookies, cookies_exp


def _verify_session_http(cookies: dict[str, str], relay: HTTPProxyRelay | None) -> bool:
    if "hatch_sess" not in cookies:
        return False
    import requests
    header = "; ".join(f"{k}={v}" for k, v in cookies.items() if v)
    proxies = {"http": relay.url, "https": relay.url} if relay else None
    try:
        r = requests.get(
            SITE + "api/session",
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
                "Origin": "https://muse.ai",
                "Referer": "https://muse.ai/",
                "Cookie": header,
            },
            proxies=proxies,
            timeout=15,
            allow_redirects=False,
        )
        return r.status_code == 200
    except Exception:
        return False


def _detect_risk_gate(url: str, body_text: str, page: CDP) -> str | None:
    low_url = (url or "").lower()
    low_txt = (body_text or "").lower()
    for kw in _RISK_KEYWORDS:
        if kw in low_url or kw in low_txt:
            return kw
    iframe_hit = page.js("""(() => {
        const frames = [...document.querySelectorAll('iframe')];
        for (const f of frames) {
            const s = (f.src || '').toLowerCase();
            if (s.includes('recaptcha') || s.includes('hcaptcha')
                || s.includes('turnstile') || s.includes('arkose')) {
                return s.slice(0, 80);
            }
        }
        return null;
    })()""")
    return iframe_hit


def run_auto_login(engine, acc: dict, auth_profile: dict, cred_key: str,
                   timeout: int = 120, onemail_default_base: str = "https://mail-api.mangoqwq.com") -> AutoLoginResult:
    """在隔离 BrowserContext 中自动登录 muse.ai 并返回有效 Cookie。"""
    if not auth_profile:
        return AutoLoginResult(ok=False, error="账号未配置自动登录凭据", stage="init")
    identifier = (auth_profile.get("identifier") or "").strip()
    if not identifier:
        return AutoLoginResult(ok=False, error="自动登录凭据缺少邮箱/用户名", stage="init")

    auth_type = (auth_profile.get("auth_type") or "password").strip().lower()
    try:
        password = unseal_secret(auth_profile.get("password_enc") or "", cred_key)
        totp_secret = unseal_secret(auth_profile.get("totp_secret_enc") or "", cred_key)
        onemail_jwt = unseal_secret(auth_profile.get("onemail_jwt_enc") or "", cred_key)
    except ValueError as exc:
        return AutoLoginResult(ok=False, error=f"凭据解密失败: {exc}", stage="decrypt")

    onemail_base = (auth_profile.get("onemail_base") or onemail_default_base).strip()
    if auth_type == "password" and not password:
        return AutoLoginResult(ok=False, error="密码登录模式缺少密码", stage="init")
    if auth_type == "email_otp" and not onemail_jwt:
        return AutoLoginResult(ok=False, error="邮箱 OTP 模式缺少 One-Mail JWT", stage="init")

    engine.start()
    if not getattr(engine, "browser", None):
        return AutoLoginResult(ok=False, error="浏览器未就绪，无法执行自动登录", stage="browser")

    proxy = acc.get("proxy") or None
    relay: HTTPProxyRelay | None = None
    ctx_id: str | None = None
    page: CDP | None = None
    login_started_at = time.time()

    try:
        if proxy:
            relay = HTTPProxyRelay(proxy)
            res = engine.browser.send("Target.createBrowserContext", {
                "proxyServer": relay.url,
                "proxyBypassList": "<-loopback>",
            })
        else:
            res = engine.browser.send("Target.createBrowserContext")
        ctx_id = (res.get("result") or {}).get("browserContextId")
        if not ctx_id:
            return AutoLoginResult(ok=False, error="Chromium 未返回隔离上下文 ID", stage="context")

        t_res = engine.browser.send("Target.createTarget", {
            "url": "about:blank", "browserContextId": ctx_id,
        })
        target_id = (t_res.get("result") or {}).get("targetId")
        if not target_id:
            return AutoLoginResult(ok=False, error="Chromium 未返回页面 ID", stage="target")

        ws_url = f"ws://127.0.0.1:{engine.cfg.cdp_port}/devtools/page/{target_id}"
        page = CDP(ws_url, timeout=60)
        page.send("Network.enable")
        page.send("Page.enable")
        page.send("Runtime.enable")
        fp = acc.get("fingerprint")
        if fp:
            engine._apply_fingerprint(page, fp)

        page.send("Page.navigate", {"url": SITE})

        deadline = time.monotonic() + max(30, timeout)
        sign_in_clicked = False
        email_submitted = False
        password_submitted = False
        otp_requested = False
        otp_submitted = False
        totp_submitted = False

        while time.monotonic() < deadline:
            time.sleep(1.5)
            cookies, cookies_exp = _harvest_cookies(page, ctx_id)
            if "hatch_sess" in cookies and _verify_session_http(cookies, relay):
                return AutoLoginResult(
                    ok=True, cookies=cookies, cookies_exp=cookies_exp, stage="verified"
                )

            url = str(page.js("location.href") or "")
            body_text = str(page.js("(document.body && document.body.innerText) || ''") or "")
            if not body_text.strip() and url in ("about:blank", ""):
                continue

            if "ERR_TUNNEL_CONNECTION_FAILED" in body_text or "chrome-error://" in url:
                return AutoLoginResult(
                    ok=False, error="代理隧道连接失败，请检查账号出口代理", stage="network"
                )

            risk = _detect_risk_gate(url, body_text, page)
            if risk:
                return AutoLoginResult(
                    ok=False,
                    error=f"触发安全验证/人机挑战 ({risk[:40]})，已熔断等待人工处理",
                    needs_manual=True,
                    stage="risk_gate",
                )

            low_txt = body_text.lower()

            # 1. 首页入口：点击 Sign in / Log in / Use another account
            if not sign_in_clicked:
                hit = _click_button_matching(page, [
                    r"^(sign\s*in|log\s*in)$",
                    r"use another account|continue with email|sign\s*in|log\s*in",
                ])
                if hit:
                    sign_in_clicked = True
                    continue

            # 若出现账号选择器（AYMH）但还没点“使用其他账号”
            if email_submitted and not password_submitted and "use another account" in low_txt:
                _click_button_matching(page, [r"use another account"])
                continue

            # 2. TOTP 2FA 检测（必须在通用 OTP 之前判断）
            if (totp_secret and not totp_submitted
                    and any(k in low_txt for k in ("two-factor", "2fa", "authenticator", "authentication app", "two_fac"))):
                code = generate_totp(totp_secret)
                if _fill_visible_input(page, [
                    'input[autocomplete="one-time-code"]',
                    'input[inputmode="numeric"]',
                    'input[name*="code"]',
                    'input[type="text"]',
                ], code):
                    _submit_form(page)
                    totp_submitted = True
                    continue

            # 3. 邮箱 OTP / Magic Link 页面检测
            is_otp_screen = any(k in low_txt for k in (
                "enter the code", "verification code", "check your email",
                "we sent a code", "sent a login link", "one-time code", "6-digit",
            ))
            if is_otp_screen and not otp_submitted:
                if not onemail_jwt:
                    return AutoLoginResult(
                        ok=False,
                        error="Muse 要求邮箱验证码/确认，但未配置 One-Mail JWT",
                        needs_manual=True,
                        stage="otp_required",
                    )
                try:
                    code, magic_link = fetch_onemail_otp(
                        onemail_base, onemail_jwt, since_ts=login_started_at,
                        timeout=min(45.0, max(8.0, deadline - time.monotonic())),
                    )
                except ValueError as exc:
                    return AutoLoginResult(
                        ok=False, error=str(exc), needs_manual=True, stage="onemail_auth"
                    )
                if magic_link and _allowed_muse_url(magic_link):
                    page.send("Page.navigate", {"url": magic_link})
                    otp_submitted = True
                    continue
                if code:
                    if _fill_visible_input(page, [
                        'input[autocomplete="one-time-code"]',
                        'input[inputmode="numeric"]',
                        'input[name*="code"]',
                        'input[name*="otp"]',
                        'input[type="text"]',
                    ], code):
                        _submit_form(page)
                        otp_submitted = True
                        continue
                return AutoLoginResult(
                    ok=False, error="等待 One-Mail 邮箱验证码超时", stage="otp_timeout"
                )

            # 4. 邮箱 + 密码（兼容同屏单步表单与两步表单）
            has_pw = bool(page.js(
                "!![...document.querySelectorAll('input[type=password]')]"
                ".find(e => e.getClientRects().length > 0)"
            ))
            if not email_submitted:
                if auth_type == "email_otp":
                    clicked_email_btn = _click_button_matching(page, [r"continue with email|email me a code"])
                    if clicked_email_btn:
                        continue
                email_sels = [
                    'input[type="email"]',
                    'input[autocomplete="email"]',
                    'input[autocomplete="username"]',
                    'input[name*="email"]',
                    'input[name*="identifier"]',
                    'input[name*="user"]',
                    'input[type="text"]',
                ]
                if _fill_visible_input(page, email_sels, identifier):
                    email_submitted = True
                    # 若同屏已有密码框，先填密码再统一提交；否则先提交邮箱进入下一步
                    if not has_pw:
                        _submit_form(page)
                        continue

            if has_pw:
                if auth_type == "email_otp" and not otp_requested:
                    switched = _click_button_matching(page, [
                        r"email.*code|send.*code|one-time|use.*code|magic\s*link",
                    ])
                    if switched:
                        otp_requested = True
                        continue
                if password and not password_submitted:
                    if _fill_visible_input(page, ['input[type="password"]'], password):
                        _submit_form(page)
                        password_submitted = True
                        continue

            # 6. 错误提示检测（密码错误/账号不存在等，立即熔断不重试）
            if any(k in low_txt for k in (
                "incorrect password", "wrong password", "invalid credentials",
                "couldn't find your account", "invalid email", "too many requests",
            )):
                return AutoLoginResult(
                    ok=False,
                    error="登录被上游拒绝（密码或账号有误，或触发限流）",
                    needs_manual=True,
                    stage="rejected",
                )

        return AutoLoginResult(ok=False, error="自动登录超时，未获取到有效会话", stage="timeout")
    except Exception as exc:  # noqa: BLE001
        return AutoLoginResult(ok=False, error=f"自动登录异常: {str(exc)[:120]}", stage="error")
    finally:
        if page:
            try:
                page.close()
            except Exception:
                pass
        if ctx_id and getattr(engine, "browser", None):
            try:
                engine.browser.send("Target.disposeBrowserContext", {"browserContextId": ctx_id})
            except Exception:
                pass
        if relay:
            try:
                relay.close()
            except Exception:
                pass
