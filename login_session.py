"""patch27 — 远程浏览器导号会话（CDP 串流形态）。

用户打开 /admin/remote-login?token=m2a_imp_xxx → 本模块在 muse2api 容器内
的 chromium 里创建一个带独立出口的隔离 context，把画面经 CDP
Page.startScreencast 串流给用户浏览器，用户键鼠经 WebSocket 回传 CDP
Input.dispatch*，直接在服务器浏览器里登录 muse.ai。cookie 天然落在服务器。

与生成链路的关系：
  - 复用 engine.browser（同一 chromium 进程，省内存），但 context 完全独立
    （独立 proxy / 存储 / cookie），生命周期本模块自管，绝不进入引擎的
    _local.page 复用键与 close_current_page 状态机。
  - 出口：登录动作走 premium_only（账号一生风控最敏感时刻用优质 IP），
    一号一 IP；登录成功后该 proxy/binding 直接写账号档案，后续生成同源 IP。

只依赖标准库 + websocket-client（经 cdp.CDP）。
"""
from __future__ import annotations

import threading
import time
import uuid

from cdp import CDP, CDPError
from proxy import HTTPProxyRelay

SITE = "https://muse.ai/"
LOGIN_TIMEOUT = 15 * 60          # 会话最长存活（与导入令牌 TTL 对齐再留余量）
SCREEN_JPEG_QUALITY = 60
SCREEN_MAX_WIDTH = 1280
PROBE_INTERVAL = 3.0             # 登录态探测间隔（服务端真值判定）


