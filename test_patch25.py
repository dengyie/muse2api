# -*- coding: utf-8 -*-
"""test_patch25 — 直调路径代理异常自愈回归（真实 TestClient 模式）。

覆盖：
  1. _rebind_on_proxy_error 帮助函数语义：
     - 非托管代理 / TP 未启用 → 返回 None 不动作
     - 托管代理 + TP 启用 → acquire(force)+report+store 落库+proxy_snapshot 更新
  2. attach-probe 出口故障自愈：ensure_page 抛 MuseProxyError → 重绑 → 重试成功
  3. quota 端点出口故障自愈：engine.quota 首抛 MuseProxyError → 重绑 → 成功读数
  4. test 端点出口故障自愈：refresh 抛 MuseProxyError → 重绑 → 会话 ok
  5. 重绑失败（acquire 返回 None）→ 原异常上抛，行为不劣化
"""
import json
import os
import sys
import tempfile
import types

_here = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="m2a-p25-test-")
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
from engine import MuseProxyError  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app.app)
ADMIN = {"Authorization": "Bearer testkey123"}
app.CFG.api_key = "testkey123"


def cleanup_accounts():
    with store_mod._LOCK:
        app.store.accounts = []


def add_acc(aid="f519883a14b2", label="p25-a"):
    cleanup_accounts()
    with store_mod._LOCK:
        app.store.accounts.append({
            "id": aid, "label": label, "enabled": True, "ok": True,
            "cookies": {"hatch_sess": "s25-" + aid},
            "cookies_exp": {"hatch_sess": 9999999999},
            "proxy": {"server": "http://10.144.144.9:9101/",
                      "username": aid + "__type_warp_ipv6__service_muse2api",
                      "password": "tpw"},
            "binding": {"node_id": "huawei-sg-warp-25",
                        "exit_ip": "2a09:bac5::x"},
        })
    return app.store.get_account(aid)


# ---------------- 0. TP stub 环境 ----------------
# stub tetherpool：enabled()=True、is_managed()=True、acquire 返回新出口、
# report_proxy_error 记录调用。stub 挂在 app 模块命名空间（app 以 `import
# tetherpool` 方式引用其属性）。
CALLS = {"acquire": [], "report": []}
NEW_PROXY = {"server": "http://10.144.144.9:9101/",
             "username": "x__type_warp_ipv6__service_muse2api",
             "password": "tpw", "_binding": {"node_id": "azure-india-warp-42"}}


class _TPStub:
    def acquire(self, aid, force=False, constraints=None):
        CALLS["acquire"].append((aid, force, constraints))
        return dict(NEW_PROXY)

    def report_proxy_error(self, aid, node_name=None, event_type="proxy_error",
                           rebind=False):
        CALLS["report"].append((aid, event_type, rebind))


app.TP = _TPStub()
app.tetherpool = types.SimpleNamespace(
    enabled=lambda: True,
    is_managed=lambda p: bool(p and "tetherpool" in (p.get("username") or "")
                              or (p and "9101" in (p.get("server") or ""))),
)

# ---------------- 1. _rebind_on_proxy_error 语义 ----------------
print("== 1. _rebind_on_proxy_error 帮助函数 ==")


def reset_calls():
    CALLS["acquire"] = []
    CALLS["report"] = []


# patch25c（F1）：记录 close_current_page 调用
_CLOSED = []
app.engine.close_current_page = lambda: _CLOSED.append(1)


acc = add_acc()
reset_calls()
_CLOSED.clear()
out = app._rebind_on_proxy_error(acc["id"], acc)
check("1.1 托管+启用→重绑返回账号", out is not None, repr(out)[:80])
check("1.2 acquire(force=True) 带约束", CALLS["acquire"] and CALLS["acquire"][0][1] is True)
check("1.3 上报 proxy_error 事件", CALLS["report"] and CALLS["report"][0][1] == "proxy_error")
check("1.3b 重绑成功关旧页（review F1）", len(_CLOSED) == 1, str(_CLOSED))
stored = app.store.get_account(acc["id"])
check("1.4 store proxy 已更新", stored["proxy"]["username"] == NEW_PROXY["username"])
check("1.5 binding 档案同步", (stored.get("binding") or {}).get("node_id") == "azure-india-warp-42")
local = app.engine._ensure_local()
check("1.6 proxy_snapshot 已刷新", getattr(local, "proxy_snapshot", None) is not None
      and local.proxy_snapshot["username"] == NEW_PROXY["username"])

# 非托管代理 → None 不动作
reset_calls()
app.tetherpool.is_managed = lambda p: False
out2 = app._rebind_on_proxy_error(acc["id"], acc)
check("1.7 非托管→None", out2 is None)
check("1.8 非托管不调 acquire", not CALLS["acquire"])
app.tetherpool.is_managed = lambda p: bool(p and "9101" in (p.get("server") or ""))

# TP 未启用 → None
reset_calls()
app.tetherpool.enabled = lambda: False
out3 = app._rebind_on_proxy_error(acc["id"], acc)
check("1.9 TP 禁用→None", out3 is None)
app.tetherpool.enabled = lambda: True

# acquire 失败 → None 且异常被吞（不劣化）
reset_calls()
_CLOSED.clear()
class _TPFail(_TPStub):
    def acquire(self, aid, force=False, constraints=None):
        return None
app.TP = _TPFail()
out4 = app._rebind_on_proxy_error(acc["id"], acc)
check("1.10 acquire 无果→None 不抛", out4 is None)
check("1.11 acquire 无果不关页（页属调用方现场）", not _CLOSED, str(_CLOSED))
app.TP = _TPStub()

