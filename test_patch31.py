# -*- coding: utf-8 -*-
"""test_patch31 — 常驻登录锚点保活回归（页面级拟真保活 + cookie 收割）。

语义钉死：
  候选过滤：只保活 enabled + ok=True + 未冻结账号（401 死号不浪费上游访问）
  排序：最久未页面保活优先，并列取 last_used 最旧（与生成粘性同哲学）
  节流：interval 内不重复保活；失败也记时间戳（防 15min 重试轰炸）
  互斥：账号忙（生成/检测中）→ 本轮跳过，绝不 409 硬抢
  收割：复用 _sync_cookies 合并语义（expires 只增不覆盖）
  兜底：任何异常不抛出循环（MuseAuthError 由 loop 侧吞掉）
"""
import os
import sys
import time
import types
import threading

_here = os.path.dirname(os.path.abspath(__file__))
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
import store as store_mod  # noqa: E402
from store import Store  # noqa: E402


def _fresh_store():
    d = __import__("tempfile").mkdtemp(prefix="p31_")
    cfg = types.SimpleNamespace(
        accounts_file=os.path.join(d, "accounts.json"),
        tasks_file=os.path.join(d, "tasks.json"),
        data_dir=d, base_dir=d)
    s = Store.__new__(Store)
    s.cfg = cfg
    s.accounts = []
    s.tasks = {}
    s._tasks_lock = threading.Lock()
    s.add_account({"hatch_sess": "x"}, label="p31a")
    s.add_account({"hatch_sess": "y"}, label="p31b")
    s.add_account({"hatch_sess": "z"}, label="p31c")
    s.a, s.b, s.c = (x["id"] for x in s.accounts)
    return s


print("== 0. config 开关与默认值 ==")
import config as config_mod  # noqa: E402
cfg = config_mod.Config()
check("0.1 默认开启", cfg.anchor_keepalive_enabled is True, cfg.anchor_keepalive_enabled)
check("0.2 默认间隔 3600 且下限 300", cfg.anchor_keepalive_interval == 3600,
      cfg.anchor_keepalive_interval)
os.environ["MUSE2API_ANCHOR_INTERVAL"] = "100"
try:
    cfg_min = config_mod.Config()
    check("0.3 interval 下限 300", cfg_min.anchor_keepalive_interval == 300,
          cfg_min.anchor_keepalive_interval)
finally:
    del os.environ["MUSE2API_ANCHOR_INTERVAL"]

print("== 1. app 层：候选过滤 / 排序 / 节流 / 互斥 ==")
import app  # noqa: E402

saved = {}
for name, val in (("store", app.store), ("ACCOUNT_POOL", app.ACCOUNT_POOL),
                  ("engine", app.engine), ("_renew_and_persist", app._renew_and_persist),
                  ("_bind_account_proxy", app._bind_account_proxy),
                  ("_sync_cookies", app._sync_cookies)):
    saved[name] = val

s31 = _fresh_store()
app.store = s31
app.ACCOUNT_POOL = app.AccountPoolManager(s31)


class _FakeEngine:
    def __init__(self):
        self.ensure_calls = []
    def ensure_page(self, cookies, expires=None, account_id=None):
        self.ensure_calls.append(account_id)
        return object()
    def start(self): pass
    def close_current_page(self): pass


class _FakePool:
    def __init__(self, acc):
        self.acc = acc
        self.busy_accounts = set()
        self.cond = threading.Condition()
        self.released = []
    def acquire(self, *a, **kw):
        return dict(self.acc)
    def release(self, aid):
        self.released.append(aid)
        self.busy_accounts.discard(aid)


eng = _FakeEngine()
pool = _FakePool(s31.get_account(s31.a))
app.engine = eng
app.ACCOUNT_POOL = pool
app._renew_and_persist = lambda aid, wake_vm=True, force=False: s31.get_account(aid)
app._bind_account_proxy = lambda acc: None
sync_calls = []
app._sync_cookies = lambda aid: (sync_calls.append(aid),
                                 {"synced": 5, "changed": 2})[1]

# 全部账号标健康
for aid in (s31.a, s31.b, s31.c):
    s31.update_account(aid, ok=True)

