"""账号登录凭据加密、TOTP 计算与 One-Mail OTP 拉取（纯标准库 + requests）。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import struct
import time


_ENC_PREFIX = "v1:"


def _derive_keys(master_key: str) -> tuple[bytes, bytes]:
    raw = (master_key or "").encode("utf-8")
    if not raw:
        raise ValueError("MUSE2API_CREDENTIAL_KEY 未配置")
    enc_key = hmac.new(raw, b"muse2api-cred-enc-v1", hashlib.sha256).digest()
    mac_key = hmac.new(raw, b"muse2api-cred-mac-v1", hashlib.sha256).digest()
    return enc_key, mac_key


def _keystream(enc_key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(
            enc_key, nonce + struct.pack(">Q", counter), hashlib.sha256
        ).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def seal_secret(plaintext: str, master_key: str) -> str:
    """使用 SHA256-CTR + HMAC-SHA256 (Encrypt-then-MAC) 加密敏感凭据。"""
    if not plaintext:
        return ""
    enc_key, mac_key = _derive_keys(master_key)
    nonce = secrets.token_bytes(16)
    pt = plaintext.encode("utf-8")
    ks = _keystream(enc_key, nonce, len(pt))
    ct = bytes(a ^ b for a, b in zip(pt, ks))
    mac = hmac.new(mac_key, b"v1" + nonce + ct, hashlib.sha256).digest()
    return (
        _ENC_PREFIX
        + base64.urlsafe_b64encode(nonce).decode("ascii")
        + ":"
        + base64.urlsafe_b64encode(ct).decode("ascii")
        + ":"
        + base64.urlsafe_b64encode(mac).decode("ascii")
    )


def unseal_secret(token: str, master_key: str) -> str:
    """解密 seal_secret 产出的密文；校验 MAC 失败抛出 ValueError。"""
    if not token:
        return ""
    if not token.startswith(_ENC_PREFIX):
        raise ValueError("未知的凭据密文格式")
    parts = token[len(_ENC_PREFIX):].split(":")
    if len(parts) != 3:
        raise ValueError("凭据密文结构损坏")
    enc_key, mac_key = _derive_keys(master_key)
    nonce = base64.urlsafe_b64decode(parts[0].encode("ascii"))
    ct = base64.urlsafe_b64decode(parts[1].encode("ascii"))
    mac = base64.urlsafe_b64decode(parts[2].encode("ascii"))
    expected = hmac.new(mac_key, b"v1" + nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        raise ValueError("凭据密文校验失败（密钥不匹配或数据被篡改）")
    ks = _keystream(enc_key, nonce, len(ct))
    pt = bytes(a ^ b for a, b in zip(ct, ks))
    return pt.decode("utf-8")


def generate_totp(secret: str, ts: float | None = None, digits: int = 6,
                  period: int = 30) -> str:
    """RFC 6238 TOTP 生成（纯标准库，不依赖 pyotp）。"""
    cleaned = re.sub(r"[\s\-]+", "", (secret or "").strip()).upper()
    if not cleaned:
        raise ValueError("TOTP 密钥为空")
    pad = (-len(cleaned)) % 8
    if pad:
        cleaned += "=" * pad
    key = base64.b32decode(cleaned, casefold=True)
    counter = int((time.time() if ts is None else ts) // period)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code_int % (10 ** digits)).zfill(digits)


def mask_identifier(identifier: str) -> str:
    """邮箱/用户名脱敏展示（如 m***@example.com）。"""
    s = (identifier or "").strip()
    if not s:
        return ""
    if "@" in s:
        local, domain = s.split("@", 1)
        if len(local) <= 2:
            masked_local = local[0] + "***"
        else:
            masked_local = local[0] + "***" + local[-1]
        return f"{masked_local}@{domain}"
    if len(s) <= 3:
        return s[0] + "***"
    return s[:2] + "***" + s[-1]


def public_auth_profile(profile: dict | None) -> dict | None:
    """账号对外可见的自动登录摘要（绝不含任何密文或明文凭据）。"""
    if not profile:
        return None
    return {
        "configured": True,
        "auth_type": profile.get("auth_type") or "password",
        "identifier_masked": mask_identifier(profile.get("identifier") or ""),
        "has_password": bool(profile.get("password_enc")),
        "has_totp": bool(profile.get("totp_secret_enc")),
        "has_onemail": bool(profile.get("onemail_jwt_enc")),
        "onemail_base": profile.get("onemail_base") or "",
        "auto_relogin": bool(profile.get("auto_relogin", True)),
        "last_relogin_at": profile.get("last_relogin_at"),
        "last_relogin_ok": profile.get("last_relogin_ok"),
        "last_relogin_error": profile.get("last_relogin_error") or "",
        "needs_manual": bool(profile.get("needs_manual", False)),
    }


def _parse_mail_ts(raw: str | None) -> float | None:
    if not raw:
        return None
    s = str(raw).strip()
    if len(s) < 19:
        return None
    normalized = s[:19].replace("T", " ")
    try:
        import calendar
        tm = time.strptime(normalized, "%Y-%m-%d %H:%M:%S")
        return float(calendar.timegm(tm))
    except ValueError:
        return None


def _extract_muse_code_or_link(mail: dict) -> tuple[str | None, str | None]:
    """从单封 One-Mail 邮件中提取 Muse/Meta 验证码或 magic link。"""
    sender = f"{mail.get('source') or ''} {mail.get('sender') or ''}".lower()
    subject = str(mail.get("subject") or "")
    body = f"{mail.get('text') or ''}\n{mail.get('html') or ''}"
    combined = f"{sender}\n{subject}\n{body}".lower()
    if not any(k in combined for k in ("muse", "meta", "hatch", "verification", "login", "code", "sign in")):
        return None, None
    # 1. Magic link (strictly https://muse.ai/...)
    link_m = re.search(r"https://(?:[a-z0-9-]+\.)?muse\.ai/[^\s\"'<>]+", body, re.I)
    if link_m and any(k in link_m.group(0).lower() for k in ("auth", "login", "verify", "token", "magic", "callback")):
        return None, link_m.group(0)
    # 2. Numeric OTP (6-8 digits)
    clean_text = re.sub(r"<[^>]+>", " ", f"{subject}\n{mail.get('text') or ''}\n{mail.get('html') or ''}")
    for pat in (
        r"(?:code|otp|pin|verification|验证码|代码)[^\d]{0,24}(\d{6,8})\b",
        r"\b(\d{6,8})\b",
    ):
        m = re.search(pat, clean_text, re.I)
        if m:
            return m.group(1), None
    return None, None


def fetch_onemail_otp(base_url: str, jwt: str, since_ts: float,
                      timeout: float = 45.0, poll_interval: float = 3.0) -> tuple[str | None, str | None]:
    """轮询 One-Mail API 获取本次登录触发的新验证码或 magic link。

    返回 (otp_code, magic_link_url)。超时未收到返回 (None, None)。
    """
    import requests
    base = (base_url or "https://mail-api.mangoqwq.com").rstrip("/")
    headers = {"Authorization": f"Bearer {jwt.strip()}"}
    deadline = time.monotonic() + max(5.0, timeout)
    cutoff = since_ts - 15.0

    while time.monotonic() < deadline:
        # 1. 优先尝试 /api/verifcodes（One-Mail 内置验证码提取）
        try:
            r = requests.get(f"{base}/api/verifcodes?fresh=5&limit=10",
                             headers=headers, timeout=8)
            if r.status_code in (401, 403):
                raise ValueError(f"One-Mail JWT 无效或已过期 (HTTP {r.status_code})")
            if r.status_code == 200:
                results = (r.json() or {}).get("results") or []
                for item in results:
                    code = str(item.get("code") or "").strip()
                    src = str(item.get("from_addr") or item.get("sender") or "").lower()
                    rcv_ts = _parse_mail_ts(item.get("received_at") or item.get("created_at"))
                    if rcv_ts is not None and rcv_ts < cutoff:
                        continue
                    if code and code.isdigit() and 4 <= len(code) <= 8:
                        if not src or any(k in src for k in ("muse", "meta", "hatch", "noreply", "no-reply")):
                            return code, None
        except ValueError:
            raise
        except Exception:
            pass

        # 2. 兜底解析 /api/parsed_mails
        try:
            r = requests.get(f"{base}/api/parsed_mails?limit=10&offset=0",
                             headers=headers, timeout=8)
            if r.status_code in (401, 403):
                raise ValueError(f"One-Mail JWT 无效或已过期 (HTTP {r.status_code})")
            if r.status_code == 200:
                results = (r.json() or {}).get("results") or []
                for mail in results:
                    rcv_ts = _parse_mail_ts(mail.get("created_at"))
                    if rcv_ts is not None and rcv_ts < cutoff:
                        continue
                    code, link = _extract_muse_code_or_link(mail)
                    if code or link:
                        return code, link
        except ValueError:
            raise
        except Exception:
            pass

        time.sleep(min(poll_interval, max(0.2, deadline - time.monotonic())))
    return None, None
