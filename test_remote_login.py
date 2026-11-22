# -*- coding: utf-8 -*-
"""test_remote_login — patch27 远程浏览器导号回归（真实 TestClient 模式）。

覆盖：
  1. 令牌机制扩展：mode 标记（cli/url）、跨用途单活跃互斥、_import_token_mode
  2. POST /admin/import-login-url：KEY 鉴权 / 返回 URL 含令牌 / label_hint 透传
  3. GET /admin/remote-login：URL 令牌可开页 / CLI 令牌 403 防串用 /
     非活跃令牌 401 / 错误令牌 401（防枚举同口径）
  4. POST /admin/accounts：URL 令牌不能当 CLI 导入令牌用（mode 隔离）
  5. store.add_account(aid=...)：预分配 ID 落库（TP 绑定同键）、重复 aid 冲突语义
  6. cdp.pump_ws / CDP.send 并发：响应帧经 _pending 队列回填不互吞
  7. RemoteLoginSession：snapshot 无敏感字段、expired 边界、
     check_login 限频、cookie_details 筛 muse.ai + expires 归整
"""
import json
import os
import sys
import tempfile
import time

_here = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="m2a-p27-test-")
os.environ["MUSE2API_HOME"] = TMP
sys.path.insert(0, _here)

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print("FAIL: %s %s" % (name, detail))


import warnings
warnings.filterwarnings("ignore")
import app  # noqa: E402
import store as store_mod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app.app)
ADMIN = {"Authorization": "Bearer testkey123"}
app.CFG.api_key = "testkey123"


def cleanup_accounts():
    with store_mod._LOCK:
        app.store.accounts = []


# ---------------- 1. 令牌 mode 标记与单活跃互斥 ----------------
print("== 1. 令牌 mode 标记 ==")
app._IMPORT_TOKENS.clear()
t_cli = "m2a_imp_" + "a" * 32
t_url = "m2a_imp_" + "b" * 32
h = app._import_token_hash
app._IMPORT_TOKENS[h(t_cli)] = {"expires": time.monotonic() + 600, "label_hint": "", "mode": "cli"}
app._IMPORT_TOKENS[h(t_url)] = {"expires": time.monotonic() + 600, "label_hint": "", "mode": "url"}
check("1.1 mode=cli 识别", app._import_token_mode(t_cli) == "cli")
check("1.2 mode=url 识别", app._import_token_mode(t_url) == "url")
check("1.3 非活跃令牌 mode=None",
      app._import_token_mode("m2a_imp_" + "c" * 32) is None)
app._IMPORT_TOKENS[h(t_dead)] = {"expires": time.monotonic() - 1, "mode": "url"} \
    if (t_dead := "m2a_imp_" + "d" * 32) else None
check("1.4 过期令牌 mode=None", app._import_token_mode(t_dead) is None)

# 旧令牌无 mode 字段（容器热升级兼容）→ 默认 cli
app._IMPORT_TOKENS[h("m2a_imp_" + "e" * 32)] = {"expires": time.monotonic() + 600}
check("1.5 无 mode 默认 cli", app._import_token_mode("m2a_imp_" + "e" * 32) == "cli")

# ---------------- 2. POST /admin/import-login-url ----------------
print("== 2. import-login-url 端点 ==")
r = client.post("/admin/import-login-url", json={"label": "acc-02"})
check("2.1 无 KEY 401", r.status_code == 401, r.status_code)
r = client.post("/admin/import-login-url", json={"label": "acc-02"}, headers=ADMIN)
check("2.2 200", r.status_code == 200, r.text[:120])
d = r.json()
check("2.3 url 含令牌与路径", "/admin/remote-login?token=m2a_imp_" in d.get("url", ""))
check("2.4 expires_in 900", d.get("expires_in") == 900)
check("2.5 label_hint 透传", d.get("label_hint") == "acc-02")
tok_url = d["url"]
token_in_url = tok_url.split("token=", 1)[1]
check("2.6 新建撤销旧令牌（单活跃跨用途）",
      all(e["mode"] == "url" for e in app._IMPORT_TOKENS.values()))