# ---------------- 2. attach-probe 自愈 ----------------
print("== 2. attach-probe 出口故障自愈 ==")
acc = add_acc("a25attach01", "p25-attach")
reset_calls()
_orig_ensure = app.engine.ensure_page
_state = {"n": 0}


class _FakePage:
    def js(self, expr, *a, **k):
        return json.dumps({"remove_btns": ["Remove attachment"]})


def _flaky_ensure(cookies, expires, account_id=None, **kw):
    _state["n"] += 1
    if _state["n"] == 1:
        raise MuseProxyError("账号代理请求失败 (ProxyError)")
    return _FakePage()


app.engine.ensure_page = _flaky_ensure
app.engine.start = lambda: None
app.engine._attach_images = lambda imgs: {"ok": True}
app.engine.refresh = lambda cookies, expires=None, account_id=None, **kw: _FakePage()
app.engine.close_current_page = lambda: None
# composer probe 依赖真实 page：stub 掉
app._composer_probe_js = lambda page: "{}"
try:
    r = client.post("/admin/accounts/a25attach01/attach-probe", headers=ADMIN)
    d = r.json()
finally:
    app.engine.ensure_page = _orig_ensure
check("2.1 attach-probe 最终 ok", d.get("ok") is True and d.get("attach") is True, json.dumps(d)[:160])
check("2.2 触发了一次重绑", len(CALLS["acquire"]) == 1)
check("2.3 ensure_page 调了两次", _state["n"] == 2)

# 重绑失败 → 异常上抛（502 形态）
reset_calls()
app.TP = _TPFail()
_state["n"] = 0
try:
    r = client.post("/admin/accounts/a25attach01/attach-probe", headers=ADMIN)
    d2 = r.json()
finally:
    app.TP = _TPStub()
check("2.4 重绑失败→ok:false", d2.get("ok") is False, json.dumps(d2)[:160])
check("2.5 ProxyError 语义保留", "ProxyError" in json.dumps(d2), json.dumps(d2)[:160])

# ---------------- 3. quota 端点自愈 ----------------
print("== 3. quota 出口故障自愈 ==")
acc = add_acc("a25quota01", "p25-quota")
reset_calls()
_orig_quota = app.engine.quota
_qstate = {"n": 0}


def _flaky_quota(cookies, expires=None, account_id=None, **kw):
    _qstate["n"] += 1
    if _qstate["n"] == 1:
        raise MuseProxyError("账号代理请求失败 (ProxyError)")
    return {"plan": "Free plan", "weekly_used_pct": 1, "extra_left": "2B tokens left",
            "extra_used_pct": 0, "extra_expires": "never", "found": True}


app.engine.quota = _flaky_quota
try:
    r = client.post("/admin/accounts/a25quota01/quota", headers=ADMIN)
    d = r.json()
finally:
    app.engine.quota = _orig_quota
check("3.1 quota 最终 found", d.get("found") is True, json.dumps(d)[:160])
check("3.2 触发了一次重绑", len(CALLS["acquire"]) == 1)
check("3.3 quota 调了两次", _qstate["n"] == 2)
stored = app.store.get_account("a25quota01")
check("3.4 读数已缓存", (stored.get("quota") or {}).get("weekly_used_pct") == 1)

# ---------------- 4. test 端点自愈 ----------------
print("== 4. test 端点出口故障自愈 ==")
acc = add_acc("a25test001", "p25-test")
reset_calls()
_orig_refresh = app.engine.refresh
_state = {"n": 0}


def _flaky_refresh(cookies, expires=None, account_id=None, **kw):
    _state["n"] += 1
    if _state["n"] == 1:
        raise MuseProxyError("账号代理请求失败 (ProxyError)")
    return "PAGE-OK"


app.engine.refresh = _flaky_refresh
app.engine.start = lambda: None
app.engine.close_current_page = lambda: None
app._sync_cookies = lambda aid: 0
app.engine.quota = lambda cookies, expires=None, account_id=None, **kw: {"found": False}
try:
    r = client.post("/admin/accounts/a25test001/test", headers=ADMIN)
    d = r.json()
finally:
    app.engine.refresh = _orig_refresh
check("4.1 test 最终 ok:true", d.get("ok") is True, json.dumps(d)[:200])
check("4.2 触发了一次重绑", len(CALLS["acquire"]) == 1)
check("4.3 refresh 调了两次", _state["n"] == 2)

# ---------------- 5. 结构断言 ----------------
print("== 5. 结构断言 ==")
src = open(os.path.join(_here, "app.py"), encoding="utf-8").read()
check("5.1 _rebind_on_proxy_error 定义存在", "def _rebind_on_proxy_error" in src)
check("5.2 三端点接线", src.count("_rebind_on_proxy_error(") >= 4)
check("5.3 帮助函数走 TP.acquire 带约束", "TP.acquire(acc_id, force=True" in src)
src_patrol = open(os.path.join(_here, "_scripts", "patrol.sh"), encoding="utf-8").read()
check("5.4 patrol 读 MUSE2API_BUILD_SHA", "MUSE2API_BUILD_SHA" in src_patrol
      and "echo $BUILD_SHA" not in src_patrol)
src_compose = open(os.path.join(_here, "docker-compose.yml"), encoding="utf-8").read()
check("5.5 compose 引用式 MESH_URL",
      "MUSE2API_BACKUP_MESH_URL=${MUSE2API_BACKUP_MESH_URL:-" in src_compose)

print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)