try:
    # 1a 首轮：选最旧候选（并列 last_used 全 0 → 排序稳定取第一个，a 或按排序）
    app.ANCHOR_LAST.clear()
    r = app._anchor_keepalive_sync()
    check("1.1 首轮有动作", r is not None, repr(r))
    check("1.2 ensure_page 被调（页面级保活发生）", len(eng.ensure_calls) == 1, eng.ensure_calls)
    check("1.3 cookie 收割发生", sync_calls == [r["id"]], sync_calls)
    check("1.4 时间戳已记", app.ANCHOR_LAST.get(r["id"], 0) > 0)
    check("1.5 租约已释放", r["id"] in pool.released and r["id"] not in pool.busy_accounts)

    # 1b 节流：interval 内（3600s）再调 → None
    r2 = app._anchor_keepalive_sync()
    check("1.6 interval 内轮转到下一个账号（a 被节流）",
          r2 is not None and r2["id"] != r["id"], (r2, r["id"]))

    # 1c 全部在节流窗口内 → None（c 也要激活过才算"全节流"）
    app.ANCHOR_LAST[s31.c] = time.time()
    r3 = app._anchor_keepalive_sync()
    check("1.7 全节流无动作", r3 is None, repr(r3))

    # 1d 间隔外：时间戳推老 → 重新可保活
    app.ANCHOR_LAST[r["id"]] = time.time() - 4000
    r4 = app._anchor_keepalive_sync()
    check("1.8 间隔外恢复动作", r4 is not None, repr(r4))

    # 1e ok=False 死号被过滤：把 a 标死，b/c 节流中 → None（不会选 a）
    s31.update_account(s31.a, ok=False)
    app.ANCHOR_LAST[s31.b] = time.time() - 4000
    app.ANCHOR_LAST[s31.c] = time.time() - 4000
    app.ANCHOR_LAST[s31.a] = 0.0  # 死号最旧
    eng.ensure_calls.clear()
    r5 = app._anchor_keepalive_sync()
    check("1.9 ok=False 死号不参与保活",
          r5 is not None and r5["id"] != s31.a, (r5, s31.a[:8]))

    # 1f 禁用账号被过滤
    s31.update_account(s31.b, enabled=False)
    app.ANCHOR_LAST.clear()
    app.ANCHOR_LAST[s31.a] = 0.0  # 死号最旧；c 健康
    r6 = app._anchor_keepalive_sync()
    check("1.10 disabled 账号不参与", r6 is not None and r6["id"] == s31.c, (r6, s31.c[:8]))

    # 1g 冻结账号被过滤
    for _ in range(3):
        s31.record_video_refusal(s31.c)
    check("1.11 前置：c 已冻结", s31.video_frozen(s31.get_account(s31.c)))
    s31.update_account(s31.a, ok=True)  # 复活 a
    app.ANCHOR_LAST.clear()
    app.ANCHOR_LAST[s31.a] = 0.0
    r7 = app._anchor_keepalive_sync()
    check("1.12 冻结账号不参与", r7 is not None and r7["id"] == s31.a, (r7, s31.a[:8]))

    # 1h 账号忙 → 跳过（不硬抢）
    app.ANCHOR_LAST.clear()
    pool.busy_accounts.add(s31.a)
    r8 = app._anchor_keepalive_sync()
    check("1.13 账号忙本轮跳过", r8 is None, repr(r8))
    check("1.14 未硬抢（busy 集未被清）", s31.a in pool.busy_accounts)

    # 1i 无健康账号 → None
    for aid in (s31.a, s31.b, s31.c):
        s31.update_account(aid, ok=False)
    app.ANCHOR_LAST.clear()
    r9 = app._anchor_keepalive_sync()
    check("1.15 无健康候选无动作", r9 is None, repr(r9))

    # 1j 开关关闭 → None
    saved_cfg = app.CFG
    app.CFG = types.SimpleNamespace(**{**vars(saved_cfg),
                                       "anchor_keepalive_enabled": False,
                                       "anchor_keepalive_interval": 3600})
    try:
        s31.update_account(s31.a, ok=True)
        r10 = app._anchor_keepalive_sync()
        check("1.16 开关关闭无动作", r10 is None, repr(r10))
    finally:
        app.CFG = saved_cfg

    # 1k ensure_page 抛 MuseAuthError → 上抛（由 loop 吞），时间戳已记
    from engine import MuseAuthError
    s31.update_account(s31.a, ok=True)
    pool.busy_accounts.discard(s31.a)  # 清 1h 遗留的 busy 标记
    app.ANCHOR_LAST.clear()

    def _boom(aid, wake_vm=True, force=False):
        raise MuseAuthError("401 dead")

    app._renew_and_persist = _boom
    raised = None
    try:
        app._anchor_keepalive_sync()
    except MuseAuthError as e:  # noqa: BLE001
        raised = e
    check("1.17 MuseAuthError 上抛（loop 侧吞）", raised is not None, repr(raised))
    check("1.18 失败也记节流戳（防重试轰炸）", app.ANCHOR_LAST.get(s31.a, 0) > 0)
finally:
    for name, val in saved.items():
        setattr(app, name, val)

print("\n== 结果: %d PASS / %d FAIL ==" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