check("2.7 库内只存 sha256", token_in_url not in repr(app._IMPORT_TOKENS))

# ---------------- 3. GET /admin/remote-login 控制页 ----------------
print("== 3. remote-login 页面校验 ==")
r = client.get("/admin/remote-login?token=" + token_in_url)
check("3.1 url 令牌 200 + HTML", r.status_code == 200 and "text/html" in r.headers.get("content-type", ""))
check("3.2 页面含 canvas 与 WS 路径", "canvas" in r.text and "/ws/remote-login" in r.text)

# CLI 令牌开页 → 403（防串用）
app._IMPORT_TOKENS.clear()
app._IMPORT_TOKENS[h(t_cli)] = {"expires": time.monotonic() + 600, "label_hint": "", "mode": "cli"}
r = client.get("/admin/remote-login?token=" + t_cli)
check("3.3 cli 令牌开页 403", r.status_code == 403, r.status_code)
check("3.4 403 不焚毁令牌", h(t_cli) in app._IMPORT_TOKENS)

r = client.get("/admin/remote-login?token=m2a_imp_" + "f" * 32)
check("3.5 不存在令牌 401", r.status_code == 401)
r = client.get("/admin/remote-login")
check("3.6 缺 token 401", r.status_code == 401)

# ---------------- 4. URL 令牌不能给 CLI 导入用 ----------------
print("== 4. mode 隔离：/admin/accounts ==")
app._IMPORT_TOKENS.clear()
app._IMPORT_TOKENS[h(t_url)] = {"expires": time.monotonic() + 600, "label_hint": "", "mode": "url"}
cleanup_accounts()
r = client.post("/admin/accounts",
                json={"label": "x", "cookies": {"hatch_sess": "s-rl-1"}},
                headers={"Authorization": "Bearer " + t_url})
check("4.1 url 令牌 CLI 导入 403（mode 隔离防串用）", r.status_code == 403, r.status_code)
check("4.2 令牌未被焚毁", h(t_url) in app._IMPORT_TOKENS)
check("4.3 未产生账号", not app.store.accounts)

# ---------------- 5. store.add_account(aid=) 预分配 ID ----------------
print("== 5. add_account 指定 aid ==")
cleanup_accounts()
acc = store_mod.Store.add_account(app.store, {"hatch_sess": "s-aid-1"}, "rl-a", aid="abcd1234abcd")
check("5.1 指定 aid 落库", acc["id"] == "abcd1234abcd", acc["id"])
check("5.2 默认路径不变", len(acc["id"]) == 12)
check("5.3 指纹已生成", bool(acc.get("fingerprint")))
cleanup_accounts()

# ---------------- 6. cdp 泵/send 并发：响应帧回填 ----------------
print("== 6. cdp 并发 ==")
from cdp import CDP  # noqa: E402


class _FakeWS:
    """模拟 CDP ws：send 的请求由 fake 服务器异thread回一个响应帧。"""
    def __init__(self):
        self.sent = []
        import queue as _q
        self.inbox = _q.Queue()
        self.timeout = 90
        self.connected = True

    def settimeout(self, t):
        pass

    def send(self, raw):
        msg = json.loads(raw)
        # 模拟服务器：立即回响应帧（经队列，模拟 pump 回填路径）
        self.inbox.put({"id": msg["id"], "result": {}})

    def recv(self):
        try:
            return json.dumps(self.inbox.get(timeout=0.2))
        except Exception:  # noqa: BLE001
            import websocket as _w
            raise _w.WebSocketTimeoutException()

    def close(self):
        self.connected = False


