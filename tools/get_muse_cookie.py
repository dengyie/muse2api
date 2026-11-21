#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""muse2api Cookie 助手 —— 一键把 muse.ai 的登录 cookie 导入账号池。

为什么需要这个脚本？
    muse.ai 的关键会话 cookie（hatch_sess / hatch_vml）带 httpOnly 标记，
    网页里的 document.cookie 读不到它们。只有从浏览器底层
    （Chrome DevTools Protocol）才能读到。hatch_gw 是可重新签发的路由缓存。
    所以没法用「小书签」或「控制台一行代码」解决，必须让脚本驱动浏览器。

它是怎么工作的？
    1. 用一个按 --label 命名的持久配置目录启动一个独立 Chrome 窗口
       （不影响你日常用的浏览器；每个号一个 profile，登录态长期保留）
    2. 首次导某号：你在那个窗口里登录 muse.ai，登录一次就够了
    3. 之后任何场景重跑同一 --label：脚本探测到会话仍有效 → 零交互，
       几秒内自动抓取上传直接关窗
    4. 脚本轮询读取 cookie 并验证 /api/session 登录态，一有效就自动上传

用法：
    python get_muse_cookie.py --base http://your-server-ip:18610 --token m2a_imp_xxx
    python get_muse_cookie.py --base http://your-server-ip:18610 --key m2a_xxx
    python get_muse_cookie.py --base http://your-server-ip:18610 --key m2a_xxx --label acc-01

推荐用 --token（管理页「生成一次性导入令牌」给出，单次有效，不暴露完整管理 Key）。
--label 强烈建议固定（如 acc-01）：profile 目录按它命名，固定 label = 该号只登录一次。
换号 / 强制重新登录时加 --fresh（清空该 label 的 profile，重新登录一次）。

也可以先用环境变量，省得每次敲：
    set MUSE2API_BASE=http://your-server-ip:18610     (Windows)
    set MUSE2API_KEY=m2a_xxx
    export MUSE2API_BASE=...                          (macOS / Linux)
    export MUSE2API_KEY=...

只依赖 Python 标准库（Python 3.8+），不需要 pip install 任何东西。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

SITE = "https://muse.ai/"
CDP_PORT = 9333
ESSENTIAL = ("hatch_sess",)
DOMAIN_HINT = "muse.ai"


# ---------------------------------------------------------------- 输出
def say(msg=""):
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:                     # 老 Windows 控制台
        print(msg.encode("utf-8", "replace").decode("utf-8", "replace"), flush=True)


def init_console():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")   # Python 3.7+
        except Exception:                          # noqa: BLE001
            pass