class RemoteLoginSession:
    """一次远程导号会话：隔离 context + 串流 + 登录检测 + cookie 抓取。

    生命周期：start() → (线程 pump_ws 串流) → finish()/cleanup()。
    全程不落任何 cookie 明文到磁盘；日志不回显代理凭据。
    """

    def __init__(self, aid: str, label: str):
        self.aid = aid
        self.label = label
        self.created_at = time.time()
        self.deadline = self.created_at + LOGIN_TIMEOUT
        self.proxy: dict | None = None       # TP.acquire 结果（含 _binding）
        self.context_id: str | None = None
        self.target_id: str | None = None
        self.page: CDP | None = None
        self.relay: HTTPProxyRelay | None = None
        self.pump_thread: threading.Thread | None = None
        self.stop_evt = threading.Event()
        self.logged_in = False
        self.finished = False
        self.error: str | None = None
        self._frame_seq = 0
        self._last_probe = 0.0
        self.on_login_probe = None           # 测试钩子：注入替代 _probe_logged_in

    # ------------------------------------------------------------ 生命周期
    def start(self, engine) -> None:
        """建隔离 context（带 premium 出口）并开始串流。

        engine: app.engine（MuseEngine 实例，提供 .browser / .cfg.cdp_port）。
        TP 由调用方（app 层）先 acquire 好塞进 self.proxy。
        """
        if engine.browser is None:
            raise RuntimeError("浏览器未就绪，无法创建远程登录会话")
        proxy = self.proxy
        if proxy:
            self.relay = HTTPProxyRelay(proxy)
            res = engine.browser.send("Target.createBrowserContext", {
                "proxyServer": self.relay.url,
                "proxyBypassList": "<-loopback>",
            })
        else:
            res = engine.browser.send("Target.createBrowserContext")
        self.context_id = (res.get("result") or {}).get("browserContextId")
        if not self.context_id:
            raise RuntimeError("Chromium 未返回远程登录上下文 ID")
        t = engine.browser.send("Target.createTarget", {
            "url": SITE, "browserContextId": self.context_id,
        })
        self.target_id = (t.get("result") or {}).get("targetId")
        if not self.target_id:
            self.cleanup(engine)
            raise RuntimeError("Chromium 未返回远程登录页面 ID")
        ws_url = f"ws://127.0.0.1:{engine.cfg.cdp_port}/devtools/page/{self.target_id}"
        self.page = CDP(ws_url, timeout=120)
        self.page.send("Page.enable")
        self.page.send("Runtime.enable")
        self.page.send("Page.startScreencast", {
            "format": "jpeg", "quality": SCREEN_JPEG_QUALITY,
            "maxWidth": SCREEN_MAX_WIDTH, "everyNthFrame": 1,
        })
        self.pump_thread = threading.Thread(
            target=self._pump_loop, name=f"rlogin-{self.aid[:8]}", daemon=True)
        self.pump_thread.start()

    def _pump_loop(self):
        """CDP 事件泵线程：screencastFrame → on_frame 回调（WS 发送方）。"""
        try:
            self.page.pump_ws(on_frame=self._on_screencast_frame,
                              stop=self.stop_evt, sock_timeout=0.5)
        except CDPError as exc:
            self.error = f"浏览器连接断开：{str(exc)[:80]}"
            self.stop_evt.set()

    def _on_screencast_frame(self, params: dict) -> None:
        """screencastFrame 回调——由 app 层注入（把 base64 JPEG 经 WS 推前端）。

        ackPageFrame 在 cdp.pump_ws 里已回。
        """
        self._frame_seq += 1
        cb = getattr(self, "_frame_sink", None)
        if cb:
            cb(params.get("data"), params.get("metadata") or {})

    # ------------------------------------------------------------ 输入转发
    def dispatch_input(self, msg: dict) -> None:
        """把前端 WS 消息转发成 CDP Input.dispatch*。msg 形如
        {"t":"mouse","x":..,"y":..,"type":"mousePressed","button":"left",...}
        {"t":"key","type":"keyDown",...,"text":"a"}
        {"t":"insert","text":"..."} （整段粘贴——密码框友好）
        {"t":"viewport","w":..,"h":..} （resize 模拟）
        {"t":"goto","url":"..."} （控制页「发送登录链接」——magic link 是 URL，
        Input.insertText 只能塞输入框不能导航；限 muse.ai 域，防令牌持有者
        把服务器浏览器当通用跳板）
        """
        if not self.page:
            return
        t = msg.get("t")
        try:
            if t == "mouse":
                self.page.send("Input.dispatchMouseEvent", {
                    "type": msg.get("type") or "mouseMoved",
                    "x": float(msg.get("x", 0)), "y": float(msg.get("y", 0)),
                    "button": msg.get("button") or "none",
                    "clickCount": int(msg.get("clickCount") or 0),
                })
            elif t == "wheel":
                self.page.send("Input.dispatchMouseEvent", {
                    "type": "mouseWheel",
                    "x": float(msg.get("x", 0)), "y": float(msg.get("y", 0)),
                    "deltaX": float(msg.get("deltaX", 0)),
                    "deltaY": float(msg.get("deltaY", 0)),
                })
            elif t == "key":
                params = {"type": msg.get("type") or "keyDown",
                          "key": msg.get("key") or "", "code": msg.get("code") or ""}
                if msg.get("text"):
                    params["text"] = msg["text"]
                if msg.get("modifiers"):
                    params["modifiers"] = int(msg["modifiers"])
                self.page.send("Input.dispatchKeyEvent", params)
            elif t == "insert":
                self.page.send("Input.insertText", {"text": msg.get("text") or ""})
            elif t == "goto":
                url = (msg.get("url") or "").strip()
                # patch27e：只放行 muse.ai（含子域）http(s)——magic link 形如
                # https://muse.ai/auth/... ；其余一律拒绝（登录页可能弹出
                # facebook/google OAuth，但 premium 出口到不了那些域，见
                # 2026-11-23 实弹；邮箱 magic link 全程不出 muse.ai 域）。
                from urllib.parse import urlsplit
                try:
                    parts = urlsplit(url)
                except ValueError:
                    parts = None
                if (not parts or parts.scheme not in ("http", "https")
                        or not (parts.hostname or "").endswith("muse.ai")):
                    self.error = "登录链接被拒绝：仅支持 muse.ai 域"
                    return
                self.page.send("Page.navigate", {"url": url})
            elif t == "viewport":
                self.page.send("Emulation.setDeviceMetricsOverride", {
                    "width": int(msg.get("w") or 1280),
                    "height": int(msg.get("h") or 800),
                    "deviceScaleFactor": 1, "mobile": False,
                })
        except Exception as exc:                       # noqa: BLE001
            # 输入失败不炸会话（页面可能在跳转中），记最后一次错误供诊断
            self.error = f"输入转发失败：{type(exc).__name__}"

    # ------------------------------------------------------------ 登录检测
    def check_login(self, engine) -> bool:
        """轮询登录态：Storage.getCookies 拿到 hatch_sess 且 /api/session 200。

        带独立限频（PROBE_INTERVAL）；探测失败（网络抖动）不判失败，下轮再试。
        """
        if not self.page or self.finished:
            return False
        now = time.time()
        if now - self._last_probe < PROBE_INTERVAL:
            return self.logged_in
        self._last_probe = now
        probe = self.on_login_probe or self._probe_logged_in
        try:
            self.logged_in = bool(probe(engine))
        except Exception:                              # noqa: BLE001
            pass                                       # 下轮再试
        return self.logged_in

    def _probe_logged_in(self, engine) -> bool:
        cookies = self.get_cookies()
        if "hatch_sess" not in cookies:
            return False
        # 服务端真值：带 context cookie 走同一 premium 出口直查 /api/session
        # （200=有效；对齐 engine.renew_session_http 的请求指纹）。
        header = "; ".join(f"{k}={v}" for k, v in cookies.items() if v)
        import requests
        proxies = {"http": self.relay.url, "https": self.relay.url} if self.relay else None
        r = requests.get(
            SITE + "api/session",
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
                     "Origin": "https://muse.ai", "Referer": "https://muse.ai/",
                     "Cookie": header},
            proxies=proxies, timeout=15)
        return r.status_code == 200

    # ------------------------------------------------------------ cookie
    def get_cookies(self) -> dict[str, str]:
        """本 context 的 muse.ai cookie（含 httpOnly）。只返回名值，不落盘。"""
        if not self.page:
            return {}
        res = self.page.send("Storage.getCookies", {"browserContextId": self.context_id})
        out: dict[str, str] = {}
        for c in (res.get("result") or {}).get("cookies", []):
            if "muse.ai" in (c.get("domain") or "").lstrip(".") and c.get("value"):
                out[c["name"]] = c["value"]
        return out

    def cookie_details(self) -> dict[str, dict]:
        """入库用完整 cookie 档案（value/expires/httpOnly）。"""
        if not self.page:
            return {}
        res = self.page.send("Storage.getCookies", {"browserContextId": self.context_id})
        out: dict[str, dict] = {}
        for c in (res.get("result") or {}).get("cookies", []):
            if "muse.ai" not in (c.get("domain") or "").lstrip("."):
                continue
            if not c.get("name") or not c.get("value"):
                continue
            try:
                exp = int(float(c.get("expires", -1)))
            except (TypeError, ValueError):
                exp = -1
            out[c["name"]] = {"value": c["value"], "expires": exp,
                              "httpOnly": bool(c.get("httpOnly"))}
        return out

    # ------------------------------------------------------------ 收尾
    def finish(self, engine) -> None:
        """正常收尾：停泵 → 停串流 → dispose context。"""
        if self.finished:
            return
        self.finished = True
        self.stop_evt.set()
        try:
            if self.page:
                try:
                    self.page.send("Page.stopScreencast")
                except Exception:                      # noqa: BLE001
                    pass
                self.page.close()
        finally:
            self._dispose_context(engine)
            if self.proxy:
                # 保留绑定（登录 IP 将成为账号持久出口），不 release；
                # 由 app 层写账号档案。 relay 只需停线程。
                pass
            self.relay = None
            self.page = None

    def cleanup(self, engine) -> None:
        """异常/超时收尾：dispose context + release 出口（不留悬空绑定）。"""
        self.finished = True
        self.stop_evt.set()
        self._dispose_context(engine)
        if self.proxy and self.aid:
            try:
                from tetherpool import TP
                TP.release(self.aid)
            except Exception:                          # noqa: BLE001
                pass
        self.relay = None
        self.page = None

    def _dispose_context(self, engine) -> None:
        if self.context_id and getattr(engine, "browser", None):
            try:
                engine.browser.send("Target.disposeBrowserContext",
                                    {"browserContextId": self.context_id})
            except Exception:                          # noqa: BLE001
                pass
        self.context_id = None

    # ------------------------------------------------------------ 状态
    def expired(self) -> bool:
        return time.time() > self.deadline

    def snapshot(self) -> dict:
        """管理页/日志用状态快照（无敏感字段）。"""
        return {
            "aid": self.aid, "label": self.label,
            "age_s": int(time.time() - self.created_at),
            "remaining_s": max(0, int(self.deadline - time.time())),
            "logged_in": self.logged_in, "frames": self._frame_seq,
            "error": self.error,
            "node": (self.proxy or {}).get("_binding", {}).get("node_id"),
        }


class _null_ctx:
    """占位（patch26b review 同款模式）：保留空上下文管理器以兼容旧调用点。"""

    def __enter__(self):
        return None

    def __exit__(self, *a):
        return False


def new_login_aid() -> str:
    """预分配新号 ID（uuid4 hex 12 位，与既有号 id 形态一致）。"""
    return uuid.uuid4().hex[:12]