fake = _FakeWS()
c = CDP.__new__(CDP)
import threading as _th  # noqa: E402
import queue as _q  # noqa: E402
c.ws = fake
c.timeout = 5
c._id = 0
c._lock = _th.Lock()
c._event_cb = None
c._pending = _q.Queue()
res = c.send("Runtime.evaluate", {"expression": "1"})
check("6.1 send 拿到响应（队列路径）", res.get("result") == {})
check("6.2 id 递增", c._id == 1)

# 错误响应 → CDPError
fake2 = _FakeWS()
c2 = CDP.__new__(CDP)
c2.ws = fake2
c2.timeout = 5
c2._id = 0
c2._lock = _th.Lock()
c2._event_cb = None
c2._pending = _q.Queue()
fake2.inbox.put({"id": 1, "error": {"message": "boom"}})
try:
    c2.send("X.y", timeout=3)
    check("6.3 错误响应抛 CDPError", False)
except Exception as exc:  # noqa: BLE001
    check("6.3 错误响应抛 CDPError", "boom" in str(exc))

# 旧 id 响应不串号
fake3 = _FakeWS()
c3 = CDP.__new__(CDP)
c3.ws = fake3
c3.timeout = 5
c3._id = 0
c3._lock = _th.Lock()
c3._event_cb = None
c3._pending = _q.Queue()
fake3.inbox.put({"id": 99, "result": {}})      # 迟到的旧响应
r1 = c3.send("A.a")
r2 = c3.send("A.b")
check("6.4 旧 id 响应不串号", r1["id"] == 1 and r2["id"] == 2, (r1.get("id"), r2.get("id")))

# ---------------- 7. RemoteLoginSession 语义 ----------------
print("== 7. RemoteLoginSession ==")
from login_session import RemoteLoginSession, new_login_aid, LOGIN_TIMEOUT  # noqa: E402

sess = RemoteLoginSession(new_login_aid(), "t")
check("7.1 aid 形态 12 hex", len(sess.aid) == 12 and all(ch in "0123456789abcdef" for ch in sess.aid))
check("7.2 deadline=TTL", abs((sess.deadline - sess.created_at) - LOGIN_TIMEOUT) < 0.01)
check("7.3 snapshot 无敏感字段",
      not any(k in sess.snapshot() for k in ("proxy", "cookies", "token", "password")))
check("7.4 snapshot 含 node", "node" in sess.snapshot() and sess.snapshot()["node"] is None)

sess.proxy = {"_binding": {"node_id": "node-9", "exit_ip": "1.2.3.4"}}
check("7.5 binding node 进 snapshot", sess.snapshot()["node"] == "node-9")
sess.proxy = None

# expired 边界
sess.deadline = time.time() - 0.01
check("7.6 expired=True", sess.expired() is True)
sess.deadline = time.time() + 60
check("7.7 未过期", sess.expired() is False)

# check_login 限频：无 page → False 且不调 probe
calls = []
sess2 = RemoteLoginSession("fedcba987654", "t")
sess2.on_login_probe = lambda eng: calls.append(1) or True
check("7.8 无 page 时 check_login=False", sess2.check_login(None) is False)
check("7.9 未调 probe", not calls)

# cookie_details：muse.ai 筛选 + expires 归整
sess3 = RemoteLoginSession("001122334455", "t")
class _FakePage:
    def send(self, method, params=None):
        assert method == "Storage.getCookies"
        return {"result": {"cookies": [
            {"name": "hatch_sess", "value": "v1", "domain": ".muse.ai", "httpOnly": True, "expires": 1e12},
            {"name": "other", "value": "x", "domain": "example.com", "expires": 1e12},
            {"name": "empty", "value": "", "domain": ".muse.ai"},
            {"name": "bad_exp", "value": "y", "domain": ".muse.ai", "expires": "NaN"},
        ]}}