# ---------------------------------------------------------------- 极简 WebSocket
class WS:
    """只为 CDP 用的最小 WebSocket 客户端（RFC6455，文本帧）。"""

    def __init__(self, url: str, timeout: float = 15.0):
        assert url.startswith("ws://"), url
        rest = url[5:]
        hostport, _, path = rest.partition("/")
        path = "/" + path
        host, _, port = hostport.partition(":")
        port = int(port or 80)

        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\n"
               f"Host: {host}:{port}\r\n"
               "Upgrade: websocket\r\n"
               "Connection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("WebSocket 握手被断开")
            buf += chunk
        status = buf.split(b"\r\n", 1)[0]
        if b" 101" not in status:
            raise ConnectionError(f"WebSocket 握手失败: {status!r}")
        self._id = 0

    # --- 发送 ---
    def _frame(self, opcode: int, payload: bytes):
        head = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", n)
        mask = os.urandom(4)
        head += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(head) + masked)

    def send_text(self, text: str):
        self._frame(0x1, text.encode("utf-8"))

    # --- 接收 ---
    def _read(self, n: int) -> bytes:
        out = b""
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise ConnectionError("连接已关闭")
            out += chunk
        return out

    def recv_text(self) -> str:
        while True:
            h = self._read(2)
            opcode = h[0] & 0x0F
            masked = h[1] & 0x80
            ln = h[1] & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._read(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._read(8))[0]
            mk = self._read(4) if masked else None
            data = self._read(ln) if ln else b""
            if mk:
                data = bytes(b ^ mk[i % 4] for i, b in enumerate(data))
            if opcode == 0x1:
                return data.decode("utf-8", "replace")
            if opcode == 0x9:                       # ping -> pong
                self._frame(0xA, data)
            elif opcode == 0x8:
                raise ConnectionError("服务端关闭了连接")
            # 其它（pong / 二进制 / 分片）直接忽略

    def call(self, method: str, params: dict | None = None,
             timeout: float = 20.0) -> dict:
        self._id += 1
        mid = self._id
        self.send_text(json.dumps({"id": mid, "method": method,
                                   "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                msg = json.loads(self.recv_text())
            except socket.timeout:
                continue
            except ConnectionError:
                raise
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
        raise TimeoutError(f"CDP {method} 超时")

    def close(self):
        try:
            self.sock.close()
        except Exception:                              # noqa: BLE001
            pass


# ---------------------------------------------------------------- 找浏览器
CHROME_EXES = ("chrome.exe", "msedge.exe", "brave.exe", "chromium.exe")


def _win_registry_paths() -> list[str]:
    """从注册表 App Paths 找浏览器。

    不能靠 %ProgramFiles% 环境变量 —— 它在某些 shell / 精简环境下并不存在
    （本机实测 ProgramFiles 和 PROGRAMFILES 都是 None）。
    """
    out: list[str] = []
    try:
        import winreg
    except ImportError:
        return out
    sub = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths")
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for exe in CHROME_EXES:
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(root, sub + "\\" + exe, 0,
                                        winreg.KEY_READ | view) as k:
                        val = winreg.QueryValueEx(k, "")[0]
                        if val:
                            out.append(os.path.expandvars(val))
                except OSError:
                    continue
    return out


def _win_common_paths() -> list[str]:
    roots = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
             os.environ.get("LOCALAPPDATA"), r"C:\Program Files",
             r"C:\Program Files (x86)"]
    rel = [
        r"Google\Chrome\Application\chrome.exe",
        r"Microsoft\Edge\Application\msedge.exe",
        r"BraveSoftware\Brave-Browser\Application\brave.exe",
    ]
    return [os.path.join(r, p) for r in roots if r for p in rel]


def find_chrome() -> str | None:
    if sys.platform == "win32":
        cands = _win_registry_paths() + _win_common_paths()
    elif sys.platform == "darwin":
        cands = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        ]
    else:
        cands = ["google-chrome", "google-chrome-stable", "chromium",
                 "chromium-browser", "microsoft-edge", "brave-browser"]
    seen = set()
    for c in cands:
        if not c or c in seen:
            continue
        seen.add(c)
        if os.path.isfile(c):
            return c
        if not os.path.isabs(c):
            w = shutil.which(c)
            if w:
                return w
    return None


def wait_cdp(port: int, timeout: float = 30.0) -> str:
    """等 DevTools 端口起来，返回浏览器级 WebSocket 地址。"""
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/json/version"
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                return json.load(r)["webSocketDebuggerUrl"]
        except Exception as exc:                       # noqa: BLE001
            last = exc
            time.sleep(0.5)
    raise RuntimeError(f"浏览器调试端口没起来（{port}）：{last}")


# ---------------------------------------------------------------- 读 cookie
def read_cookies(ws: WS) -> dict[str, dict]:
    """用浏览器级 Storage.getCookies 拿全部 cookie，再筛 muse.ai。

    这是关键：CDP 能读到 httpOnly 的 cookie，网页 JS 读不到。
    """
    res = ws.call("Storage.getCookies", {}, timeout=20)
    out: dict[str, dict] = {}
    for c in res.get("cookies", []):
        domain = (c.get("domain") or "").lstrip(".")
        if DOMAIN_HINT not in domain:
            continue
        name = c.get("name")
        if not name:
            continue
        try:
            exp = int(float(c.get("expires", -1)))
        except (TypeError, ValueError):
            exp = -1
        out[name] = {"value": c.get("value", ""), "expires": exp,
                     "httpOnly": bool(c.get("httpOnly"))}
    return out


# ---------------------------------------------------------------- 上传
def upload(base: str, key: str, label: str, cookies: dict[str, dict]) -> dict:
    payload = {
        "label": label,
        "cookies": {k: v["value"] for k, v in cookies.items()},
        "expires": {k: v["expires"] for k, v in cookies.items() if v["expires"] > 0},
    }
    req = urllib.request.Request(
        base.rstrip("/") + "/admin/accounts",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        if e.code == 401:
            detail += "（401：令牌/Key 无效或已用过——一次性令牌导入成功即失效，请回管理页重新生成）"
        elif e.code == 409:
            detail += "（409：这个 muse.ai 号已经在账号池里了，无需重复导入）"
        raise RuntimeError(f"上传失败 HTTP {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"连不上 {base}：{e.reason}") from None


# ---------------------------------------------------------------- 登录态探测
def session_ok(cookies: dict[str, dict]) -> bool:
    """带抓到的 cookie 请求 /api/session，200 = 登录态有效。

    服务端真值判定，不猜页面跳转：匿名请求实测返回
    401 {"error":"Authentication required"}，带有效会话则 200。
    """
    if "hatch_sess" not in cookies:
        return False
    header = "; ".join(f"{k}={v['value']}" for k, v in cookies.items())
    req = urllib.request.Request(SITE + "api/session",
                                 headers={"Cookie": header,
                                          "User-Agent": "muse2api-cookie-helper"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200
    except Exception:                              # noqa: BLE001  401/网络错=未登录
        return False


def fmt_ts(ts: int) -> str:
    if ts <= 0:
        return "会话级"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


# ---------------------------------------------------------------- 主流程
def main() -> int:
    init_console()
    ap = argparse.ArgumentParser(
        description="把 muse.ai 的登录 cookie 一键导入 muse2api 账号池")
    ap.add_argument("--base", default=os.environ.get("MUSE2API_BASE", ""),
                    help="muse2api 地址，如 http://your-server-ip:18610")
    ap.add_argument("--key", default=os.environ.get("MUSE2API_KEY", ""),
                    help="API Key（m2a_ 开头）")
    ap.add_argument("--token", default=os.environ.get("MUSE2API_IMPORT_TOKEN", ""),
                    help="一次性导入令牌（m2a_imp_ 开头，管理页生成，单次有效）")
    ap.add_argument("--label", default="", help="账号标签，如 acc-01（同时决定持久 profile 目录，建议固定）")
    ap.add_argument("--timeout", type=int, default=300,
                    help="等待登录的最长秒数，默认 300")
    ap.add_argument("--keep-open", action="store_true",
                    help="上传成功后不关闭浏览器窗口")
    ap.add_argument("--fresh", action="store_true",
                    help="清空该 label 的持久 profile（换号 / 强制重新登录时用）")
    ap.add_argument("--port", type=int, default=CDP_PORT,
                    help=f"调试端口，默认 {CDP_PORT}（多号并行导入时各用不同端口）")
    ap.add_argument("--chrome", default="", help="手动指定浏览器可执行文件路径")
    args = ap.parse_args()

    say("=" * 66)
    say("  muse2api Cookie 助手")
    say("=" * 66)
    say()

    if not args.base or not (args.token or args.key):
        say("✗ 缺少参数。请这样运行：")
        say("    python get_muse_cookie.py --base https://你的域名 --token m2a_imp_xxx")
        say("  （命令由管理页「生成一次性导入令牌」直接给出，复制即可）")
        say("  或者用完整 API Key：")
        say("    python get_muse_cookie.py --base https://你的域名 --key m2a_xxx")
        return 2
    if not args.label:
        args.label = "muse-" + time.strftime("%m%d-%H%M")
    if args.token:
        say(f"使用一次性导入令牌（{args.token[:12]}…，导入成功即失效）")

    chrome = args.chrome or find_chrome()
    if not chrome:
        say("✗ 没找到 Chrome / Edge 浏览器。")
        say("  请先安装 Chrome，或用 --chrome \"完整路径\" 手动指定。")
        return 3
    say(f"浏览器：{chrome}")

    # 持久 profile：按 label 分目录，登录态长期保留（patch26 扩号链路优化）。
    # 不再每次 rmtree——同一 label 重跑时 Chrome 直接复用已有会话，零交互直取。
    # cookie 由 Chrome profile 自己加密存储，不经过任何明文落盘。
    safe_label = "".join(c for c in args.label if c.isalnum() or c in "-_") or "default"
    profile = os.path.join(tempfile.gettempdir(), "muse2api-cookie-profiles", safe_label)
    if args.fresh:
        shutil.rmtree(profile, ignore_errors=True)
        say(f"已清空 {safe_label} 的持久 profile，将重新登录。")
    os.makedirs(profile, exist_ok=True)
    first_time = not os.path.isdir(os.path.join(profile, "Default")) \
        and not os.listdir(profile)

    args_cmd = [
        chrome,
        f"--remote-debugging-port={args.port}",
        f"--user-data-dir={profile}",
        "--no-first-run", "--no-default-browser-check",
        # 受限环境（容器 / 沙箱 / CI）里 Chrome 自带的沙箱会初始化失败：
        #   "sandbox initialization failed: Operation not permitted"
        # → GPU/网络子进程崩掉 → Chrome 直接退出，调试端口从未监听起来，
        #   表现是 WebSocket 一握手就 "连接已关闭 / Broken pipe"。
        # 加下面两个开关可绕过。日常桌面环境无副作用（只是不启用 Chrome 自带沙箱）。
        "--no-sandbox", "--disable-gpu",
        "--new-window", SITE,
    ]
    say("正在打开一个独立的浏览器窗口（不会动你日常用的浏览器）…")
    proc = subprocess.Popen(args_cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)

    ws = None
    try:
        try:
            ws_url = wait_cdp(args.port, 30)
        except RuntimeError as exc:
            say(f"✗ {exc}")
            say("  提示：如果浏览器已经开着，请先完全退出再试一次。")
            say("  （日常 Chrome 在跑时，新窗口会被它接管，调试端口起不来。）")
            return 4
        ws = WS(ws_url)

        say()
        if first_time:
            say("┌" + "─" * 64 + "┐")
            say("│  首次导入该号：请在弹出的浏览器窗口里登录 muse.ai           │")
            say("│  （登录一次即可，profile 会保留登录态，之后重跑零交互）      │")
            say("│  登录成功、能看到聊天界面后，脚本会自动抓取，无需其它操作。  │")
            say("└" + "─" * 64 + "┘")
        else:
            say("  检测到该号的持久 profile——若之前已登录，会自动直取无需操作。")
        say()

        deadline = time.time() + args.timeout
        got: dict[str, dict] = {}
        last_note = 0.0
        while time.time() < deadline:
            try:
                got = read_cookies(ws)
            except Exception as exc:                    # noqa: BLE001
                say(f"  读取 cookie 出错，重试中…（{exc}）")
                time.sleep(2)
                continue

            have = [n for n in ESSENTIAL if n in got]
            if len(have) == len(ESSENTIAL):
                # patch26：cookie 在手 + 服务端确认登录态才动手。旧的失效
                # cookie 挂在 profile 里时单看「有 hatch_sess」会误判完成。
                if session_ok(got):
                    break
                now = time.time()
                if now - last_note > 10:
                    last_note = now
                    left = int(deadline - now)
                    say(f"  有旧 cookie 但会话已失效，请在窗口里重新登录（剩余 {left}s）")
            else:
                now = time.time()
                if now - last_note > 5:
                    last_note = now
                    left = int(deadline - now)
                    say(f"  等待登录… 已拿到 {len(have)}/{len(ESSENTIAL)} 条核心 cookie"
                        f"（{', '.join(have) or '无'}）剩余 {left}s")
            time.sleep(2)
        else:
            say()
            say("✗ 等待超时，还没登录成功。")
            say("  请重跑一次脚本，并在窗口里完成登录。")
            return 5

        say()
        say("✓ 已拿到完整会话 cookie：")
        say()
        say(f"  {'cookie 名':<30}{'httpOnly':<10}{'有效期'}")
        say("  " + "-" * 62)
        for name in ESSENTIAL:
            c = got.get(name, {})
            say(f"  {name:<30}{('是' if c.get('httpOnly') else '否'):<10}"
                f"{fmt_ts(c.get('expires', -1))}")
        say()

        say("正在上传到 muse2api…")
        try:
            r = upload(args.base, args.token or args.key, args.label, got)
        except RuntimeError as exc:
            say(f"✗ {exc}")
            say()
            say("  cookie 已经抓到了，但上传失败。你可以手动复制下面这行，")
            say("  粘贴到管理页面的「导入账号」输入框：")
            say()
            say("  " + "; ".join(f"{k}={v['value']}" for k, v in got.items()))
            return 6

        say()
        say("=" * 66)
        say(f"✓ 导入成功！账号标签：{r['added'][0]['label']}")
        say(f"  账号 ID：{r['added'][0]['id']}")
        say(f"  共 {r['added'][0]['cookie_count']} 条 cookie")
        if r.get("warning"):
            say(f"  ⚠ {r['warning']}")
        say()
        say(f"  打开 {args.base.rstrip('/')}/ 就能在账号池里看到它。")
        say("=" * 66)
        return 0

    finally:
        if ws:
            ws.close()
        if not args.keep_open:
            try:
                proc.terminate()
            except Exception:                           # noqa: BLE001
                pass
        # patch26：持久 profile 保留（登录态复用的本钱），只有 --fresh 才清。
        # 用 --keep-open 时浏览器还开着，profile 正被占用更不能动。


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say()
        say("已取消。")
        sys.exit(130)
