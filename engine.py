"""Muse 生成引擎：用真实网页会话驱动 muse.ai 完成生图/生视频。

已验证流程：
  注入 cookie -> 打开 https://muse.ai/ -> 定位 textarea(placeholder=消息)
  -> Input.insertText 填入 -> 点「发送」
  -> 等待新的附件容器 [data-testid^=hatch-chat-attachment-presentation-]
  -> 从该容器内的 img/video 取 blob 字节 -> 落盘
"""
from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import http.client
import ipaddress
import mimetypes
import math
import urllib.error
import urllib.parse
import urllib.request
import json
import logging
import os
import re
import shutil
import struct
import socket
import subprocess
import tempfile
import threading
import time
import uuid
import unicodedata

from cdp import CDP, http_json
from proxy import HTTPProxyRelay, request_proxy_context

log = logging.getLogger("muse2api")

ATT_SEL = '[data-testid^="hatch-chat-attachment-presentation-"]'
# 参考素材上限对齐 muse.ai 网页端（maxFilesPerUpload=10 / maxFileSize=40 MiB /
# maxVideoDurationSeconds=120）。常量名沿用 *_IMAGES，但统计的是全部素材。
MAX_REFERENCE_IMAGES = 10
MAX_REFERENCE_IMAGE_BYTES = 40 * 1024 * 1024
MAX_REFERENCE_TOTAL_BYTES = 100 * 1024 * 1024
MAX_REFERENCE_VIDEO_SECONDS = 120
_MIB = 1024 * 1024

# 允许上传到 muse 输入框的素材类型 -> 落盘扩展名。
# 图片/视频/文档取自 muse 前端白名单；音频不在白名单里，但输入框接受，
# 且实测模型能读出 wav 的内容，所以一并放开。
REFERENCE_MIME_EXT = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
    "image/webp": ".webp", "image/heic": ".heic", "image/heif": ".heif",
    "image/avif": ".avif",
    "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm",
    "video/3gpp": ".3gp",
    "audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/mp4": ".m4a",
    "audio/ogg": ".ogg", "audio/flac": ".flac", "audio/aac": ".aac",
    "audio/opus": ".opus",
    "application/pdf": ".pdf", "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}
_MIME_ALIASES = {
    "image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg",
    "video/x-m4v": "video/mp4", "video/mov": "video/quicktime",
    "audio/mp3": "audio/mpeg", "audio/x-mp3": "audio/mpeg", "audio/mpeg3": "audio/mpeg",
    "audio/x-wav": "audio/wav", "audio/wave": "audio/wav", "audio/vnd.wave": "audio/wav",
    "audio/x-m4a": "audio/mp4", "audio/m4a": "audio/mp4", "audio/x-flac": "audio/flac",
    "audio/x-aac": "audio/aac", "audio/aacp": "audio/aac",
}
# 这些声明类型不可信，按文件头识别
_GENERIC_MIMES = {"", "application/octet-stream", "binary/octet-stream",
                  "application/binary", "application/unknown"}


def reference_kind(mime: str) -> str:
    """image / video / audio / document。"""
    head = (mime or "").split("/", 1)[0]
    return head if head in ("image", "video", "audio") else "document"


def _sniff_mime(data: bytes) -> str | None:
    """按文件头识别素材类型；识别不了返回 None。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand == b"qt  ":
            return "video/quicktime"
        if brand in (b"M4A ", b"M4B "):
            return "audio/mp4"
        if brand in (b"heic", b"heix", b"hevc", b"heim", b"heis"):
            return "image/heic"
        if brand in (b"mif1", b"msf1", b"heif"):
            return "image/heif"
        if brand in (b"avif", b"avis"):
            return "image/avif"
        if brand.startswith(b"3g"):
            return "video/3gpp"
        return "video/mp4"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    if data.startswith(b"ID3") or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg"
    if data[:2] in (b"\xff\xf1", b"\xff\xf9"):
        return "audio/aac"
    if data.startswith(b"OggS"):
        return "audio/opus" if b"OpusHead" in data[:64] else "audio/ogg"
    if data.startswith(b"fLaC"):
        return "audio/flac"
    if data.startswith(b"%PDF"):
        return "application/pdf"
    return None


def _mp4_duration(data: bytes) -> float | None:
    """读 mp4/mov 的 moov/mvhd 时长（秒）；解析不了返回 None。"""
    def boxes(start: int, end: int):
        pos = start
        while pos + 8 <= end:
            size = int.from_bytes(data[pos:pos + 4], "big")
            kind = data[pos + 4:pos + 8]
            header = 8
            if size == 1 and pos + 16 <= end:
                size = int.from_bytes(data[pos + 8:pos + 16], "big")
                header = 16
            elif size == 0:
                size = end - pos
            if size < header or pos + size > end:
                return
            yield kind, pos + header, pos + size
            pos += size

    for kind, body, end in boxes(0, len(data)):
        if kind != b"moov":
            continue
        for sub, sbody, send in boxes(body, end):
            if sub != b"mvhd" or send - sbody < 20:
                continue
            if data[sbody] == 1:
                if send - sbody < 32:
                    return None
                scale = int.from_bytes(data[sbody + 20:sbody + 24], "big")
                duration = int.from_bytes(data[sbody + 24:sbody + 32], "big")
            else:
                scale = int.from_bytes(data[sbody + 12:sbody + 16], "big")
                duration = int.from_bytes(data[sbody + 16:sbody + 20], "big")
            return duration / scale if scale else None
    return None


def _webm_duration(data: bytes) -> float | None:
    """Read EBML Segment/Info duration; unknown or malformed metadata fails closed."""
    def vint(pos, end, keep_marker=False):
        if pos >= end or not data[pos]:
            raise ValueError("invalid EBML integer")
        width = 1
        while width <= 8 and not (data[pos] & (0x80 >> (width - 1))):
            width += 1
        if width > 8 or pos + width > end:
            raise ValueError("truncated EBML integer")
        value = int.from_bytes(data[pos:pos + width], "big")
        if not keep_marker:
            value &= (1 << (7 * width)) - 1
            if value == (1 << (7 * width)) - 1:
                value = None
        return value, pos + width

    def elements(start, end):
        while start < end:
            tag, pos = vint(start, end, True)
            size, body = vint(pos, end)
            stop = end if size is None else body + size
            if stop > end or stop <= start:
                raise ValueError("invalid EBML element size")
            yield tag, body, stop
            start = stop

    try:
        for tag, body, end in elements(0, len(data)):
            if tag != 0x18538067:  # Segment
                continue
            for tag, body, end in elements(body, end):
                if tag != 0x1549A966:  # Info
                    continue
                scale, duration = 1000000, None
                for tag, body, end in elements(body, end):
                    if tag == 0x2AD7B1 and 0 < end - body <= 8:
                        scale = int.from_bytes(data[body:end], "big")
                    elif tag == 0x4489 and end - body in (4, 8):
                        duration = struct.unpack(">f" if end - body == 4 else ">d", data[body:end])[0]
                return duration * scale / 1e9 if duration is not None and scale else None
    except (ValueError, struct.error):
        return None
    return None


# 决定账号生死的核心 cookie（缺失或过期 = 会话失效）
# hatch_sess 是登录凭据；hatch_vml 可由工作区签发，缺少不阻止导入
ESSENTIAL_COOKIES = ("hatch_sess",)


class MuseAuthError(RuntimeError):
    pass


class MuseGenerationError(RuntimeError):
    """生成失败。.code 为机器可读错误码（OpenPet Agent 契约封闭集合）。

    patch22：可选 .stage（失败环节）与 .retryable（重试安全性）——
    stage < submit 的失败发生在消息提交前，重发不会重复计费；
    stage >= submit 的失败上游结果未知，safe_to_resubmit 必须为 False。
    """

    def __init__(self, msg: str, code: str = "generation_failed",
                 stage: str = "", retryable: bool | None = None):
        super().__init__(msg)
        self.code = code
        self.stage = stage
        self.retryable = retryable


# patch22：失败环节封闭集合（随错误一起落库，供 Agent 判定 safe_to_resubmit）。
# 顺序即流水线序：queued < page_prep < upload < submit < wait < extract。
SUBMIT_STAGE = "submit"


def _cb(cb, *args):
    """patch22：引擎侧回调安全执行（app._generation_callback 的引擎内等价物）。"""
    if cb:
        try:
            cb(*args)
        except Exception:
            pass


def _stage_before(a: str, b: str) -> bool:
    """a 是否发生在 b（提交）之前；未知 stage 按提交后处理（fail-safe）。"""
    order = ("queued", "page_prep", "upload", SUBMIT_STAGE, "wait", "extract")
    return a in order and b in order and order.index(a) < order.index(b)


class MuseProxyError(MuseGenerationError):
    """The account's configured proxy failed; this is not Cookie invalidation."""


class MuseSessionUnavailable(MuseGenerationError):
    """/api/session returned 200 without an assigned workspace.

    muse.ai may rotate hatch_sess / hatch_gw in that same response and revoke the old
    values, so the merged cookies are carried here and callers must keep using them.
    """

    def __init__(self, message: str, cookies: dict | None = None,
                 cookies_exp: dict | None = None, rotated: bool = False):
        super().__init__(message)
        self.cookies = cookies or {}
        self.cookies_exp = cookies_exp or {}
        self.rotated = rotated


class MuseModelRefusalError(MuseGenerationError):
    """Muse completed the turn with text instead of the requested media."""

    def __init__(self, msg: str, code: str = "generation_failed"):
        super().__init__(msg, code)


class RefImageSafetyError(MuseGenerationError):
    """A reference image source is forbidden by the configured input policy."""


class _RefImageHTTPHandler(urllib.request.HTTPHandler, urllib.request.HTTPSHandler):
    def __init__(self, address: str):
        super().__init__()
        self.address = address

    def _connection(self, host, connection_type, **kwargs):
        connection = connection_type(host, **kwargs)

        def create_connection(address, timeout, source_address):
            return socket.create_connection((self.address, address[1]), timeout, source_address)

        # Keep the original host for Host, TLS SNI and certificate validation;
        # only the socket destination is pinned to the checked address.
        connection._create_connection = create_connection
        return connection

    def http_open(self, req):
        return self.do_open(lambda host, **kw: self._connection(
            host, http.client.HTTPConnection, **kw), req)

    def https_open(self, req):
        return self.do_open(lambda host, **kw: self._connection(
            host, http.client.HTTPSConnection, **kw), req)


class _NoRefImageRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_public_ref_image(url: str):
    """Open one public HTTP(S) URL with DNS rebinding and redirect checks."""
    for _ in range(11):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise RefImageSafetyError("参考图重定向仅允许有效的 http(s) URL")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
        checked = []
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
                ip = ip.ipv4_mapped
            if not ip.is_global:
                raise RefImageSafetyError(
                    "参考图地址被拒绝：仅允许公网地址；如确需私网抓取，请显式设置 "
                    "MUSE2API_REF_IMAGE_PRIVATE_FETCH=1")
            checked.append(str(ip))
        if not checked:
            raise urllib.error.URLError("参考图域名未解析到地址")
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRefImageRedirect(),
            _RefImageHTTPHandler(checked[0]))
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            return opener.open(req, timeout=20)
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location")
            if exc.code not in (301, 302, 303, 307, 308) or not location:
                raise
            exc.close()
            url = urllib.parse.urljoin(url, location)
    raise urllib.error.URLError("参考图重定向次数过多")


# ---------------- patch17：指纹噪声注入（首文档前执行） ----------------
# 每账号一个 NoiseSeed（由 store 侧 seed 派生），canvas/audio/WebGL 读出值
# 做恒定微扰：同一账号内稳定、跨账号互异。同时统一 navigator 硬件参数。
_FINGERPRINT_NOISE_JS = r"""
(function() {
  try {
    var fp = window.__MUSE_FP__; if (!fp) {
      /* NoiseSeed 由 CDP evaluate 在注入本脚本后立即写入（见下）；
         若缺失则本脚本整体降级为 no-op。 */
      return;
    }
    var seed = 0;
    for (var i = 0; i < fp.noiseSeed.length; i++) {
      seed = (seed * 31 + fp.noiseSeed.charCodeAt(i)) >>> 0;
    }
    function rnd() { // mulberry32：确定性伪随机（同账号每次读值一致）
      seed |= 0; seed = (seed + 0x6D2B79F5) | 0;
      var t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    }
    // navigator 硬件参数
    try { Object.defineProperty(navigator, 'hardwareConcurrency',
      {get: function() { return fp.cores; }}); } catch (e) {}
    try { Object.defineProperty(navigator, 'deviceMemory',
      {get: function() { return fp.mem; }}); } catch (e) {}
    // patch17e（P3 保真）：每次读取前重置 mulberry32 种子——旧实现 rnd()
    // 跨调用推进，同一画布两次读值不同，「读值不一致」本身是可检测信号；
    // 现在同一账号每页内所有读值恒定（跨账号仍互异）。
    function resetSeed() {
      seed = 0;
      for (var i = 0; i < fp.noiseSeed.length; i++) {
        seed = (seed * 31 + fp.noiseSeed.charCodeAt(i)) >>> 0;
      }
    }
    // canvas：toDataURL / getImageData 读出时按种子微扰少量像素
    // （patch17e：补上 getImageData 覆盖——旧实现只包 toDataURL，与注释
    // 不符，canvas 指纹主流读法漏防）
    function perturbPixels(ctx, w, h) {
      var px = ctx.getImageData(0, 0, w, h);
      resetSeed();
      for (var i = 0; i < px.data.length; i += 97) {
        px.data[i] = px.data[i] ^ (rnd() < 0.5 ? 1 : 0);
      }
      ctx.putImageData(px, 0, 0);
    }
    var origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function() {
      try {
        var ctx = this.getContext('2d');
        if (ctx && this.width > 0 && this.height > 0) {
          perturbPixels(ctx, Math.min(this.width, 16), Math.min(this.height, 16));
        }
      } catch (e) {}
      return origToDataURL.apply(this, arguments);
    };
    var origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
    CanvasRenderingContext2D.prototype.getImageData = function(sx, sy, sw, sh) {
      var px = origGetImageData.apply(this, arguments);
      try {
        resetSeed();
        for (var i = 0; i < px.data.length; i += 97) {
          px.data[i] = px.data[i] ^ (rnd() < 0.5 ? 1 : 0);
        }
      } catch (e) {}
      return px;
    };
    // audio：AudioBuffer 通道数据恒定微扰
    if (window.AudioBuffer) {
      var origGetChannel = AudioBuffer.prototype.getChannelData;
      AudioBuffer.prototype.getChannelData = function() {
        var data = origGetChannel.apply(this, arguments);
        try {
          resetSeed();  // patch17e：恒定微扰（同账号每页读值一致）
          for (var i = 12; i < data.length; i += 500) {
            data[i] = data[i] + (rnd() - 0.5) * 1e-7;
          }
        } catch (e) {}
        return data;
      };
    }
    // WebGL：vendor/renderer 参数覆写（getContext 参数级，不动 GL 调用）
    var origGetContext = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function(type) {
      var ctx = origGetContext.apply(this, arguments);
      if (ctx && (type === 'webgl' || type === 'webgl2') && !ctx.__fpWrapped) {
        try {
          ctx.__fpWrapped = true;
          var origGetParam = ctx.getParameter.bind(ctx);
          ctx.getParameter = function(p) {
            if (p === 37445) return fp.glVendor;    // UNMASKED_VENDOR_WEBGL
            if (p === 37446) return fp.glRenderer;  // UNMASKED_RENDERER_WEBGL
            return origGetParam(p);
          };
        } catch (e) {}
      }
      return ctx;
    };
  } catch (e) {}
})();
"""