sess3.page = _FakePage()
cd_ = sess3.cookie_details()
check("7.10 只含 muse.ai cookie", set(cd_) == {"hatch_sess", "bad_exp"}, set(cd_))
check("7.11 httpOnly 保留", cd_["hatch_sess"]["httpOnly"] is True)
check("7.12 expires int 归整", cd_["hatch_sess"]["expires"] == 1000000000000)
check("7.13 非法 expires→-1", cd_["bad_exp"]["expires"] == -1)
vc = sess3.get_cookies()
check("7.14 get_cookies 名值", vc == {"hatch_sess": "v1", "bad_exp": "y"})

# dispatch_input：无 page 时静默
sess3.page = None
sess3.dispatch_input({"t": "mouse", "x": 1, "y": 2})
check("7.15 无 page 输入静默丢弃", sess3.error is None)

# patch27e：goto 导航——muse.ai 域白名单
class _NavPage:
    def __init__(self):
        self.calls = []
    def send(self, method, params=None):
        self.calls.append((method, params))
        if method == "Storage.getCookies":
            return {"result": {"cookies": []}}
        return {}
navp = _NavPage()
sess4 = RemoteLoginSession("aabbccddeeff", "t")
sess4.page = navp
sess4.dispatch_input({"t": "goto", "url": "https://muse.ai/auth/magic?tok=abc"})
check("7.18 muse.ai 链接放行导航",
      ("Page.navigate", {"url": "https://muse.ai/auth/magic?tok=abc"}) in navp.calls)
sess4.dispatch_input({"t": "goto", "url": "https://evil.example.com/phish"})
check("7.19 非 muse.ai 域拒绝", sess4.error and not any(
    m == "Page.navigate" for m, _ in navp.calls[1:]))
sess4.error = None
sess4.dispatch_input({"t": "goto", "url": "javascript:alert(1)"})
check("7.20 javascript: 拒绝", sess4.error and not any(
    m == "Page.navigate" and p.get("url") == "javascript:alert(1)"
    for m, p in navp.calls))
sess4.error = None
sess4.dispatch_input({"t": "goto", "url": "https://sub.muse.ai/path"})
check("7.21 muse.ai 子域放行", any(
    m == "Page.navigate" and p.get("url") == "https://sub.muse.ai/path"
    for m, p in navp.calls))
sess4.error = None
sess4.dispatch_input({"t": "goto", "url": "https://muse.ai.evil.com/x"})
check("7.22 仿冒域(muse.ai.evil.com)拒绝", sess4.error and not any(
    m == "Page.navigate" and p.get("url") == "https://muse.ai.evil.com/x"
    for m, p in navp.calls))
sess4.error = None
sess4.dispatch_input({"t": "goto", "url": "not a url \x01"})
check("7.23 乱串不炸会话", True)

# patch27f（review P2）：域边界绕过向量——endswith 无边界会误放行
for bad in ("https://evilmuse.ai/x", "https://xmuse.ai/x", "https://MUSE.AI.EVIL.com/x"):
    sess4.error = None
    navp.calls.clear()
    sess4.dispatch_input({"t": "goto", "url": bad})
    check("7.24 域边界绕过拒绝 %s" % bad,
          sess4.error and not any(m == "Page.navigate" for m, _ in navp.calls))
# 大写正规域应放行（hostname 归一化小写）
sess4.error = None
navp.calls.clear()
sess4.dispatch_input({"t": "goto", "url": "https://MUSE.AI/x"})
check("7.25 大小写归一化放行", any(m == "Page.navigate" for m, _ in navp.calls))

# finish 幂等 + cleanup 释放出口（无 TP 启用时静默）
class _FakeEngine:
    browser = None
sess3.finished = False
sess3.finish(_FakeEngine())
check("7.16 finish 幂等", sess3.finished is True)
sess3.cleanup(_FakeEngine())
check("7.17 cleanup 幂等", sess3.finished is True)

# ---------------- 结果 ----------------
print("\n== 结果: %d PASS / %d FAIL ==" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
