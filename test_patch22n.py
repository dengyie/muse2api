# -*- coding: utf-8 -*-
"""test_patch22n — mediabak mesh 冷却（patch22n）行为级回归。

场景：tencent-lh 容器内 127.0.0.1:5244 无监听，mesh login 每次都连接失败。
修复前：每轮 _get_token 都先撞 mesh 15s 超时 + 告警噪音。
修复后：mesh 登录异常 → 30 分钟冷却，遍历顺序排到 public 之后；
      冷却到期自动复探；mesh 登录成功即解除冷却。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print("FAIL: %s %s" % (name, detail))


def _import_real(modname):
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(modname, os.path.join(here, modname + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mediabak = _import_real("mediabak")
from config import CFG as _REAL_CFG
_REAL_CFG.backup_enabled = True
_REAL_CFG.backup_app_pass = "p"

BK = mediabak.MediaBackuper()

calls = []


class _Resp:
    def __init__(self, payload):
        self._p = payload
        self.status_code = 200

    def json(self):
        return self._p


def fake_post(url, json=None, headers=None, timeout=None, **kw):
    calls.append(url)
    if "127.0.0.1:1" in url:  # mesh base（fake 成不可达地址语义，由 fake 抛异常模拟更真实）
        raise ConnectionError("refused")
    if "127.0.0.1:2" in url:  # public base
        if url.endswith("/api/auth/login"):
            return _Resp({"code": 200, "data": {"token": "tok-pub"}})
    return _Resp({"code": 404})


orig_login_timeout = mediabak.requests.post
mediabak.requests.post = fake_post

# 让 mesh 通道指向一个 fake 会抛异常的 base
_REAL_CFG.backup_mesh_url = "http://127.0.0.1:1"
_REAL_CFG.backup_public_url = "http://127.0.0.1:2"

# 1. 首轮：mesh 异常 → 进入冷却；public 成功
calls.clear()
tok, chan = BK._get_token()
check("first round gets token", tok == "tok-pub", tok)
check("first round uses public", chan == "public", chan)
check("mesh tried before cooldown", any("127.0.0.1:1" in u for u in calls))
check("mesh cooldown armed", BK._mesh_dead_until > time.monotonic(),
      BK._mesh_dead_until - time.monotonic())

# 2. 模拟 token 过期后再取：mesh 冷却中应被排到队尾（public 先试且成功 → mesh 不再被撞）
BK._token = None
BK._token_at = 0.0
calls.clear()
tok, chan = BK._get_token()
check("second round still public", chan == "public")
check("mesh skipped during cooldown", not any("127.0.0.1:1" in u for u in calls),
      calls)

# 3. 冷却到期 → mesh 恢复被尝试（仍失败，重新冷却）
BK._token = None
BK._token_at = 0.0
BK._mesh_dead_until = time.monotonic() - 1  # 过期
calls.clear()
tok, chan = BK._get_token()
check("expired cooldown retries mesh", any("127.0.0.1:1" in u for u in calls))
check("mesh re-armed after retry fail", BK._mesh_dead_until > time.monotonic())

# 4. mesh 恢复：登录成功 → 冷却解除
def fake_post_recovered(url, json=None, headers=None, timeout=None, **kw):
    if "127.0.0.1:1" in url and url.endswith("/api/auth/login"):
        return _Resp({"code": 200, "data": {"token": "tok-mesh"}})
    return fake_post(url, json=json, headers=headers, timeout=timeout, **kw)


mediabak.requests.post = fake_post_recovered
BK._token = None
BK._token_at = 0.0
BK._mesh_dead_until = time.monotonic() - 1
tok, chan = BK._get_token()
check("mesh recovery login ok", tok == "tok-mesh", tok)
check("cooldown cleared on success", BK._mesh_dead_until == 0.0)
# 恢复后 _chan=mesh，后续优先走 mesh
BK._token = None
BK._token_at = 0.0
calls.clear()
tok, chan = BK._get_token()
check("mesh preferred after recovery", chan == "mesh" and tok == "tok-mesh")

mediabak.requests.post = orig_login_timeout

print("\nPASSED %d  FAILED %d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