class MuseEngine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.proc: subprocess.Popen | None = None
        self.browser: CDP | None = None
        self._local = threading.local()
        self._start_lock = threading.Lock()
        self._active_contexts: set[str] = set()
        self._contexts_lock = threading.Lock()
        self._proxy_relays: set[HTTPProxyRelay] = set()
        self._download_dirs: set[str] = set()
        self.proxy_resolver = None
        # patch17：账号 → 指纹档案解析器（app 层注入；None = 不做指纹隔离）
        self.fingerprint_resolver = None
        # upstream 1.6.11：轮换 cookie 回写槽（app 层注入 _persist_session_cookies）
        self.cookie_sink = None
        self._last_http_renew: dict[str, float] = {}
        self._log = None
        os.makedirs(cfg.profile_dir, exist_ok=True)
        self._sweep_orphan_download_dirs()

    def _sweep_orphan_download_dirs(self):
        """patch25：启动时清扫上次运行遗留的孤儿下载目录。

        _download_dirs 是内存 set，容器非优雅退出（OOM/宿主重启）时来不及走
        stop() 清理，重启后集合清零、context-* 目录成永久孤儿（2026-11-16
        实证两个残留目录）。__init__ 时集合必为空，凡已存在的 context-*
        目录都是上一次进程的遗产，全部清掉；若目录里有未备份的媒体残留，
        mediabak 只扫 media_dir，不受影响。
        """
        try:
            d = getattr(self.cfg, "download_dir", None)
            if not d or not os.path.isdir(d):
                return
            removed = 0
            for name in os.listdir(d):
                if name.startswith("context-"):
                    shutil.rmtree(os.path.join(d, name), ignore_errors=True)
                    removed += 1
            if removed:
                logging.getLogger("muse2api.engine").info(
                    "patch25: 清扫上次运行遗留孤儿下载目录 %d 个", removed)
        except OSError:
            pass

    def _ensure_local(self):
        local = getattr(self, "_local", None)
        if local is None:
            local = threading.local()
            self._local = local
        return local

    @property
    def page(self) -> CDP | None:
        return getattr(self._ensure_local(), "page", None)

    @page.setter
    def page(self, val: CDP | None):
        self._ensure_local().page = val

    @property
    def current_acc_id(self) -> str | None:
        return getattr(self._ensure_local(), "current_acc_id", None)

    @current_acc_id.setter
    def current_acc_id(self, val: str | None):
        self._ensure_local().current_acc_id = val

    @property
    def current_context_id(self) -> str | None:
        return getattr(self._ensure_local(), "current_context_id", None)

    @current_context_id.setter
    def current_context_id(self, val: str | None):
        self._ensure_local().current_context_id = val

    # ---------------- 浏览器生命周期 ----------------
    def _debug_url(self):
        return f"http://127.0.0.1:{self.cfg.cdp_port}/json/version"

    def start(self):
        with self._start_lock:
            if self.proc and self.proc.poll() is None and self.browser:
                return
            env = dict(os.environ)
            env.setdefault("HOME", self.cfg.home_dir)
            env["PATH"] = (self.cfg.extra_path + os.pathsep + env.get("PATH", "")) if self.cfg.extra_path else env.get("PATH", "")
            args = [
                self.cfg.chromium,
                "--headless=new", "--no-sandbox", "--disable-gpu",
                "--disable-dev-shm-usage", "--disable-background-networking",
                "--no-first-run", "--no-default-browser-check",
                "--autoplay-policy=no-user-gesture-required",
                "--window-size=1440,2400",
                f"--remote-debugging-port={self.cfg.cdp_port}",
                "--remote-allow-origins=*",
                f"--user-data-dir={self.cfg.profile_dir}",
                "about:blank",
            ]
            # 尝试复用已有健康 CDP
            if not self.proc:
                try:
                    v = http_json(self._debug_url(), timeout=1)
                    if v and "webSocketDebuggerUrl" in v:
                        self.browser = CDP(v["webSocketDebuggerUrl"], timeout=180)
                        return
                except Exception:
                    pass

            # 清理残留锁
            for lock_name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
                lp = os.path.join(self.cfg.profile_dir, lock_name)
                if os.path.exists(lp) or os.path.islink(lp):
                    try:
                        os.unlink(lp)
                    except Exception:
                        pass

            os.makedirs(self.cfg.data_dir, exist_ok=True)
            self._log = open(os.path.join(self.cfg.data_dir, "chromium.log"), "ab", buffering=0)
            cwd_dir = self.cfg.home_dir if (self.cfg.home_dir and os.path.isdir(self.cfg.home_dir)) else None
            self.proc = subprocess.Popen(args, stdout=self._log, stderr=subprocess.STDOUT,
                                         env=env, cwd=cwd_dir)
            last = None
            for _ in range(90):
                try:
                    v = http_json(self._debug_url(), timeout=2)
                    self.browser = CDP(v["webSocketDebuggerUrl"], timeout=180)
                    return
                except Exception as exc:  # noqa: BLE001
                    last = exc
                    time.sleep(1)
            raise MuseGenerationError(f"Chromium 启动失败: {last}")

    def stop(self):
        with self._start_lock:
            self._close_page()
            if self.browser:
                with self._contexts_lock:
                    for ctx_id in list(self._active_contexts):
                        try:
                            self.browser.send("Target.disposeBrowserContext", {"browserContextId": ctx_id})
                        except Exception:
                            pass
                    self._active_contexts.clear()
                try:
                    self.browser.close()
                except Exception:
                    pass
                self.browser = None
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=10)
                except Exception:  # noqa: BLE001
                    self.proc.kill()
            self.proc = None
            with self._contexts_lock:
                relays = list(self._proxy_relays)
                self._proxy_relays.clear()
            for relay in relays:
                relay.close()
            with self._contexts_lock:
                download_dirs = list(self._download_dirs)
                self._download_dirs.clear()
            for directory in download_dirs:
                shutil.rmtree(directory, ignore_errors=True)

    # ---------------- 页面 ----------------
    @property
    def download_dir(self) -> str:
        return getattr(self._ensure_local(), "download_dir", None) or self.cfg.download_dir

    def _new_download_dir(self) -> str:
        os.makedirs(self.cfg.download_dir, exist_ok=True)
        directory = tempfile.mkdtemp(prefix="context-", dir=self.cfg.download_dir)
        self._ensure_local().download_dir = directory
        with self._contexts_lock:
            self._download_dirs.add(directory)
        return directory

    def _close_page(self):
        """关闭当前线程专属的页面并销毁隔离的 BrowserContext，彻底释放内存与 Session。"""
        p = self.page
        ctx_id = self.current_context_id
        if p:
            try:
                p.close()
            except Exception:
                pass
            self.page = None
        if ctx_id and self.browser:
            try:
                self.browser.send("Target.disposeBrowserContext", {"browserContextId": ctx_id})
            except Exception:
                pass
            with self._contexts_lock:
                self._active_contexts.discard(ctx_id)
            self.current_context_id = None
        self.current_acc_id = None
        local = self._ensure_local()
        relay = getattr(local, "proxy_relay", None)
        if relay:
            relay.close()
            with self._contexts_lock:
                self._proxy_relays.discard(relay)
            local.proxy_relay = None
        local.proxy_config = None
        directory = getattr(local, "download_dir", None)
        if directory:
            shutil.rmtree(directory, ignore_errors=True)
            with self._contexts_lock:
                self._download_dirs.discard(directory)
            local.download_dir = None

    # ---------------- patch17：每账号指纹应用 ----------------
    @staticmethod
    def _apply_fingerprint(page, fp: dict):
        """对刚创建的独立 context 页面应用账号专属设备身份。

        全部走 CDP Emulation 域 + 首文档前注入，指纹在页面任何脚本运行前
        生效。失败只降级为「该 context 用浏览器默认指纹」，不阻断生成。
        """
        if not fp:
            return
        # patch22i 防御层：muse.ai composer 在非英语 Accept-Language 下附件
        # 处理树不挂载（容器内对照：de-DE → attach 0/1 form_len=0；en-US → OK）。
        # 语言维度强制英语系——存量非英语档案（de-DE 等）即时生效，无需改库；
        # 时区/屏幕/硬件等其它指纹维度不受影响。
        _lang = str(fp.get("accept_language") or "")
        if not _lang.lower().startswith("en"):
            fp = dict(fp, accept_language="en-US",
                      locale=str(fp.get("locale") or "") if str(
                          fp.get("locale") or "").lower().startswith("en")
                      else "en-US")
        try:
            # 完整 client hints（高熵位），Chrome 130-135 报法
            platform = fp.get("platform") or "Windows"
            ua = fp.get("ua")
            brands = [{"brand": "Not A(Brand", "version": "99"},
                      {"brand": "Google Chrome", "version": str(fp.get("chrome_major") or 135)},
                      {"brand": "Chromium", "version": str(fp.get("chrome_major") or 135)}]
            # patch17e（P3 保真）：userAgentMetadata.platform 真实报法是
            # 首字母大写枚举（"Windows"/"macOS"/"Linux"），.upper() 产生的
            # "WINDOWS" 与低熵 UA 互斥，属可检测不一致。
            cmaj = fp.get('chrome_major') or 135
            full = {
                "brands": brands,
                # patch17e（P3 保真）：fullVersion 带真实 patch/构建段，
                # 恒 ".0.0.0" 同为可检测特征。
                "fullVersion": f"{cmaj}.0.{6600 + cmaj % 80}.{40 + cmaj % 30}",
                "platform": platform,
                "platformVersion": fp.get("ua_platform_version") or "15.0.0",
                "architecture": "arm" if platform == "macOS" else "x86",
                "bitness": "64",
                "model": "",
                "mobile": False,
                "wow64": False,
            }
            # navigator.platform 真实报法（patch17e P3 保真）
            nav_platform = {"Windows": "Win32", "macOS": "MacIntel",
                            "Linux": "Linux x86_64"}.get(platform, "Win32")
            page.send("Emulation.setUserAgentOverride", {
                "userAgent": ua,
                "acceptLanguage": fp.get("accept_language") or "en-US",
                "platform": nav_platform,
                "userAgentMetadata": full,
            })
            try:
                page.send("Emulation.setLocaleOverride", {"locale": fp.get("locale") or "en-US"})
            except Exception:
                pass  # 老版本 chromium 无此域时忽略
            try:
                page.send("Emulation.setTimezoneOverride", {"timezoneId": fp.get("timezone") or "UTC"})
            except Exception:
                pass  # 时区非法（如上游规则更新）只降级，不阻断（patch17b P3 删死语句）
        except ValueError:
            pass
        except Exception:
            pass
        try:
            scr = fp.get("screen") or {}
            page.send("Emulation.setDeviceMetricsOverride", {
                "width": int(scr.get("width") or 1440),
                "height": int(scr.get("height") or 900),
                "deviceScaleFactor": float(fp.get("device_scale_factor") or 1),
                "mobile": False,
            })
        except Exception:
            pass
        try:
            # patch17b（P1 根修）：__MUSE_FP__ 此前从未被赋值，噪声脚本永远
            # 提前 return（canvas/audio/WebGL/navigator 隔离全部无效）。
            # 修复 = 注入时把档案值内联进脚本首部，单次 addScript 生效。
            fp_js = json.dumps({
                "noiseSeed": fp.get("noise") or "",
                "cores": fp.get("hardware_concurrency") or 8,
                "mem": fp.get("device_memory") or 8,
                "glVendor": fp.get("webgl_vendor") or "",
                "glRenderer": fp.get("webgl_renderer") or "",
            }, ensure_ascii=False)
            page.send("Page.addScriptToEvaluateOnNewDocument",
                      {"source": f"window.__MUSE_FP__ = {fp_js};\n"
                                 + _FINGERPRINT_NOISE_JS})
        except Exception:
            pass

    def _open_page(self, proxy: dict | None = None, fingerprint: dict | None = None):
        """Create an isolated account context; never fall back to shared cookies."""
        if not self.browser:
            error = MuseProxyError if proxy else MuseGenerationError
            raise error("浏览器未连接，无法创建独立账号会话；未回退共享标签页")
        page = None
        try:
            if proxy:
                relay = HTTPProxyRelay(proxy)
                self._ensure_local().proxy_relay = relay
                with self._contexts_lock:
                    self._proxy_relays.add(relay)
                res = self.browser.send("Target.createBrowserContext", {
                    "proxyServer": relay.url,
                    "proxyBypassList": "<-loopback>",
                })
            else:
                res = self.browser.send("Target.createBrowserContext")
            ctx_id = res.get("result", {}).get("browserContextId")
            if not ctx_id:
                raise MuseGenerationError("Chromium 未返回独立上下文 ID")
            self.current_context_id = ctx_id
            with self._contexts_lock:
                self._active_contexts.add(ctx_id)
            t_res = self.browser.send("Target.createTarget", {
                "url": "about:blank", "browserContextId": ctx_id,
            })
            target_id = t_res.get("result", {}).get("targetId")
            if not target_id:
                raise MuseGenerationError("Chromium 未返回独立页面 ID")
            ws_url = f"ws://127.0.0.1:{self.cfg.cdp_port}/devtools/page/{target_id}"
            page = CDP(ws_url, timeout=180)
            page.send("Network.enable")
            page.send("Page.enable")
            page.send("Runtime.enable")
            page.send("Browser.setDownloadBehavior", {
                "behavior": "allow", "downloadPath": self._new_download_dir(),
                "browserContextId": ctx_id,
            })
            # patch17：应用账号专属设备身份（Emulation + 首文档前注入）。
            # 失败只降级为默认指纹，绝不因指纹问题阻断生成链路。
            if fingerprint:
                try:
                    self._apply_fingerprint(page, fingerprint)
                except Exception:
                    pass
            return page
        except Exception:
            if page:
                try:
                    page.close()
                except Exception:
                    pass
            self._close_page()
            error = MuseProxyError if proxy else MuseGenerationError
            raise error("无法创建该账号的独立浏览器会话；未回退共享标签页或直连") from None

    @staticmethod
    def renew_session_http(cookies: dict, expires: dict | None = None,
                           wake_vm: bool = True, proxy: dict | None = None) -> dict:
        """直接调用 muse.ai/api/session 续签 hatch_vml (+48h) / hatch_sess (+30d) / hatch_gw (+1y)，
        并按需调用 /api/hatch/vm/wake 唤醒云端工作区 VM。"""
        import requests
        with request_proxy_context(proxy) as proxy_options:
            cur_cookies = dict(cookies or {})
            cur_exp = dict(expires or {})
            headers = {
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
                "Origin": "https://muse.ai",
                "Referer": "https://muse.ai/thread/new",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Dest": "empty",
                "Cookie": "; ".join(f"{k}={v}" for k, v in cur_cookies.items() if v),
            }
            try:
                r = requests.get("https://muse.ai/api/session", headers=headers,
                                 timeout=12, allow_redirects=False, **proxy_options)
            except requests.RequestException as exc:
                # 不回显请求内容：异常可能包含带凭据的代理 URL。
                if proxy:
                    raise MuseProxyError(
                        f"账号代理请求失败 ({type(exc).__name__})；请检查代理连接或认证，未回退直连") from None
                raise MuseGenerationError(
                    f"/api/session 网络请求失败 ({type(exc).__name__})；请检查服务器网络/代理后重试") from None
            if proxy and r.status_code == 407:
                raise MuseProxyError("账号代理认证失败 (HTTP 407)，请检查代理用户名和密码")
            if r.status_code == 401:
                raise MuseAuthError("会话认证失败 (/api/session HTTP 401)，请在官网确认登录后重新导入 cookie")
            if r.status_code != 200:
                hint = ("访问被拒绝，请检查服务器出口/地区/访问限制；不能据此判定 Cookie 失效"
                        if r.status_code == 403 else "上游请求未成功，请稍后重试并检查服务器网络")
                raise MuseGenerationError(f"/api/session HTTP {r.status_code}：{hint}")
            try:
                sj = r.json()
            except ValueError:
                raise MuseGenerationError("/api/session HTTP 200 返回非 JSON；会话状态未确认") from None
            if not isinstance(sj, dict):
                raise MuseGenerationError("/api/session HTTP 200 未返回 assigned 会话；请在官网检查账号/工作区状态")
            # A genuine session answer may rotate credentials even when no workspace is
            # assigned. Harvest Set-Cookie before evaluating the workspace status.
            rotated = False
            for c in r.cookies:
                if c.value:
                    if cur_cookies.get(c.name) != c.value:
                        rotated = True
                    cur_cookies[c.name] = c.value
                if c.expires:
                    cur_exp[c.name] = int(float(c.expires))
            if sj.get("status") != "assigned":
                status = sj.get("status")
                raise MuseSessionUnavailable(
                    f"/api/session HTTP 200 未返回 assigned 会话（status={status}）；"
                    f"工作区暂未分配，已保留 muse.ai 轮换下发的新 cookie",
                    cur_cookies, cur_exp, rotated)
            vm_id = sj.get("vm_id")
            vm_state = sj.get("vm_state")
            wake_ok = False
            wake_requested = False
            if wake_vm and vm_id and vm_state != "DISABLED":
                try:
                    headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cur_cookies.items() if v)
                    rw = requests.post("https://muse.ai/api/hatch/vm/wake", headers=headers, json={
                        "vm_id": vm_id,
                        "retry_count": 0,
                        "connect_attempt_id": str(uuid.uuid4()),
                    }, timeout=8, allow_redirects=False, **proxy_options)
                    if proxy and rw.status_code == 407:
                        raise MuseProxyError("账号代理认证失败 (HTTP 407)，未完成 VM 唤醒")
                    wake_requested = rw.status_code == 200
                    for c in rw.cookies:
                        if c.value:
                            cur_cookies[c.name] = c.value
                        if c.expires:
                            cur_exp[c.name] = int(float(c.expires))
                    headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cur_cookies.items() if v)
                    if wake_requested:
                        # HTTP 200 acknowledges the request; only session state confirms the VM woke.
                        verify = requests.get("https://muse.ai/api/session", headers=headers,
                                              timeout=12, allow_redirects=False, **proxy_options)
                        if proxy and verify.status_code == 407:
                            raise MuseProxyError("账号代理认证失败 (HTTP 407)，未完成 VM 状态确认")
                        for c in verify.cookies:
                            if c.value:
                                cur_cookies[c.name] = c.value
                            if c.expires:
                                cur_exp[c.name] = int(float(c.expires))
                        if verify.status_code == 200:
                            verified_session = verify.json()
                            if isinstance(verified_session, dict) and verified_session.get("status") == "assigned":
                                vm_state = verified_session.get("vm_state") or vm_state
                                wake_ok = vm_state == "RUNNING"
                except MuseProxyError:
                    raise
                except requests.RequestException as exc:
                    if proxy:
                        raise MuseProxyError(
                            f"经账号代理唤醒或确认 VM 失败 ({type(exc).__name__})；未回退直连") from None
                except Exception:
                    pass
            return {
                "ok": sj.get("status") == "assigned",
                "status": sj.get("status"),
                "vm_id": vm_id,
                "vm_state": vm_state,
                "wake_ok": wake_ok,
                "wake_requested": wake_requested,
                "cookies": cur_cookies,
                "cookies_exp": cur_exp,
            }

    def _apply_cookies(self, page: CDP, cookies: dict, expires: dict | None = None):
        """注入 cookie。彻底清空旧账号 cookie 保证隔离，且绝不传入过去时间的 expires 防止 Chromium 丢弃 hatch_vml。"""
        try:
            page.send("Network.clearBrowserCookies")
        except Exception:
            pass
        now = time.time()
        for name, value in cookies.items():
            if not value:
                continue
            exp = (expires or {}).get(name)
            try:
                exp_val = float(exp) if exp and float(exp) > now + 3600 else (now + 7 * 86400)
            except (TypeError, ValueError):
                exp_val = now + 7 * 86400
            for dom in (".muse.ai", "muse.ai"):
                params = {
                    "name": name,
                    "value": value,
                    "domain": dom,
                    "path": "/",
                    "secure": True,
                    "expires": exp_val,
                }
                try:
                    page.send("Network.setCookie", params)
                except Exception:  # noqa: BLE001
                    pass

    def read_cookies(self) -> dict[str, dict]:
        """从当前页面读回 cookie（**包含 httpOnly**，这是网页 JS 做不到的）。

        返回 {name: {"value":..., "expires": unix秒 或 -1}}。
        用途：muse.ai 在访问时会续期部分 cookie，生成完读回来写进账号池，
        账号就不容易过期。
        """
        if not self.page:
            return {}
        try:
            msg = self.page.send("Network.getCookies",
                                 {"urls": [self.cfg.site_url]}, timeout=20)
        except Exception:  # noqa: BLE001
            return {}
        out: dict[str, dict] = {}
        for c in (msg.get("result", {}).get("cookies") or []):
            name = c.get("name")
            if not name:
                continue
            try:
                exp = int(float(c.get("expires", -1)))
            except (TypeError, ValueError):
                exp = -1
            out[name] = {"value": c.get("value", ""), "expires": exp}
        return out

    def _wait_ws_ready(self, page: CDP, timeout: float = 15.0) -> bool:
        """等待 muse.ai 页面完成 React hydration 且不再处于 Connecting... 状态。

        patch22e：readyState 接受 interactive——线上实证（tencent-lh WARP 出口，
        2026-11-17 probe）主文档可长期停在 interactive（慢子资源拖住 load 事件），
        而 textarea 已渲染、hydration=hydrated、无 Connecting...，页面功能完全
        就绪；坚持 'complete' 会把可用页面误判为超时（page_prep_timeout 假阳性，
        即 OpenPet 两笔历史失败的直接根因之一）。可交互判定以 textarea+hydration
        +无 Connecting... 为准，比文档加载事件更贴近真实可用性。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                st = page.js("""(function(){
                    var rs = document.readyState;
                    if (rs !== 'complete' && rs !== 'interactive') return 'loading';
                    if (!document.querySelector('textarea')) return 'no-ta';
                    var h = document.querySelector('[data-hatch-shell-hydration-state]');
                    // patch22h：fail-closed——标记元素不存在 = React 尚未挂载
                    // hydration 状态节点，事件委托同样未就绪（线上实证半加载页
                    // 此处 h 为 null 被旧逻辑静默放过 → setFileInputFiles 的
                    // change 事件无人处理 → 附件永不渲染）。必须有标记且已 hydrated。
                    if (!h) return 'hydrating';
                    if (h.getAttribute('data-hatch-shell-hydration-state') !== 'hydrated') return 'hydrating';
                    var b = document.body ? (document.body.innerText || '') : '';
                    if (b.indexOf('Connecting...') !== -1) return 'connecting';
                    // Empty composers may not render Send until text has been entered.
                    if (document.querySelector('textarea').disabled) return 'input-disabled';
                    return 'ready';
                })()""")
                if st == "ready":
                    return True
            except Exception:
                pass
            time.sleep(0.1)
        return False

    def reset_thread(self, for_chat: bool = False):
        """关闭残留弹窗并确保处于干净会话且 WebSocket 已就绪。
        对于纯文本对话（for_chat=True），若当前热页面无附件、无卡死且气泡数较少，直接复用现有热连接以实现 2s 级秒回。"""
        if not self.page:
            return
        try:
            needs_nav = self.page.js("""(function(forChat){
                var d = document.querySelector('[role="dialog"]');
                if (d) {
                    var b = d.querySelector('button[aria-label*="close" i], button');
                    if (b) b.click();
                }
                var scope = document.querySelector('main,[class*="chat-scroll"],[class*="hatch-chat-scroll"]') || document.body;
                var bubbleCount = scope ? scope.querySelectorAll('div[class*="hatch-chat-groupable-bubble"]').length : 0;
                var hasAtts = document.querySelectorAll('[data-testid^="hatch-chat-attachment-presentation-"]').length > 0;
                var hasStop = !!(document.querySelector('[data-testid="hatch-composer-stop-button"]')
                    || document.querySelector('button[aria-label*="Stop" i]')
                    || document.querySelector('button[aria-label*="停止"]'));
                var bodyTxt = document.body ? (document.body.innerText || '') : '';
                var hasStuck = bodyTxt.indexOf('Still sending') !== -1 || bodyTxt.indexOf('Connecting...') !== -1;
                if (hasStop || hasStuck || hasAtts) return true;
                if (forChat) {
                    return bubbleCount >= 24;
                }
                return (window.location.pathname !== '/thread/new') || bubbleCount > 0;
            })(%s)""" % ("true" if for_chat else "false"))
            if needs_nav:
                self.page.send("Page.navigate", {"url": "https://muse.ai/thread/new"})
                t_end = time.time() + 10.0
                while time.time() < t_end:
                    time.sleep(0.08)
                    ready = self.page.js("""(function(){
                        return document.readyState === 'complete'
                            && !!document.querySelector('textarea')
                            && document.querySelectorAll('div[class*="hatch-chat-groupable-bubble"]').length === 0;
                    })()""")
                    if ready:
                        break
                self._wait_ws_ready(self.page, timeout=12.0)
        except Exception:
            pass

    def _report_rotated_cookies(self, account_id, cookies: dict, expires: dict | None):
        sink = self.cookie_sink
        if not (sink and account_id and cookies):
            return
        try:
            sink(account_id, dict(cookies), dict(expires or {}))
        except Exception as e:  # noqa: BLE001
            log.warning("保存轮换后的 cookie 失败 %s: %s", account_id, e)

    def ensure_page(self, cookies: dict, expires: dict | None = None, account_id: str | None = None):
        resolver = getattr(self, "proxy_resolver", None)
        local = self._ensure_local()
        # A lease snapshots routing; a concurrent config edit must never change its exit.
        proxy = copy.deepcopy(local.proxy_snapshot if hasattr(local, "proxy_snapshot")
                              else resolver(account_id) if resolver and account_id else None)
        # patch17：账号指纹档案（缺档案时 resolver 端惰性生成；None = 不隔离）
        fingerprint = None
        fp_resolver = getattr(self, "fingerprint_resolver", None)
        if fp_resolver and account_id:
            try:
                fingerprint = fp_resolver(account_id)
            except Exception:
                fingerprint = None
        # patch17e（P2）：复用键加入本次应用的指纹——否则 refingerprint
        # 后旧页面带旧设备身份被一直复用，换身份端点承诺落空。
        if (self.page is not None
                and getattr(self._ensure_local(), "proxy_config", None) == proxy
                and (account_id is None or getattr(self, "current_acc_id", None) == account_id)
                and getattr(self._ensure_local(), "current_fp", None) == fingerprint):
            try:
                if self.page.js("!!document.querySelector('textarea')") and self._wait_ws_ready(self.page, timeout=8.0):
                    return self.page
            except Exception:
                pass
        self._close_page()
        # 仅当距离上次 HTTP 续签超过 10 分钟时才在主链路调用 /api/session，避免每次切号重复阻塞
        last_map = getattr(self, "_last_http_renew", None)
        if last_map is None:
            last_map = {}
            self._last_http_renew = last_map
        now_ts = time.time()
        if not account_id or (now_ts - last_map.get(account_id, 0) > 600):
            try:
                renewed = self.renew_session_http(cookies, expires, wake_vm=True,
                                                  **({"proxy": proxy} if proxy else {}))
                if renewed.get("cookies"):
                    cookies = renewed["cookies"]
                if renewed.get("cookies_exp"):
                    expires = renewed["cookies_exp"]
                if account_id:
                    last_map[account_id] = now_ts
            except (MuseAuthError, MuseProxyError):
                raise
            except MuseSessionUnavailable as e:
                if e.cookies:
                    cookies = e.cookies
                    expires = e.cookies_exp or expires
                    if e.rotated:
                        self._report_rotated_cookies(account_id, cookies, expires)
                if account_id:
                    last_map[account_id] = now_ts
                log.warning("预续签 /api/session 未分配工作区（继续用%s cookie 加载页面）: %s",
                            "轮换后的新" if e.rotated else "现有", e)
            except Exception as e:
                log.warning("预续签 /api/session 失败（继续尝试浏览器加载）: %s", e)

        page = (self._open_page(proxy=proxy, fingerprint=fingerprint) if proxy
                else self._open_page(fingerprint=fingerprint))
        self.page = page  # Cleanup must also own pages that never become ready.
        self._ensure_local().proxy_config = proxy
        self._ensure_local().current_fp = fingerprint  # patch17e：复用键快照
        self._apply_cookies(page, cookies, expires)
        page.send("Page.navigate", {"url": "https://muse.ai/thread/new"})
        if self._wait_ws_ready(page, timeout=self.cfg.login_wait):
            self.current_acc_id = account_id
            return page
        # patch22：页面准备失败必须产出可核对的分类证据（最终 URL / 就绪状态 /
        # 输入框 / 登录标记 / 代理标记），而不是一句笼统的超时。
        # 所有诊断均来自页面 DOM 文本，不含 cookie/凭据。
        try:
            diag = page.js("""(function(){
                var h = document.querySelector('[data-hatch-shell-hydration-state]');
                return JSON.stringify({
                    url: location.href,
                    ready: document.readyState,
                    ta: !!document.querySelector('textarea'),
                    hydration: h ? h.getAttribute('data-hatch-shell-hydration-state') : '',
                    markers: (document.body ? document.body.innerText : '')
                        .slice(0, 600)
                });
            })()""") or "{}"
            import json as _json
            d = _json.loads(diag)
        except Exception:
            d = {"url": "", "ready": "", "ta": False, "hydration": "", "markers": ""}
        body = (d.get("markers") or "").lower()
        url_now = d.get("url") or ""
        log.warning("【页面准备失败】account=%s url=%s ready=%s textarea=%s hydration=%s markers=%r",
                    account_id, url_now[:120], d.get("ready"), d.get("ta"),
                    d.get("hydration"), body[:200].replace("\n", " "))
        self._close_page()
        if proxy and re.search(r"err_(?:proxy|tunnel)|proxy authentication|代理认证失败", body):
            raise MuseProxyError(
                "账号代理浏览器连接失败，请检查节点或认证；未回退直连",
                code="proxy_error", stage="page_prep", retryable=True)
        if "connecting..." in body:
            raise MuseGenerationError(
                "页面网关连接超时 (Connecting...)，正在自动切换账号",
                code="gateway_connecting_timeout", stage="page_prep", retryable=True)
        # Distinguish an expired session from a page that never became ready.
        if re.search(r"log in|sign in|create an account|登录|use another account", body):
            raise MuseAuthError("会话已被 muse.ai 登出（可能被其它登录挤掉或触发风控），"
                                "请用浏览器扩展重新导入 cookie；未提交任何生成请求")
        # patch22：带证据的超时分类——不是账号失效，也不是会话失效。
        # patch22e：ready 只把「非 complete 且非 interactive」算未加载——
        # interactive+textarea+hydrated 是功能就绪页，不会走到这里。
        hint = []
        if d.get("ready") not in ("complete", "interactive"):
            hint.append("主文档未完成加载(%s)" % (d.get("ready") or "?"))
        if not d.get("ta"):
            hint.append("输入框未渲染")
        if d.get("hydration") and d.get("hydration") != "hydrated":
            hint.append("前端未完成 hydration(%s)" % d.get("hydration"))
        raise MuseGenerationError(
            "muse.ai 页面加载超时（未出现聊天输入框）：%s；"
            "最终 URL=%s，未确认会话失效，未提交任何生成请求"
            % ("、".join(hint) or "页面就绪条件未满足", url_now[:100] or "unknown"),
            code="page_prep_timeout", stage="page_prep", retryable=True)

    def close_current_page(self):
        """显式释放当前线程所持有的页面和隔离上下文。"""
        self._close_page()

    def refresh(self, cookies: dict, expires: dict | None = None, account_id: str | None = None):
        self._close_page()
        return self.ensure_page(cookies, expires, account_id=account_id)

    # ---------------- 额度查询（Settings 面板） ----------------
    # muse.ai 的额度在底部 Settings 菜单 → Settings 项 → 设置面板的
    # General → Usage 区块里，形如：
    #   Free plan
    #   Weekly limit resets on Sep 30
    #   1% used
    #   Additional tokens / Never expires / 0% used (2B tokens left)
    def _click_point(self, x: int, y: int):
        for t in ("mousePressed", "mouseReleased"):
            self.page.send("Input.dispatchMouseEvent",
                           {"type": t, "x": x, "y": y,
                            "button": "left", "clickCount": 1})

    _CLICK_JS = (
        "(function(){var sel=%s;"
        "var b=[...document.querySelectorAll(sel)]"
        ".filter(function(x){return x.offsetParent!==null;})[0];"
        "if(!b)return null;var r=b.getBoundingClientRect();"
        "return JSON.stringify({x:Math.round(r.x+r.width/2),"
        "y:Math.round(r.y+r.height/2)});})()")

    _QUOTA_SETTINGS_JS = r"""(() => {
        const visible = e => e.getClientRects().length > 0
            && getComputedStyle(e).visibility !== 'hidden';
        const labels = ['Settings', '設定', '设置'];
        const b = [...document.querySelectorAll('[data-testid="hatch-dock-more"]')].find(visible)
            || [...document.querySelectorAll('button,[role=button]')].find(e =>
                visible(e) && labels.includes((e.getAttribute('aria-label') || '').trim()));
        if (!b) return null;
        const r = b.getBoundingClientRect();
        return JSON.stringify({x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)});
    })()"""

    _QUOTA_MENU_JS = r"""(() => {
        const visible = e => e.getClientRects().length > 0
            && getComputedStyle(e).visibility !== 'hidden';
        const labels = ['Settings', '設定', '设置'];
        const exact = e => labels.includes((e.textContent || '').trim());
        // Menu items include a shortcut suffix (for example 設定Ctrl+,).
        const b = [...document.querySelectorAll('[role=menuitem]')].find(e =>
            visible(e) && (exact(e) || [...e.querySelectorAll('span,div')].some(exact)))
            || [...document.querySelectorAll('[role=menu] button,[role=menu] span,[role=menu] div')]
                .find(e => visible(e) && exact(e));
        if (!b) return null;
        const r = b.getBoundingClientRect();
        return JSON.stringify({x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)});
    })()"""

    _QUOTA_DIALOG_JS = r"""(() => {
        const d = [...document.querySelectorAll('[role=dialog],[aria-modal=true]')]
            .find(e => e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden');
        return d ? (d.innerText || '') : '';
    })()"""

    def _quota_click(self, expr: str, error: str, timeout: float = 8.0):
        deadline = time.monotonic() + timeout
        while True:
            raw = self.page.js(expr)
            if raw:
                pt = json.loads(raw)
                self._click_point(pt['x'], pt['y'])
                return
            if time.monotonic() >= deadline:
                raise MuseGenerationError(error)
            time.sleep(0.15)

    def _dismiss_quota_ui(self):
        # A failed lookup can leave a menu open; clean up menus and dialogs.
        for _ in range(2):
            for typ in ('keyDown', 'keyUp'):
                self.page.send('Input.dispatchKeyEvent',
                               {'type': typ, 'key': 'Escape', 'code': 'Escape',
                                'windowsVirtualKeyCode': 27, 'nativeVirtualKeyCode': 27})
            time.sleep(0.1)  # Let the UI finish closing before the next interaction.

    def quota(self, cookies: dict, expires: dict | None = None,
              account_id: str | None = None) -> dict:
        """Read one account's quota in a fresh, disposable browser context."""
        # Muse's Settings menu can remain in a half-closed React state after a read.
        # A disposable context is slower, but prevents consecutive reads from
        # inheriting stale UI and keeps account cookies strictly isolated.
        self._close_page()
        try:
            self.ensure_page(cookies, expires, account_id=account_id)
            self._quota_click(self._QUOTA_SETTINGS_JS, '找不到设置入口（Settings／設定／设置）')
            self._quota_click(self._QUOTA_MENU_JS, '设置菜单未出现可识别的设置项')
            deadline = time.monotonic() + 12.0
            while True:
                parsed = self._parse_quota(self.page.js(self._QUOTA_DIALOG_JS) or '')
                if parsed['found']:
                    return parsed
                if time.monotonic() >= deadline:
                    raise MuseGenerationError('额度面板未读到有效用量；旧缓存未覆盖')
                time.sleep(0.2)
        finally:
            self._close_page()

    @staticmethod
    def _parse_quota(txt: str) -> dict:
        """Parse English, traditional Chinese and simplified Chinese usage text."""
        normalized = unicodedata.normalize('NFKC', txt or '')
        lines = [ln.strip() for ln in normalized.splitlines() if ln.strip()]
        # Avoid unrelated account/profile text before the Usage section.
        start = next((i + 1 for i, ln in enumerate(lines)
                      if ln.lower() in ('usage', '用量', '使用量')), 0)
        lines = lines[start:]
        end = next((i for i, ln in enumerate(lines)
                    if ln.lower() in ('language', '语言', '語言')), len(lines))
        lines = lines[:end]
        out: dict = {'raw': '\n'.join(lines[:40])}
        for ln in lines:
            if re.fullmatch(r'.{1,60}?(?:\s+plan|計劃|计划|方案)', ln, re.I):
                out['plan'] = ln
                break
        text = '\n'.join(lines)
        reset = re.search(r'Weekly limit resets? on ([^\n]+)', text, re.I)
        if not reset:
            reset = re.search(
                r'(?:每(?:週|周|星期)|週|周)(?:上限|限額|限额|額度|额度)'
                r'(?:會|会|將|将)?(?:於|于|在)\s*([^\n]+?)\s*(?:重設|重置)', text)
        if reset:
            out['weekly_reset'] = reset.group(1).strip()

        extra_at = next((i for i, ln in enumerate(lines) if re.match(
            r'^(?:(?:Additional|Extra)\s+tokens|(?:額外|额外)(?:的)?(?:符元|符記|代幣|代币|令牌|憑證|凭证|tokens))',
            ln, re.I)), len(lines))

        def percentage(line):
            m = re.search(r'(\d+(?:\.\d+)?)\s*%\s*(?:used\b|已(?:使)?用)', line, re.I)
            if not m:
                m = re.search(r'已(?:使)?用\s*(\d+(?:\.\d+)?)\s*%', line)
            if not m:
                return None
            value = float(m.group(1))
            if not 0 <= value <= 100:
                return None
            return int(value) if value.is_integer() else value

        for ln in lines[:extra_at]:
            value = percentage(ln)
            if value is not None:
                out['weekly_used_pct'] = value
                break
        for ln in lines[extra_at:]:
            value = percentage(ln)
            if value is not None:
                out['extra_used_pct'] = value
                left = re.search(r'\(([^)]+)\)', ln)
                if left:
                    out['extra_left'] = left.group(1).strip()
                break
        if re.search(r'Never expires|永不過期|永不过期', text, re.I):
            out['extra_expires'] = 'never'
        # A plan label alone is not a successful quota read. Preserve old cache.
        out['found'] = 'weekly_used_pct' in out
        return out

    # ---------------- 附件（生成结果） ----------------
    _ATT_JS = (
        "(function(){"
        "var list = []; var seen = new Set();"
        "function addEl(el, tid){"
        "  if(!el || seen.has(el)) return;"
        "  seen.add(el);"
        "  if(el.closest('form, [class*=chat-user-bubble], [class*=\"group/msg\"]')) return;"
        "  var v = el.querySelector('video') || (el.tagName === 'VIDEO' ? el : null);"
        "  var img = el.querySelector('img') || (el.tagName === 'IMG' ? el : null);"
        "  var isVid = (tid || '').includes('video') || !!v;"
        "  var primary = isVid ? (v || img) : (img || v);"
        "  var src = primary ? (primary.currentSrc || primary.src || '') : '';"
        "  if(src && !seen.has(src)){"
        "    seen.add(src);"
        "    list.push({"
        "      tid: tid || el.getAttribute('data-testid') || (isVid ? 'video' : 'image'),"
        "      hasVideo: !!v,"
        "      hasImg: !!img,"
        "      src: src,"
        "      vSrc: v ? (v.currentSrc || v.src || '') : '',"
        "      iSrc: img ? (img.currentSrc || img.src || '') : '',"
        "      w: primary ? (primary.videoWidth || primary.naturalWidth || 0) : 0,"
        "      h: primary ? (primary.videoHeight || primary.naturalHeight || 0) : 0"
        "    });"
        "  }"
        "}"
        "document.querySelectorAll('[data-testid^=\"hatch-chat-attachment-presentation-\"]').forEach(function(a){ addEl(a, a.getAttribute('data-testid')); });"
        "document.querySelectorAll('div[class*=\"hatch-agent-bubble-bg\"] img, div[class*=\"hatch-agent-bubble-bg\"] video').forEach(function(m){"
        "  var s = m.currentSrc || m.src || '';"
        "  if(s && !s.includes('avatar') && !s.includes('emoji')) addEl(m.parentElement || m, 'agent-media');"
        "});"
        "return JSON.stringify(list);"
        "})()"
    )

    def attachments(self) -> list[dict]:
        try:
            raw = self.page.js(self._ATT_JS)
            return json.loads(raw) if raw else []
        except Exception:  # noqa: BLE001
            return []

    # ---------------- 发送 ----------------
    # 检查「文字真的进了输入框 + Send 按钮真的被渲染出来」。
    # 两个条件缺一不可：Send 按钮只有 React state 里有文字才会渲染 ——
    # 它在，就说明 React 真的收到了输入（不是 DOM value 被改了而已）。
    _SEND_BUTTON_JS = r"""(function(){
        function visible(e){
            return e.getClientRects().length > 0
                && getComputedStyle(e).visibility !== 'hidden';
        }
        var ta = document.activeElement;
        if (!ta || !ta.matches('textarea') || !visible(ta)) {
            ta = Array.from(document.querySelectorAll('textarea')).find(visible);
        }
        if (!ta) return null;
        var label = /^(?:send(?:\s+message)?|发送(?:消息)?|發送(?:訊息)?|傳送(?:訊息)?|送出)$/i;
        var testId = /^(?:hatch-composer-send-button|composer-send-button|send-button)$/i;
        function isSend(b){
            // The Send button itself uses the bg-chat-user-bubble color class.
            // Exclude message containers, not buttons carrying that color token.
            if (!visible(b) || b.closest('aside,nav,[role="navigation"],'
                + 'div[class*="chat-user-bubble"],div[class*="group/msg"],'
                + 'div[class*="hatch-chat-groupable-bubble"]')) return false;
            return label.test((b.getAttribute('aria-label') || '').trim())
                || testId.test((b.getAttribute('data-testid') || '').trim())
                || label.test((b.innerText || '').trim());
        }
        // Search the nearest composer first. Never search the whole document:
        // a history preview may itself contain "send", "Resend" or "Send feedback".
        for (var r = ta.parentElement; r && r !== document.body; r = r.parentElement) {
            var b = Array.from(r.querySelectorAll('button,[role="button"]')).find(isSend);
            if (b) return b;
            if (r.matches('form,main,[role="main"]')) break;
        }
        return null;
    })()"""
    _SEND_DISABLED_JS = (
        "b.disabled || b.matches(':disabled')"
        " || b.getAttribute('aria-disabled') === 'true' || b.closest('[inert]')"
    )
    _SEND_STATE_JS = (
        "(function(){var ta=document.querySelector('textarea');"
        "var b=" + _SEND_BUTTON_JS + ";"
        "return JSON.stringify({v:ta?ta.value:'',btn:b?("
        + _SEND_DISABLED_JS + "?2:1):0});})()"
    )
    _CLICK_SEND_JS = (
        "(function(){var b=" + _SEND_BUTTON_JS + ";"
        "if(!b)return 'no-button';"
        "if(" + _SEND_DISABLED_JS + ")return 'disabled';"
        "b.click();return 'clicked';})()"
    )

    def _send(self, prompt: str):
        # 1. 确保 textarea 滚动到视口中央并获得真实焦点
        try:
            self.page.js("""(function(){
                var ta = document.querySelector('textarea');
                if (ta) {
                    ta.scrollIntoView({block: 'center', inline: 'nearest'});
                    ta.focus();
                }
            })()""")
        except Exception:
            pass
        time.sleep(0.1)

        rect = self.page.js(
            "(function(){var t=document.querySelector('textarea');if(!t)return null;"
            "var r=t.getBoundingClientRect();"
            "return JSON.stringify({x:Math.round(r.left+r.width/2),"
            "y:Math.round(r.top+r.height/2)});})()")
        if not rect:
            raise MuseGenerationError("找不到聊天输入框")
        try:  # patch13(F15)
            c = json.loads(rect)
        except (TypeError, ValueError) as exc:
            raise MuseGenerationError("聊天输入框定位失败（页面状态异常）") from exc
        for t in ("mousePressed", "mouseReleased"):
            self.page.send("Input.dispatchMouseEvent",
                           {"type": t, "x": c["x"], "y": c["y"],
                            "button": "left", "clickCount": 1})
        time.sleep(0.1)

        # 触发 React 18 原型 setter 以及 input/change 事件以同步发送按钮状态
        # （对于 DeepSeek/Codex 等 100KB+ 超长上下文，直接走原型 setter 仅需 <1s，避免 Input.insertText 逐字注入卡死）
        _SETTER_JS = (
            "(function(t){var ta=document.querySelector('textarea');"
            "if(!ta) return 0;"
            "ta.focus();"
            "var s=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set;"
            "s.call(ta,t);"
            "ta.dispatchEvent(new Event('input',{bubbles:true}));"
            "ta.dispatchEvent(new Event('change',{bubbles:true}));"
            "return (ta.value||'').length;})(%s)")
        val_len = self.page.js(_SETTER_JS % json.dumps(prompt)) or 0
        if not val_len and len(prompt) < 500:
            self.page.send("Input.insertText", {"text": prompt})
            self.page.js(_SETTER_JS % json.dumps(prompt))

        # 等待发送按钮就绪并点击
        clicked = "no-button"
        t_deadline = time.time() + 3.0
        while time.time() < t_deadline:
            res = self._click_send()
            if res == "clicked":
                clicked = "clicked"
                break
            time.sleep(0.08)

        if clicked != "clicked":
            # 兜底：Ctrl+Enter 或普通 Enter
            # patch13(DeleteList)：原 {"modifiers": 1} 实为 Alt+Enter（CDP 位掩码
            # Alt=1, Ctrl=2）——在 composer 里是换行非发送，纯空转还插换行符。
            for combo in ({"modifiers": 2}, {}):
                for t in ("keyDown", "char", "keyUp"):
                    params = {"type": t, "key": "Enter", "code": "Enter",
                              "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13,
                              "modifiers": combo.get("modifiers", 0)}
                    if t == "char":
                        params["text"] = "\r"
                        params["unmodifiedText"] = "\r"
                    self.page.send("Input.dispatchKeyEvent", params)
                time.sleep(1.0)
                try:
                    if self.page.js("!(document.querySelector('textarea')||{value:''}).value"):
                        clicked = "enter-sent"
                        break
                except Exception:
                    pass
        time.sleep(0.3)
        return clicked

    def _composer_unsent(self) -> str:
        """输入框里还留着什么（没发出去的附件 / 文字）；空串 = 输入框是空的。读不到时按空处理（不误判）。"""
        try:
            n = int(self.page.js(self._ATTACHED_COUNT_JS) or 0)
            t = int(self.page.js("(document.querySelector('textarea')||{value:''}).value.length") or 0)
        except Exception:  # noqa: BLE001
            return ""
        parts = []
        if n:
            parts.append("%d 个附件" % n)
        if t:
            parts.append("%d 字提示词" % t)
        return "、".join(parts)

    def _click_send(self) -> str:
        """只点当前输入框的发送按钮（不重新填字），不把点击等同于上游送达。"""
        try:
            return str(self.page.js(self._CLICK_SEND_JS))
        except Exception:  # noqa: BLE001
            return "error"

    # ---------------- 等待生成 ----------------
    def _last_attachment(self) -> dict | None:
        atts = self.attachments()
        return atts[-1] if atts else None

    def _scroll_bottom(self):
        """滚到聊天底部。muse.ai 的聊天滚动容器是内层 div（不是 document），
        虚拟列表按滚动位置渲染节点 —— 不滚到底，新消息根本不在 DOM 里。"""
        try:
            self.page.js(
                "(function(){"
                "var els=[...document.querySelectorAll('*')].filter(function(e){"
                "var s=getComputedStyle(e);"
                "return (s.overflowY==='auto'||s.overflowY==='scroll')"
                "&&e.scrollHeight>e.clientHeight+100;});"
                "els.sort(function(a,b){return b.scrollHeight-a.scrollHeight;});"
                "if(els[0])els[0].scrollTop=els[0].scrollHeight;"
                "var s=document.scrollingElement||document.body;"
                "s.scrollTop=s.scrollHeight;"
                "var el=document.querySelector('textarea');"
                "if(el)el.scrollIntoView({block:'end'});return 1;})()")
        except Exception:  # noqa: BLE001
            pass

    def _wait_attachment(self, baseline_src: str, timeout: int, expect: str,
                         on_progress=None, base_agent_cnt: int = 0,
                         base_att_cnt: int = 0, stop_event=None, baseline_sources=None,
                         has_upload: bool = False, base_user_cnt: int = 0) -> dict | None:
        baseline_sources = set(baseline_sources or ()) | {baseline_src}
        deadline = time.time() + timeout
        t_start = time.time()
        stable_src, stable_n = "", 0
        last_txt, txt_stable = "", 0
        refusal_last, refusal_stable = "", 0
        seen_stop = False
        previous_state = None
        last_activity = t_start
        progress_sent = 0
        resent = False
        # patch11：分层判定状态。user_bubble_seen = 消息提交成功的 DOM 收据
        # （chat-user-bubble 渲染出来 = 上游已接收本条消息+附件）；
        # att_ever_grew = presentation 附件曾挂载过（上游处理附件的痕迹）。
        user_bubble_seen = False
        att_ever_grew = False
        # patch12：base_user_cnt 由调用方在 _send 之前采集传入（patch11 在此
        # 采集太晚——气泡可能在 send 与 wait 进入之间已渲染，基线被污染，
        # 收据永远判不出 → L2 误判已送达消息并重复提交）。
        while time.time() < deadline:
            if stop_event is not None and stop_event.is_set():
                raise MuseGenerationError("客户端已断开连接，终止生成任务")
            time.sleep(0.6)
            self._scroll_bottom()
            atts = self.attachments()
            # patch11：观测消息提交收据与附件挂载痕迹（每轮刷新）。
            # patch21c：user_bubble_seen 只被带图分层判定（L2/L3）消费，
            # 纯文本等待不发这条 CDP 往返（省一次 js 调用，也让 page.js 的
            # 调用序列在无附件场景与上游语义一致）。
            if has_upload and not user_bubble_seen:
                cur_user = self._user_count()
                if cur_user > base_user_cnt:
                    user_bubble_seen = True
            if len(atts) > base_att_cnt:
                att_ever_grew = True
            # 严格按期望类型挑选附件，绝不接受跨模态兜底（如将视频请求降级为图片封面）
            matching_atts = []
            for a in atts:
                tid = (a.get("tid") or "").lower()
                has_video = bool(a.get("hasVideo"))
                src = a.get("src") or ""
                v_src = a.get("vSrc") or ""
                if expect == "video":
                    # tid 含 "video" 不足以命中：muse 会先把 data-testid 标成
                    # ...-presentation-video，此时 <video> 可能尚未挂上，_ATT_JS 的
                    # primary = v || img 会退回封面 img，src/w/h 全是静帧。
                    # 只认真正的视频源（vSrc / hasVideo / 视频扩展名）。
                    is_match = (
                        has_video
                        or bool(v_src)
                        or src.endswith((".mp4", ".webm", ".mov"))
                    )
                else:
                    is_match = ("image" in tid) or (not has_video and not src.endswith((".mp4", ".webm", ".mov")))
                if is_match:
                    matching_atts.append(a)

            att = matching_atts[-1] if matching_atts else None
            if att:
                src = att.get("src") or ""
                v_src = att.get("vSrc") or ""
                w = att.get("w", 0) or 0
                h = att.get("h", 0) or 0
                # 视频任务禁止用封面 src 冒充成品；无 vSrc 时仅接受显式视频后缀的 src
                if expect == "video":
                    check_src = v_src if v_src else (
                        src if src.endswith((".mp4", ".webm", ".mov")) else ""
                    )
                else:
                    check_src = src
                if check_src and check_src not in baseline_sources:
                    if w > 0 and h > 0:
                        return att
                    if check_src == stable_src:
                        stable_n += 1
                    else:
                        stable_src, stable_n = check_src, 0
                    if stable_n >= 1:
                        return att
            elapsed = time.time() - t_start
            cur_cnt = base_agent_cnt
            cur_txt = ""
            has_stop = False
            tail = ""
            try:
                st_raw = self.page.js("""(function(){
                    var bs=[].slice.call(document.querySelectorAll('div[class*="hatch-chat-groupable-bubble"]'))
                        .filter(function(b){return /hatch-agent-bubble-bg/.test(b.className||'');});
                    var lastTxt = bs.length ? (bs[bs.length-1].innerText||'').trim() : '';
                    var hasStop = !!(document.querySelector('[data-testid="hatch-composer-stop-button"]')
                        || document.querySelector('button[aria-label*="Stop" i]')
                        || document.querySelector('button[aria-label*="停止"]'));
                    var tail = document.body ? (document.body.innerText||'').slice(-1200) : '';
                    return JSON.stringify({cnt: bs.length, txt: lastTxt, stop: hasStop, tail: tail,
                        path: location.pathname || '', href: location.href || ''});
                })()""")
                st = json.loads(st_raw) if st_raw else {}
            except Exception:
                st = {}
            tail = st.get("tail") or ""
            cur_cnt = int(st.get("cnt") or 0)
            cur_txt = st.get("txt") or ""
            has_stop = bool(st.get("stop"))
            path = st.get("path") or ""
            seen_stop = seen_stop or has_stop
            state = (cur_cnt, cur_txt, len(atts), has_stop, path)
            if previous_state is None or state != previous_state:
                last_activity = time.time()
                previous_state = state

            if on_progress:
                milestone = 25 if seen_stop else 20
                if cur_cnt > base_agent_cnt:
                    milestone = 40
                if milestone > progress_sent:
                    try:
                        on_progress(milestone)
                        progress_sent = milestone
                    except Exception:
                        pass

            # COOL AI 2026-11-08：发出 25 秒后模型毫无回复、输入框里还留着附件 / 文字 = 这条消息没发出去。
            # 补点一次发送；60 秒仍没发出就报错，交给外层换号重试（以前要干等到总超时 10 分钟）。
            # patch11：分层判定（替换 patch9 的单条件误杀版）。
            # 原条件把「agent 气泡未现 + presentation 未挂载」当成附件层失效，
            # 但用户气泡（上游接收回执）出现后上游可静默慢启动 >25s——
            # 同批 6 成功 1 失败即此竞速（task_5f914296 实证）。
            # patch13(F24)：额度检查上移——额度耗尽可能同时满足「气泡已现 +
            # 附件未挂载」，先于 L3 判定避免坏账账号被误归因 attachment_broken。
            if re.search(r"额度不足|积分不足|out of credits|达到上限|token limit", tail, re.I):
                raise MuseGenerationError("账号额度不足", code="quota_exhausted", stage="wait", retryable=False)
            if has_upload and elapsed > 25.0 and cur_cnt == base_agent_cnt:
                if not user_bubble_seen:
                    # L2 发送层未送达：用户气泡从未出现 = 消息没提交。
                    # 输入框还留着东西就补点一次发送；再不行按发送失败处理
                    # （换号重试有意义——不是附件层坏）。
                    unsent = self._composer_unsent()
                    # patch13(F14)：点击前重读收据——观测与点击之间隔着 CDP 往返，
                    # 气泡可能恰在此渲染，盲目补发会重复提交已送达消息。
                    if self._user_count() > base_user_cnt:
                        user_bubble_seen = True
                        unsent = ""
                    if unsent and not resent:
                        resent = True
                        log.warning("带图提示词发出 %.0f 秒未送达（%s），补点一次发送",
                                    elapsed, unsent)
                        self._click_send()
                    elif unsent and elapsed > 60.0:
                        raise MuseGenerationError(
                            "带图消息未送达（用户气泡未出现，%s 仍在输入框），终止等待换号重试"
                            % unsent)
                    elif not unsent and elapsed > 45.0:
                        # 输入框已空但收据仍缺：提交状态未知，按发送失败处理
                        raise MuseGenerationError(
                            "带图消息提交状态未知（输入框已空但用户气泡未出现 45s），换号重试")
                elif not att_ever_grew:
                    # L3 真附件层失效：上游收了消息，但 presentation 附件从未挂载
                    # （附件没进对话上下文）。改 prompt 修不了，快速失败不烧重试链。
                    raise MuseModelRefusalError(
                        "上游附件失效：消息已送达（用户气泡出现）但 25 秒内附件从未进入生成上下文",
                        code="upstream_attachment_broken")
                # L1：user_bubble_seen 且 att_ever_grew（或曾增长）→ 上游正常处理中，
                # 静默慢启动不设限，交由外层 deadline 兜底。

            # patch12：legacy 纯文本 unsent 块加 not has_upload 门槛——
            # 带图场景由上方 patch11 分层判定接管（其 L2 有用户气泡收据门槛，
            # legacy 块没有：气泡已出现仍补点发送会把已送达消息重复提交）。
            if not has_upload and elapsed > 25.0 and cur_cnt == base_agent_cnt:
                unsent = self._composer_unsent()
                if unsent and not resent:
                    resent = True
                    log.warning("提示词发出 %.0f 秒后仍在输入框（%s），补点一次发送", elapsed, unsent)
                    self._click_send()
                elif unsent and elapsed > 60.0:
                    raise MuseGenerationError("提示词没有发出去（%s 仍在输入框里），终止等待并换号重试" % unsent)

            # patch13(F24)：额度检查已上移到分层判定之前
            # A Stop button is also shown for messages never acknowledged by the gateway.
            if elapsed > 30.0 and cur_cnt == base_agent_cnt and re.search(
                    r"Still sending|Delivery not confirmed|正在发送|未确认送达", tail, re.I):
                raise MuseGenerationError(
                    "提示词未成功送达后端（Still sending / Delivery not confirmed），终止等待并重试")

            refusal_source = cur_txt if cur_cnt > base_agent_cnt else (
                tail if seen_stop and not has_stop and elapsed > 20.0 else "")
            refusal = self._media_refusal_text(refusal_source, expect)
            if refusal and not has_stop and len(atts) <= base_att_cnt:
                if refusal == refusal_last:
                    refusal_stable += 1
                else:
                    refusal_last, refusal_stable = refusal, 0
                if refusal_stable >= 4 and elapsed > 8.0:
                    raise MuseModelRefusalError(f"模型未生成媒体，仅返回文本: {refusal}")
            else:
                refusal_last, refusal_stable = "", 0

            if cur_cnt > base_agent_cnt and cur_txt and not has_stop and len(atts) <= base_att_cnt:
                # Media-status text can precede a real attachment; do not fail on it.
                is_media_report = self._media_status_pending(cur_txt)
                if not is_media_report:
                    if cur_txt == last_txt:
                        txt_stable += 1
                    else:
                        last_txt, txt_stable = cur_txt, 0
                    if txt_stable >= 15 and elapsed > 8.0:
                        clean = self._clean_model_text(cur_txt)
                        raise MuseModelRefusalError(
                            f"模型未生成媒体，仅返回文本: {clean}")
                else:
                    txt_stable = 0
            else:
                txt_stable = 0

            # Muse accepted the click but never created a thread. Requiring a completed
            # Stop-button lifecycle, an idle page, no messages/attachments and /thread/new
            # avoids treating ordinary long video rendering as failure.
            if (expect == "video" and seen_stop and not has_stop
                    and path.rstrip("/").endswith("/thread/new")
                    and cur_cnt <= base_agent_cnt and len(atts) <= base_att_cnt
                    and elapsed >= 180.0 and time.time() - last_activity >= 90.0):
                raise MuseGenerationError(
                    "Muse 未建立视频任务线程，页面在提交结束后持续无响应（已提前停止等待）")
        return None

    @staticmethod
    def _clean_model_text(text: str, limit: int = 240) -> str:
        clean = re.sub(r"\s+", " ", text or "").strip()
        return clean[:limit]

    @staticmethod
    def _media_status_pending(text: str) -> bool:
        """A success claim can precede an attachment; it is never delivery proof."""
        if re.search(
                r"(?:未|没(?:有)?|无法|不能|未能)[^。！？!?\n]{0,16}(?:生成|制作|提供|完成)|"
                r"(?:生成|制作|渲染|处理)[^。！？!?\n]{0,12}(?:失败|未成功)|"
                r"\b(?:not|never|hasn['’]t|wasn['’]t|isn['’]t)\s+(?:(?:be|been|successfully)\s+){0,3}"
                r"(?:generated|created|completed|ready)|"
                r"\b(?:no|without)\s+(?:video|clip|image|picture|animation)\b"
                r"[^.\n]{0,60}\b(?:generated|created|ready)|"
                r"\b(?:failed|cannot|can['’]t|could not|couldn['’]t|unable to)\b"
                r"[^.\n]{0,60}\b(?:generat(?:e|ed|ing)|creat(?:e|ed|ing)|render|provide|video|image)\b|"
                r"\b(?:video|image|render|upload)\b[^.\n]{0,32}\bfailed\b",
                text, re.I):
            return False
        return bool(re.search(
            r"\.(?:webp|png|jpe?g|mp4|webm)|imagine_media|deliverable|"
            r"generated\s+.*image|verified\s+generated|artifact|"
            # patch22j：muse 视频 I2V 的实测成功声明报法——模型先输出参数
            # 验证报告（"Verified: 6.00 seconds, 144 frames, 24fps, 1248x704,
            # native I2V only..."），媒体随后才渲染/挂载。task_2e001214291342ea911e
            # 实证：此文本 75s 稳定即被 txt_stable 误判「仅文本无媒体」提前终止
            # （实际视频仍在产出）。此类带参数的 Verified 报告必须当作进行中信号。
            r"^Verified?:\s*\d|[0-9]+\s*frames?[, ]|[0-9]+\s*fps\b|"
            r"^(?:已(?:经)?(?:成功)?生成|生成好了|生成完毕|已完成|做好了)(?:[，。！\s]|$)|"
            r"(?:视频|影片|短片|图片|图像)[^。！？!?\n]{0,40}"
            r"(?:已(?:经)?(?:成功)?生成|生成(?:成功|好了|完毕|完成)|已(?:经)?完成|做好了)|"
            r"(?:已(?:经)?(?:成功)?生成|生成好了|生成完毕|已完成|做好了)"
            r"[^。！？!?\n]{0,32}(?:视频|影片|短片|图片|图像)|"
            r"\b(?:video|clip|image|picture|animation)\b[^.\n]{0,60}"
            r"\b(?:generated|created|rendered|completed|ready)\b|"
            r"\b(?:generated|created|rendered|completed)\b[^.\n]{0,32}"
            r"\b(?:video|clip|image|picture|animation)\b", text, re.I))

    @classmethod
    def _media_refusal_text(cls, text: str, expect: str) -> str:
        """Return only an explicit media refusal line, never an arbitrary page tail."""
        if not text:
            return ""
        patterns = [
            r"^(?:没生成成|视频没生成成功|视频未生成成功|视频生成失败)[^\n]{0,240}"
            r"(?:国家|地区|country|region)[^\n]{0,160}(?:不支持|不可用|无法|not available|unavailable|not supported)",
            r"^(?:could not|couldn't|cannot|can't|unable to)[^\n]{0,120}video[^\n]{0,160}"
            r"(?:country|region|not available|unavailable|not supported)",
        ] if expect == "video" else [
            r"^(?:图片没生成成功|图片未生成成功|图片生成失败|无法生成图片)",
            r"^(?:could not|couldn't|cannot|can't|unable to)[^\n]{0,120}image",
        ]
        for line in reversed((text or "").splitlines()):
            clean = cls._clean_model_text(line)
            if clean and any(re.search(pattern, clean, re.I) for pattern in patterns):
                return clean
        return ""

    # ---------------- 取字节 ----------------
    _EXTRACT_JS = r"""
    (async function(src, expect){
      try{
        var u = src;
        if(!u) return JSON.stringify({ok:false,err:'no-media-src'});
        var r = await fetch(u);
        if(!r.ok) return JSON.stringify({ok:false,err:'media-http-'+r.status});
        var b = await r.blob();
        var ab = await b.arrayBuffer();
        var bytes = new Uint8Array(ab);
        var s = '';
        for(var i=0; i<bytes.length; i+=65536){
          s += String.fromCharCode.apply(null, bytes.subarray(i, i+65536));
        }
        return JSON.stringify({ok:true, mime:b.type||'', size:b.size, url:u, b64:btoa(s)});
      }catch(e){
        return JSON.stringify({ok:false, err:String(e)});
      }
    })(%s, %s)
    """

    # ---------------- 文本 / 代码对话 ----------------
    _AGENT_TEXT_JS = (
        "(function(){"
        "var bs=[].slice.call(document.querySelectorAll("
        "'div[class*=\"hatch-chat-groupable-bubble\"]'));"
        "for(var i=bs.length-1;i>=0;i--){"
        "var cs=bs[i].className||'';"
        "if(/hatch-agent-bubble-bg/.test(cs))return bs[i].innerText||'';}"
        "return '';})()"
    )
    _USER_COUNT_JS = (
        "(function(){"
        "var bs=[].slice.call(document.querySelectorAll("
        "'div[class*=\"hatch-chat-groupable-bubble\"]'));"
        "var n=0;for(var i=0;i<bs.length;i++){"
        "if(/chat-user-bubble/.test(bs[i].className||''))n++;}"
        "return String(n);})()"
    )
    _AGENT_COUNT_JS = (
        "(function(){"
        "var bs=[].slice.call(document.querySelectorAll("
        "'div[class*=\"hatch-chat-groupable-bubble\"]'));"
        "var n=0;for(var i=0;i<bs.length;i++){"
        "if(/hatch-agent-bubble-bg/.test(bs[i].className||''))n++;}"
        "return String(n);})()"
    )
    _POLL_CHAT_JS = (
        "(function(){"
        "var els=[...document.querySelectorAll('*')].filter(function(e){"
        "var s=getComputedStyle(e);"
        "return (s.overflowY==='auto'||s.overflowY==='scroll')&&e.scrollHeight>e.clientHeight+100;});"
        "els.sort(function(a,b){return b.scrollHeight-a.scrollHeight;});"
        "if(els[0])els[0].scrollTop=els[0].scrollHeight;"
        "var scope=document.querySelector('main,[class*=\"chat-scroll\"],[class*=\"hatch-chat-scroll\"]')||document.body;"
        "var bs=[].slice.call(scope.querySelectorAll('div[class*=\"hatch-chat-groupable-bubble\"]'))"
        ".filter(function(b){return /hatch-agent-bubble-bg/.test(b.className||'');});"
        "var nonEmpty=bs.filter(function(b){return ((b.innerText||'').trim().length)>0;});"
        "var txt=nonEmpty.length?(nonEmpty[nonEmpty.length-1].innerText||'').trim():'';"
        "var stop=!!(document.querySelector('[data-testid=\"hatch-composer-stop-button\"]')"
        "||document.querySelector('button[aria-label*=\"Stop\" i]')"
        "||document.querySelector('button[aria-label*=\"停止\"]'));"
        "return JSON.stringify({cnt:nonEmpty.length,total:bs.length,txt:txt,stop:stop});})()"
    )

    def _agent_text(self) -> str:
        """最后一个助手气泡的文本（取不到就返回空串）。"""
        try:
            return (self.page.js(self._AGENT_TEXT_JS) or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    def _agent_count(self) -> int:
        try:
            return int(self.page.js(self._AGENT_COUNT_JS) or 0)
        except Exception:  # noqa: BLE001
            return 0

    def _user_count(self) -> int:
        try:
            return int(self.page.js(self._USER_COUNT_JS) or 0)
        except Exception:  # noqa: BLE001
            return 0

    def _poll_chat(self) -> tuple[int, str, bool]:
        try:
            raw = self.page.js(self._POLL_CHAT_JS)
            if raw:
                d = json.loads(raw)
                return int(d.get("cnt") or 0), (d.get("txt") or "").strip(), bool(d.get("stop"))
        except Exception:
            pass
        return 0, "", False

    def chat_stream(self, cookies: dict, prompt: str, expires: dict | None = None,
                    timeout: int | None = None, account_id: str | None = None,
                    stop_event=None):
        """发一条消息，流式 yield 增量文本。"""
        timeout = int(timeout or getattr(self.cfg, "chat_timeout", 300))
        self.ensure_page(cookies, expires, account_id=account_id)
        self.reset_thread(for_chat=True)
        base_agent, base_text, _ = self._poll_chat()
        self._send(prompt)

        t_sent = time.time()
        deadline = t_sent + timeout
        first_token_deadline = min(deadline, t_sent + 40.0)
        sent, last, stable = "", None, 0
        got_first = False

        # 1. 等待助手生成并开始吐字（高频 60ms 采样，捕获到首批增量文字瞬间 yield 出去）
        while time.time() < first_token_deadline:
            if stop_event is not None and stop_event.is_set():
                return
            time.sleep(0.06)
            cnt, cur, has_stop = self._poll_chat()
            if not cur or (cnt <= base_agent and cur == base_text):
                if time.time() - t_sent > 14.0:
                    try:
                        tail = self.page.js("document.body.innerText.slice(-500)") or ""
                    except Exception:
                        tail = ""
                    if "Still sending" in tail or "Connecting..." in tail:
                        raise MuseGenerationError("云端 VM 连接超时 (Still sending)")
                continue

            delta = cur[len(sent):] if cur.startswith(sent) else cur
            if delta:
                sent = cur
                yield delta
                last = cur
                got_first = True
                break

        if not got_first:
            raise MuseGenerationError("等待助手首字响应超时")

        # 2. 持续捕获增量文本
        while time.time() < deadline:
            if stop_event is not None and stop_event.is_set():
                return
            time.sleep(0.06)
            cnt, cur, has_stop = self._poll_chat()
            if not cur:
                continue

            if cur != last:
                delta = cur[len(sent):] if cur.startswith(sent) else cur
                if delta:
                    sent = cur
                    yield delta
                last, stable = cur, 0
            else:
                stable += 1
                # Stop 按钮消失说明前端生成彻底结束，连续 3 次（约 0.18s）无新文本即正常退出
                if not has_stop and stable >= 3:
                    return
                # Stop 按钮仍在时绝不过早截断（模型思考、代码块或网络抖动），允许等待至 stable >= 80（约 5s）防卡死
                if has_stop and stable >= 80:
                    return

        raise MuseGenerationError("等待助手回复超时")

    def chat(self, cookies: dict, prompt: str, expires: dict | None = None,
             timeout: int | None = None, account_id: str | None = None) -> str:
        """发一条消息，返回完整回复文本（非流式）。"""
        out = ""
        for chunk in self.chat_stream(cookies, prompt, expires, timeout, account_id=account_id):
            out += chunk
        return out

    def extract_bytes(self, src: str, expect: str = "image", retries: int = 4,
                      stop_event=None):
        last = "未知"
        for _ in range(retries):
            # patch13(F7)：每轮检查取消——提取阶段原脱离外层 deadline/stop_event，
            # blob 回收反复失败时任务会占账号 ~40 分钟不可取消。
            if stop_event is not None and stop_event.is_set():
                raise MuseGenerationError("任务已取消")
            raw = self.page.js(self._EXTRACT_JS % (json.dumps(src), json.dumps(expect)),
                               await_promise=True, timeout=600)
            try:
                info = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:  # noqa: BLE001
                info = {"ok": False, "err": f"解析失败 {str(raw)[:150]}"}
            if info.get("ok"):
                return base64.b64decode(info["b64"]), info.get("mime", ""), info.get("url", "")
            last = info.get("err", "未知")
            time.sleep(2)
        raise MuseGenerationError(f"未能取回生成结果: {last}")

    # ---------------- 下载兜底 ----------------
    def _download_fallback(self, src: str, timeout: int = 180) -> str | None:
        directory = self.download_dir
        before = set(os.listdir(directory))
        # ponytail: fail closed when the selected result has no local download;
        # never click an unrelated/global button that can return the upload.
        clicked = self.page.js("""(function(src){
            var media=[...document.querySelectorAll('img,video')]
                .find(m=>(m.currentSrc||m.src||'')===src);
            var node=media && media.closest('[data-testid^="hatch-chat-attachment-presentation-"]');
            if(!node) return 'none';
            node=node.closest('[class*="group/widget-presentation"]')||node;
            var b=[...node.querySelectorAll('button,[role=button]')]
                .find(x=>/下载|保存|download/i.test(x.getAttribute('aria-label')||''));
            if(!b) return 'none';
            b.click();return 'ok';
        })(%s)""" % json.dumps(src))
        if clicked == "none":
            return None
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(1.5)
            new = [f for f in (set(os.listdir(directory)) - before)
                   if not f.endswith(".crdownload")]
            if new:
                p = os.path.join(directory, max(
                    new, key=lambda f: os.path.getmtime(os.path.join(directory, f))))
                if os.path.getsize(p) > 0:
                    return p
        return None

    def _normalize_image(self, img: str) -> tuple[str, str]:
        """Normalize one reference file (image/video/audio/document) and enforce
        source, MIME, size and video-duration policy."""
        if not img:
            return "", "image/png"
        img = str(img).strip()

        def checked(data: bytes, mime: str, fallback: str = "image/png",
                    name: str = "") -> tuple[str, str]:
            mime = (mime or "").split(";", 1)[0].strip().lower()
            mime = _MIME_ALIASES.get(mime, mime)
            if mime in _GENERIC_MIMES:
                mime = (_sniff_mime(data)
                        or _MIME_ALIASES.get(mimetypes.guess_type(name)[0] or "",
                                             mimetypes.guess_type(name)[0] or "")
                        or "")
            if mime not in REFERENCE_MIME_EXT:
                raise MuseGenerationError(
                    f"参考文件类型不受支持（{mime or 'unknown'}）；"
                    "支持图片、视频(mp4/mov/webm/3gp)、音频(mp3/wav/m4a/ogg/flac/aac/opus)与 PDF/Word")
            if len(data) > MAX_REFERENCE_IMAGE_BYTES:
                raise MuseGenerationError(
                    f"单个参考素材超过 {MAX_REFERENCE_IMAGE_BYTES // _MIB} MiB 限制")
            if mime.startswith("video/"):
                seconds = _webm_duration(data) if mime == "video/webm" else _mp4_duration(data)
                if seconds is None or not math.isfinite(seconds) or seconds <= 0:
                    raise MuseGenerationError("无法确认参考视频时长，请提供带有效时长信息的 MP4/MOV/WebM/3GP 文件")
                if seconds > MAX_REFERENCE_VIDEO_SECONDS:
                    raise MuseGenerationError(
                        f"参考视频时长 {seconds:.0f} 秒，超过 {MAX_REFERENCE_VIDEO_SECONDS} 秒限制")
            return base64.b64encode(data).decode("ascii"), mime

        if img.startswith("data:"):
            head, sep, payload = img.partition(",")
            if not sep:
                raise MuseGenerationError("参考图 Data URL 格式无效")
            mime = head[5:].split(";", 1)[0]
            try:
                data = (base64.b64decode(payload.strip(), validate=True)
                        if ";base64" in head.lower()
                        else urllib.parse.unquote_to_bytes(payload))
            except (binascii.Error, ValueError) as exc:
                raise MuseGenerationError("参考图 Data URL 编码无效") from exc
            return checked(data, mime)

        if img.startswith(("http://", "https://")):
            try:
                req = urllib.request.Request(img, headers={"User-Agent": "Mozilla/5.0"})
                response = (urllib.request.urlopen(req, timeout=20)
                            if self.cfg.ref_image_private_fetch
                            else _open_public_ref_image(img))
                with response as resp:
                    declared = resp.headers.get("Content-Type")
                    mime = resp.headers.get_content_type() if declared else ""
                    data = resp.read(MAX_REFERENCE_IMAGE_BYTES + 1)
                return checked(data, mime, name=urllib.parse.urlsplit(img).path)
            except (RefImageSafetyError, MuseGenerationError):
                raise
            except Exception as exc:
                raise MuseGenerationError(
                    f"下载远程参考图失败: {type(exc).__name__}") from exc

        # Raw Base64 contains no server-side path and remains safe in restricted mode.
        try:
            return checked(base64.b64decode(img, validate=True), "")
        except (binascii.Error, ValueError):
            pass

        if not self.cfg.ref_image_local_files:
            raise RefImageSafetyError(
                "参考图本地路径被拒绝；如确需读取服务器文件，请显式设置 "
                "MUSE2API_REF_IMAGE_LOCAL_FILES=1")
        if os.path.isfile(img):
            try:
                if os.path.getsize(img) > MAX_REFERENCE_IMAGE_BYTES:
                    raise MuseGenerationError(
                        f"单个参考素材超过 {MAX_REFERENCE_IMAGE_BYTES // _MIB} MiB 限制")
                with open(img, "rb") as f:
                    return checked(f.read(MAX_REFERENCE_IMAGE_BYTES + 1),
                                   mimetypes.guess_type(img)[0] or "", name=img)
            except MuseGenerationError:
                raise
            except Exception as exc:
                raise MuseGenerationError(
                    f"读取本地参考图失败: {type(exc).__name__}") from exc
        raise MuseGenerationError("参考图必须是 URL、Data URL、本地图片或有效 Base64")

    def _clear_attachments(self):
        """清除聊天输入框里遗留的附件缩略图（兼容中/英文 UI）。"""
        try:
            self.page.js(
                "(function(){"
                "var btns=Array.from(document.querySelectorAll('button[aria-label]')).filter(function(b){"
                "var al=b.getAttribute('aria-label')||'';"
                "return /remove\\s*attachment|移除附件|删除附件|移除|删除/i.test(al);"
                "});"
                "btns.forEach(function(b){b.click();});"
                "Array.from(document.querySelectorAll('input[type=\"file\"]')).forEach(function(inp){inp.value='';});"
                "return btns.length;"
                "})()")
            time.sleep(0.3)
        except Exception:
            pass

    # 新版 muse 页面已没有 hatch-composer-placeholder-overlay，也不包 <form>：
    # 输入框所在容器是 textarea 往上第二层，附件的「Remove attachment」按钮在那里。
    # 旧结构（overlay / form）优先，新结构从 textarea 逐级向上找最近的一层。
    # patch22g3：closest('form') 可能命中一个 React 空壳 form（线上实证
    # form_len=0、composer 内容渲染在 form 外），此时必须回退逐级向上探索，
    # 否则 composer root 是空壳 → 附件按钮永远数到 0（假性上传失败）。
    _COMPOSER_ROOTS_JS = """
        function __composerRoots(){
            var ov = document.querySelector('[data-testid="hatch-composer-placeholder-overlay"]');
            var legacy = ov ? (ov.closest('form') || ov.parentElement.parentElement.parentElement) : null;
            if (legacy && legacy.innerHTML.length > 100) return [legacy];
            var ta = document.querySelector('textarea');
            if (!ta) return [];
            var form = ta.closest('form');
            if (form && form.innerHTML.length > 100) return [form];
            var out = [], el = ta.parentElement;
            for (var i = 0; i < 4 && el && el !== document.body; i++) { out.push(el); el = el.parentElement; }
            return out;
        }
    """

    _FILE_INPUT_JS = "(function(){" + _COMPOSER_ROOTS_JS + """
        var inputs = Array.from(document.querySelectorAll('input[type="file"]'));
        var roots = __composerRoots();
        for (var r of roots) {
            var hit = inputs.find(function(x){ return r.contains(x); });
            if (hit) return hit;
        }
        return null;
    })()"""

    _ATTACHED_COUNT_JS = "(function(){" + _COMPOSER_ROOTS_JS + """
        var rx = /remove\\s*attachment|移除附件|删除附件/i;
        var bubble = '[class*=chat-user-bubble], [class*="group/msg"], [class*=hatch-chat-groupable-bubble]';
        var roots = __composerRoots();
        for (var r of roots) {
            var n = Array.from(r.querySelectorAll('button[aria-label]')).filter(function(b){
                return rx.test(b.getAttribute('aria-label') || '') && !b.closest(bubble);
            }).length;
            if (n) return n;
        }
        return 0;
    })()"""

    def _set_input_files(self, paths: list[str]):
        """用 CDP 直接把磁盘文件交给上传控件（大视频不再经 base64 注入页面）。"""
        res = self.page.send("Runtime.evaluate", {
            "expression": self._FILE_INPUT_JS, "returnByValue": False})
        object_id = (res.get("result", {}).get("result", {}) or {}).get("objectId")
        if not object_id:
            raise MuseGenerationError("找不到上传控件，已停止生成")
        self.page.send("DOM.setFileInputFiles",
                       {"files": paths, "objectId": object_id}, timeout=60)

    def _attach_images(self, image_data: list[str]) -> dict | None:
        """逐个附加参考素材（图片/视频/音频/文档），并确认全部进入输入框。

        返回 {"dir": 临时目录, "hashes": 素材 sha256 集合}；调用方在生成结束后
        删除临时目录（Chromium 在发送时才读取文件内容），并用 hashes 防止把
        刚上传的素材当成生成结果交付。
        """
        if len(image_data) > MAX_REFERENCE_IMAGES:
            raise MuseGenerationError(f"参考素材最多 {MAX_REFERENCE_IMAGES} 个")
        files = []
        total_bytes = 0
        for index, value in enumerate(image_data):
            if not value:
                continue
            b64, mime = self._normalize_image(value)
            if not b64:
                raise MuseGenerationError(f"第 {index + 1} 个参考素材读取失败，已停止生成")
            try:
                data = base64.b64decode(b64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise MuseGenerationError(f"第 {index + 1} 个参考素材编码无效") from exc
            total_bytes += len(data)
            if total_bytes > MAX_REFERENCE_TOTAL_BYTES:
                raise MuseGenerationError(
                    f"参考素材总大小超过 {MAX_REFERENCE_TOTAL_BYTES // _MIB} MiB 限制")
            files.append((index + 1, data, mime))
        if not files:
            return None

        self._clear_attachments()
        # patch22g：composer 就绪门控——线上实证（patch22f 现场日志）DOM 可能
        # 存在 textarea + input[type=file] 但 composer 容器为空（form_len=0，
        # React 尚未把附件处理器挂到该 input 上），此时 setFileInputFiles 把
        # 文件设进无 handler 的 input，附件永远不出现（30s 超时假失败）。
        # 就绪判定 = composer root 内含 input[type=file] 且 root 内容非空。
        _composer_ready_js = "(function(){" + self._COMPOSER_ROOTS_JS + """
            var h = document.querySelector('[data-hatch-shell-hydration-state]');
            // patch22h：与 _wait_ws_ready 同语义 fail-closed——hydration 标记
            // 缺失 = React 事件委托未挂载，附件确认按钮永远数不到。
            if (!h || h.getAttribute('data-hatch-shell-hydration-state') !== 'hydrated') return false;
            var roots = __composerRoots();
            for (var r of roots) {
                if (r && r.querySelector('input[type=file]') && r.innerHTML.length > 200) return true;
            }
            return false;
        })()"""
        _ready_deadline = time.monotonic() + 15.0
        while time.monotonic() < _ready_deadline:
            try:
                if self.page.js(_composer_ready_js):
                    break
            except Exception:
                pass
            time.sleep(0.3)
        else:
            raise MuseGenerationError(
                "上传控件未就绪（composer 容器为空），已停止生成",
                code="composer_not_ready", stage="upload", retryable=True)
        # Snap Chromium cannot read the host's /tmp; profile_dir is shared with it.
        os.makedirs(self.cfg.profile_dir, exist_ok=True)
        upload_dir = tempfile.mkdtemp(prefix="muse2api-refs-", dir=self.cfg.profile_dir)
        hashes = set()
        try:
            for index, data, mime in files:
                path = os.path.join(
                    upload_dir, f"reference_{index}{REFERENCE_MIME_EXT.get(mime, '.bin')}")
                with open(path, "wb") as f:
                    f.write(data)
                hashes.add(hashlib.sha256(data).hexdigest())
                self._set_input_files([path])
                time.sleep(0.35)
        except MuseGenerationError:
            shutil.rmtree(upload_dir, ignore_errors=True)
            raise
        except Exception as e:
            shutil.rmtree(upload_dir, ignore_errors=True)
            raise MuseGenerationError("附加参考素材失败，已停止生成") from e

        try:
            # A remove button proves attachment presence, not upstream processing success.
            deadline = time.monotonic() + 30.0
            attached_count = 0
            _reset_at = {i: 0.0 for i in range(len(files))}  # patch22g：每文件上次重设时间
            while time.monotonic() < deadline:
                attached_count = self.page.js(self._ATTACHED_COUNT_JS)
                try:  # patch13(F15)：js 异常时返回 dict，int(dict) 会裸抛 TypeError
                    n_att = int(attached_count or 0)
                except (TypeError, ValueError):
                    n_att = 0
                if n_att >= len(files):
                    break
                # patch22g：附件 5s 未出现 → 重设文件（composer 可能刚完成挂载，
                # 首次 setFileInputFiles 打在了无 handler 的 input 上；重设会用
                # _FILE_INPUT_JS 现查现取，命中已挂载的真实 input）。最多每文件
                # 每 5s 一次，不放大 CDP 流量。
                now = time.monotonic()
                for i in range(n_att, len(files)):
                    if now - _reset_at[i] >= 5.0:
                        _reset_at[i] = now
                        try:
                            path_i = os.path.join(
                                upload_dir, "reference_%d%s"
                                % (i + 1, REFERENCE_MIME_EXT.get(files[i][2], '.bin')))
                            self._set_input_files([path_i])
                        except Exception:
                            pass
                        break
                time.sleep(0.3)
            else:
                # patch22d：上传确认失败属提交前失败——带 stage/code/retryable，
                # worker 据此落 safe_to_resubmit=True（重发零计费风险），并让
                # app 层 attempt 链能按提交前失败语义处理。
                # patch22f：失败现场取证（仅诊断日志，不含凭据）——page 状态、
                # file input、composer 按钮与 URL，用于定位 API 路径与 probe
                # 直连的差异（probe 同序列 attach 1.3s 成功）。
                try:
                    _diag = self.page.js("""(function(){                        var ta = document.querySelector('textarea');
                        var form = ta ? ta.closest('form') : null;
                        return JSON.stringify({
                            url: location.href, ready: document.readyState,
                            ta: !!ta,
                            n_file_inputs: document.querySelectorAll('input[type=file]').length,
                            remove_btns: Array.from((form||document.body).querySelectorAll('button[aria-label]'))
                                .map(function(b){return b.getAttribute('aria-label');})
                                .filter(function(a){return /remove|attachment/i.test(a||'');}),
                            form_len: form ? form.innerHTML.length : 0
                        });
                    })()""") or "{}"
                    log.warning("【上传确认失败现场】%s", str(_diag)[:500])
                except Exception as _ie:
                    log.warning("【上传确认失败现场】诊断采集失败: %s", str(_ie)[:120])
                raise MuseGenerationError(
                    f"参考素材上传未确认（{int(attached_count or 0)}/{len(files)}），已停止生成",
                    code="upload_unconfirmed", stage="upload", retryable=True)
            time.sleep(0.5)
            return {"dir": upload_dir, "hashes": hashes}
        except Exception:
            shutil.rmtree(upload_dir, ignore_errors=True)
            raise

    def _attach_image(self, image_data: str):
        """Backward-compatible single-reference wrapper."""
        self._attach_images([image_data])
    # ---------------- 主流程 ----------------
    def generate(self, cookies: dict, prompt: str, expect: str = "image",
                 timeout: int = 240, expires: dict | None = None, account_id: str | None = None,
                 on_progress=None, reference_image: str | None = None,
                 reference_images: list[str] | None = None,
                 stop_event=None, on_stage=None) -> dict:
        deadline = time.monotonic() + timeout
        # patch22：页面准备的有界重试——只重试 stage=page_prep（消息必然尚未
        # 提交、参考图尚未上传，重试零计费风险）；auth 属终态不重试。
        # 最多 2 次尝试，重试间隔随退避（2s），不掩盖首次失败的分类诊断。
        page_prep_tries = 2
        for prep_attempt in range(1, page_prep_tries + 1):
            _cb(on_stage, "page_prep")
            try:
                self.ensure_page(cookies, expires, account_id=account_id)
                break
            except MuseAuthError:
                raise
            except MuseGenerationError as exc:
                if (prep_attempt >= page_prep_tries
                        or getattr(exc, "stage", "") != "page_prep"):
                    raise
                log.warning("【页面准备重试】第 %d/%d 次失败（%s），退避后重开页面",
                            prep_attempt, page_prep_tries, str(exc)[:80])
                time.sleep(2.0 * prep_attempt)
        self.reset_thread(for_chat=False)
        self._scroll_bottom()
        _cb(on_stage, "upload")
        refs = list(reference_images or ([] if not reference_image else [reference_image]))
        upload = self._attach_images(refs) if refs else None
        if not refs:
            self._clear_attachments()
        try:
            return self._generate_attached(prompt, expect, deadline, on_progress,
                                           stop_event, (upload or {}).get("hashes") or set(),
                                           on_stage=on_stage)
        finally:
            if upload:
                shutil.rmtree(upload["dir"], ignore_errors=True)

    def _generate_attached(self, prompt: str, expect: str, deadline: float,
                           on_progress, stop_event, upload_hashes: set,
                           on_stage=None) -> dict:
        atts_before = self.attachments()
        baseline_sources = {a.get(k) for a in atts_before for k in ("src", "vSrc", "iSrc") if a.get(k)}
        base = atts_before[-1] if atts_before else {}
        baseline_src = base.get("src") or ""
        base_agent_cnt = self._agent_count()
        # patch12：收据基线必须在 _send 之前取（与 base_agent_cnt 同位），
        # 否则 send→wait 窗口内渲染的用户气泡会污染基线。
        base_user_cnt = self._user_count()
        if time.monotonic() >= deadline:
            raise MuseGenerationError("任务准备超时，已停止发送")
        _cb(on_stage, "submit")
        if self._send(prompt) not in ("clicked", "enter-sent"):
            raise MuseGenerationError("提示词发送未确认，已停止生成",
                                      code="send_unconfirmed", stage="submit", retryable=True)
        _cb(on_stage, "wait")
        os.makedirs(self.cfg.media_dir, exist_ok=True)
        # 页面若把刚上传的素材渲染成"结果"，按哈希识别出来并继续等待真正的成品
        while time.monotonic() < deadline:
            if stop_event is not None and stop_event.is_set():
                raise MuseGenerationError("任务已取消")
            att = self._wait_attachment(
                baseline_src, max(0, deadline - time.monotonic()), expect, on_progress=on_progress,
                base_agent_cnt=base_agent_cnt, base_user_cnt=base_user_cnt,
                base_att_cnt=len(atts_before),
                stop_event=stop_event, baseline_sources=baseline_sources,
                has_upload=bool(upload_hashes)
            )
            if not att:
                self._debug_dump("no-attachment")
                raise MuseGenerationError("等待生成超时，未出现新的生成结果", code="generation_wait_timeout", stage="wait", retryable=False)

            data = mime = url = None
            selected_src = (att.get("vSrc") if expect == "video" else None) or att.get("src") or ""
            _cb(on_stage, "extract")
            try:
                data, mime, url = self.extract_bytes(selected_src, expect=expect)
            except Exception:  # noqa: BLE001
                self._debug_dump("extract-fail")

            if data and hashlib.sha256(data).hexdigest() in upload_hashes:
                log.warning("命中的附件是本次上传的参考素材，忽略并继续等待: %s", selected_src[:80])
                baseline_sources |= {att.get(k) for k in ("src", "vSrc", "iSrc") if att.get(k)}
                time.sleep(0.1)
                continue

            if data:
                # patch14(F23)：数据在手先用魔数嗅探（优先于 mime/url 猜测）
                ext = self._sniff_ext(data, expect) or self._pick_ext(mime, url, expect)
                name = f"{uuid.uuid4().hex}{ext}"
                dst = os.path.join(self.cfg.media_dir, name)
                # patch10a：先校验后写盘——坏字节不落盘，media_dir 零残留
                kind = "video" if ext in (".mp4", ".webm", ".mov") else "image"
                self._verify_media_bytes(data, ext, expect)
                with open(dst, "wb") as f:
                    f.write(data)
                return {"path": dst, "filename": name, "size": len(data), "ext": ext, "mime": mime,
                        "kind": kind,
                        "via": "blob", "attachment": att.get("tid"),
                        "w": att.get("w"), "h": att.get("h")}

            path = self._download_fallback(selected_src)
            if not path:
                raise MuseGenerationError("已生成但未能取回文件", code="media_extract_failed", stage="extract", retryable=False)
            with open(path, "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest()
            if digest in upload_hashes:
                os.remove(path)
                log.warning("下载到的文件是本次上传的参考素材，忽略并继续等待")
                baseline_sources |= {att.get(k) for k in ("src", "vSrc", "iSrc") if att.get(k)}
                time.sleep(0.1)
                continue
            ext = os.path.splitext(path)[1].lower() or ".bin"
            name = f"{uuid.uuid4().hex}{ext}"
            dst = os.path.join(self.cfg.media_dir, name)
            shutil.move(path, dst)
            kind = "video" if ext in (".mp4", ".webm", ".mov") else "image"
            # patch10a：download 路径已 move 落盘，校验失败必须清理残留再抛
            try:
                with open(dst, "rb") as f:
                    self._verify_media_bytes(f.read(), ext, expect)
            except BaseException:
                try:
                    os.remove(dst)
                except OSError:
                    pass
                raise
            return {"path": dst, "filename": name, "size": os.path.getsize(dst), "ext": ext, "mime": "",
                    "kind": kind,
                    "via": "download", "attachment": att.get("tid"),
                    "w": att.get("w"), "h": att.get("h")}
        raise MuseGenerationError("页面只返回了本次上传的参考素材，未出现新的生成结果", code="no_native_media", stage="wait", retryable=False)

    # patch9：出片核验——容器魔数 + 最小体积 + expect 匹配。
    # 拦截上游静默替换参数/交付坏容器（HTML 错误页、零字节、图片冒充视频等）。
    _VIDEO_MAGICS = (b"ftyp", b"\x1a\x45\xdf\xa3", b"RIFF")  # mp4 / mkv-webm / avi-webm
    _IMAGE_MAGICS = (b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"RIFF")  # png/jpg/gif/webp

    @classmethod
    def _verify_media_bytes(cls, data: bytes, ext: str, expect: str) -> None:
        n = len(data)
        if n < 1024:
            raise MuseGenerationError(f"出片核验失败：文件过小 ({n}B)，疑似坏容器")
        head = data[:16]
        if ext in (".mp4", ".webm", ".mov"):
            if expect != "video":
                raise MuseGenerationError(f"出片核验失败：期望图片但得到视频容器 ({ext})")
            if not any(m in data[:64] for m in cls._VIDEO_MAGICS):
                raise MuseGenerationError(
                    f"出片核验失败：非视频容器魔数 (head={head.hex()[:24]}…)")
            # patch10a：时长 fail-closed——容器合法但解析不出有效正时长＝空轨坏片
            dur = _webm_duration(data) if ext == ".webm" else _mp4_duration(data)
            if dur is None or not math.isfinite(dur) or dur <= 0:
                raise MuseGenerationError(
                    f"出片核验失败：无法解析视频有效时长 (ext={ext})，疑似空轨坏片")
        elif ext in (".png", ".jpg", ".webp", ".gif"):
            if expect != "image":
                raise MuseGenerationError(f"出片核验失败：期望视频但得到图片 ({ext})，拒绝以图片冒充视频交付")
            if not any(m in head[:4] for m in cls._IMAGE_MAGICS):
                raise MuseGenerationError(
                    f"出片核验失败：非图片魔数 (head={head.hex()[:16]}…)")

    @staticmethod
    def _sniff_ext(data: bytes, expect: str) -> str | None:
        """patch14(F23)：数据魔数 → 扩展名。"""
        if b"ftyp" in data[:16]:
            return ".mp4"
        if b"\x1a\x45\xdf\xa3" in data[:16] or (
                b"RIFF" in data[:16] and expect == "video"):
            return ".webm"
        return None

    @staticmethod
    def _pick_ext(mime: str, url: str, expect: str) -> str:
        m = (mime or "").lower()
        for key, ext in (("mp4", ".mp4"), ("webm", ".webm"), ("png", ".png"),
                         ("jpeg", ".jpg"), ("jpg", ".jpg"), ("webp", ".webp"),
                         ("gif", ".gif")):
            if key in m:
                return ext
        for e in (".mp4", ".webm", ".png", ".jpg", ".webp"):
            if e in (url or "").lower():
                return e
        # patch14(F23)：mime/URL 都无法判定时按数据魔数嗅探，不盲选——
        # webm 内容标 .mp4 会在核验时按 mp4 容器解析失败烧掉一次重试。
        return ".mp4" if expect == "video" else ".png"

    @staticmethod
    def _debug_enabled() -> bool:
        # patch13(F16)：debug dump 默认关——tail 含用户 prompt 与模型回复明文，
        # 多用户场景下等于把用户内容留盘。MUSE2API_DEBUG_DUMP=1 显式开启。
        import os as _os
        return _os.environ.get("MUSE2API_DEBUG_DUMP", "0").strip() == "1"

    @staticmethod
    def _debug_enabled() -> bool:
        # patch13(F16)：debug dump 默认关——dump 含用户 prompt 与模型回复明文，
        # 多用户场景下等于把用户内容留盘。MUSE2API_DEBUG_DUMP=1 显式开启。
        return os.environ.get("MUSE2API_DEBUG_DUMP", "0").strip() == "1"

    def _debug_dump(self, tag: str):
        try:
            # patch13(F16)：tail（含用户 prompt 与回复明文）仅 debug 开启时收集
            tail_js = (',tail:document.body.innerText.slice(-500)'
                       if self._debug_enabled() else '')
            buttons_js = ("buttons:[...document.querySelectorAll('button,[role=button]')]"
                          ".filter(b=>b.offsetParent!==null)"
                          ".map(b=>b.getAttribute('aria-label')||b.innerText.trim().slice(0,20))"
                          ".filter(Boolean).slice(-40)"
                          if self._debug_enabled() else '')
            sep = ',' if (tail_js or buttons_js) else ''
            info = self.page.js(
                "JSON.stringify({atts:[...document.querySelectorAll('" + ATT_SEL + "')]"
                ".map(function(a){var m=a.querySelector('img,video');return {"
                "tid:a.getAttribute('data-testid'),"
                "src:m?(m.currentSrc||m.src||'').slice(0,60):''};})"
                + buttons_js + sep + tail_js + "})")
            with open(os.path.join(self.cfg.data_dir, f"debug-{tag}.json"), "w",
                      encoding="utf-8") as f:
                f.write(str(info))
        except Exception:  # noqa: BLE001
            pass
