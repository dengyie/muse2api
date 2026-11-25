# -*- coding: utf-8 -*-
"""test_patch32 — 生成路径重绑失败自愈回归（FakeEngine 驱动 _run_generation_locked 状态机）。

2026-11-24 实证 task_89b34124718047cfb037：锚点保活撞坏出口同秒生成任务
拿同一出口 → MuseProxyError → 重绑 acquire 撞控制面瞬时不可达（Max
retries）→ acquire 返回 None 被吞 → rebound=False → attempt>0 单号池无
其他账号 → break 秒死。battery 事件同样上报失败（同一次控制面故障），
ip-pool 完全不知情。

patch32 修复点（app._run_generation_locked）：
  1. MuseProxyError 分支 acquire 失败 → 置 rebound_failed + deadline 有余量
     时退避 15s 重试 acquire 一次（成功则 rebound=True/rebound_failed=False）
  2. 切号分支单号池原地重试条件放宽：rebound or rebound_failed
  3. 原语义不变：重绑成功（rebound=True）照旧原地重试
  4. review P2-2：退避期 on_stage("rebind_backoff") 透传（轮询端可见）

行为级测试（review P2-1 根修）：FakeEngine/FakeAccountPool 真实驱动
_run_generation_locked（test_patch28 模式移植），不再只靠结构断言。
结构断言保留作防回退护栏。
"""
import os
import re
import sys
import time
import types

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
import app  # noqa: E402
from engine import MuseProxyError  # noqa: E402


class _FakeAccountPool:
    """单号池语义：acquire 永远返回同一账号（rotate 时也拿不到备用号）。"""

    def __init__(self, acc):
        self.acc = acc
        self.rotate_calls = 0
        self.released = []

    def acquire(self, account_id=None, preferred_id=None, rotate=False,
                force_rotate=False, timeout=None, **kw):
        if force_rotate:
            self.rotate_calls += 1
            return dict(self.acc)  # 单号池：切号仍返回同号 → alt["id"]==old_id
        return dict(self.acc)

    def release(self, aid):
        self.released.append(aid)

    @property
    def cond(self):
        import threading
        return threading.Lock()

    busy_accounts = set()


class _FakeEngine:
    """generate 行为由外部脚本控制；页面管理方法全部空转。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.started = 0
        self._last_http_renew = {}  # app 侧生成循环 finally 清理该缓存

    def generate(self, cookies, prompt, expect=None, timeout=None, **kw):
        self.calls.append(prompt)
        action = self.script.pop(0)
        if isinstance(action, Exception):
            raise action
        return action

    def start(self):
        self.started += 1

    def close_current_page(self):
        pass

    def reset_thread(self):
        pass


class _TPStub:
    """控制面客户端 stub：acquire 行为按脚本序列（None=控制面不可达）。"""

    def __init__(self, acquire_script):
        self.script = list(acquire_script)
        self.acquire_calls = 0
        self.reported = []

    def acquire(self, aid, force=False, constraints=None):
        self.acquire_calls += 1
        action = self.script.pop(0) if self.script else None
        if isinstance(action, Exception):
            raise action
        if action is None:
            return None  # 控制面瞬时不可达 → acquire 静默兜底返回 None
        proxy = dict(action)
        proxy["_binding"] = {"node_id": "azure-india-warp-42", "region": "india"}
        return proxy

    def report_proxy_error(self, aid, node_name=None, event_type="proxy_error",
                           rebind=False):
        self.reported.append((aid, event_type))


GOOD_PROXY = {"server": "http://10.144.144.9:9101/",
              "username": "x__type_warp_ipv6__service_muse2api",
              "password": "tpw"}


def _install(stub, acc_id="f519883a14b2"):
    saved = {}
    for name in ("ACCOUNT_POOL", "engine", "TP", "tetherpool",
                 "_renew_and_persist", "_sync_cookies", "_bind_account_proxy",
                 "_clear_account_proxy"):
        saved[name] = getattr(app, name)
        setattr(app, name, stub.get(name, saved[name]))
    return saved


def _restore(saved):
    for name, val in saved.items():
        setattr(app, name, val)


def _setup_case(gen_script, tp_script, acc=None):
    acc = acc or {"id": "f519883a14b2", "label": "p32", "enabled": True,
                  "cookies": {"hatch_sess": "s32"}, "cookies_exp": {},
                  "proxy": dict(GOOD_PROXY),
                  "binding": {"node_id": "huawei-sg-warp-25", "region": "singapore"}}
    pool = _FakeAccountPool(acc)
    eng = _FakeEngine(gen_script)
    tp = _TPStub(tp_script)
    saved = _install({"ACCOUNT_POOL": pool, "engine": eng, "TP": tp,
                      "tetherpool": types.SimpleNamespace(
                          enabled=lambda: True,
                          is_managed=lambda p: bool(p and p.get("server")),
                          is_cn_region=lambda r: bool(r) and str(r).lower().startswith("cn")),
                      "_renew_and_persist": lambda *a, **k: None,
                      "_sync_cookies": lambda *a, **k: None,
                      "_bind_account_proxy": lambda *a, **k: None,
                      "_clear_account_proxy": lambda *a, **k: None})
    real_mark = app.store.mark
    app.store.mark = lambda *a, **k: None
    real_touch = app.store.touch_keepalive
    app.store.touch_keepalive = lambda *a, **k: None
    # 行为级 stub：patch32 重绑成功后 store.update_account 会写回账号档案；
    # 假账号不在真实 store 里（data/accounts.json 缺省不存在），真身返回 None
    # 会让 cur_acc["id"] TypeError。stub 回传合并后的账号 dict 即可。
    real_update = app.store.update_account
    app.store.update_account = lambda aid, **kw: {**acc, **kw}
    return pool, eng, tp, saved, real_mark, real_touch, real_update


RESULT_OK = {"url": "http://x/video.mp4"}

# ---------------- 1. 行为级：acquire 失败→退避重试成功→原地重试→成功 ----------------
print("== 1. 行为级：退避重试成功后原地重试产出 ==")
pool, eng, tp, saved, real_mark, real_touch, real_update = _setup_case(
    [MuseProxyError("proxy dead"), RESULT_OK],
    [None, GOOD_PROXY])  # 首次 acquire 控制面不可达(None)，重试成功
stages = []
try:
    t0 = time.monotonic()
    res, aid = app._run_generation_locked("一只猫", "video", 600,
                                          on_stage=lambda s: stages.append(s))
    elapsed = time.monotonic() - t0
    check("1.1 退避重试后第二轮成功", res == RESULT_OK and aid == "f519883a14b2", (res, aid))
    check("1.2 generate 恰 2 次（原地重试非秒死）", len(eng.calls) == 2, len(eng.calls))
    check("1.3 acquire 恰 2 次（退避一次）", tp.acquire_calls == 2, tp.acquire_calls)
    check("1.4 退避真实发生（elapsed>=15s）", elapsed >= 14.5, round(elapsed, 1))
    check("1.5 battery 事件已上报", tp.reported == [("f519883a14b2", "proxy_error")], tp.reported)
    check("1.6 退避期 stage 透传（review P2-2）", "rebind_backoff" in stages, stages)
finally:
    app.store.mark = real_mark
    app.store.touch_keepalive = real_touch
    app.store.update_account = real_update
    _restore(saved)

# ---------------- 2. 行为级：退避后仍失败 → rebound_failed 原地重试 → 成功 ----------------
print("== 2. 行为级：rebound_failed 放宽原地重试 ==")
pool, eng, tp, saved, real_mark, real_touch, real_update = _setup_case(
    [MuseProxyError("proxy dead"), RESULT_OK],
    [None, None])  # 两次 acquire 都不可达（控制面恢复中）
try:
    raised = None
    res = None
    try:
        res, aid = app._run_generation_locked("一只猫", "video", 600)
    except Exception as e:  # noqa: BLE001
        raised = e
    # 场景推演：轮1 ProxyError → acquire None → 退避重试仍 None → rebound_failed
    #   → 轮2 原地重试（rebound_failed 放宽）→ generate 成功
    check("2.1 退避彻底失败仍原地重试并成功（patch32 核心）",
          res == RESULT_OK and raised is None, (res, raised))
    check("2.2 generate 恰 2 次", len(eng.calls) == 2, len(eng.calls))
    check("2.3 acquire 恰 2 次", tp.acquire_calls == 2, tp.acquire_calls)
finally:
    app.store.mark = real_mark
    app.store.touch_keepalive = real_touch
    app.store.update_account = real_update
    _restore(saved)

# ---------------- 3. 行为级：deadline 临近不退避直接原地重试 ----------------
print("== 3. 行为级：deadline-30 闸门跳过退避 ==")
pool, eng, tp, saved, real_mark, real_touch, real_update = _setup_case(
    [MuseProxyError("proxy dead"), RESULT_OK],
    [None])
try:
    t0 = time.monotonic()
    res, aid = app._run_generation_locked(
        "一只猫", "video", 600, deadline=time.monotonic() + 20)  # 剩余<30s
    elapsed = time.monotonic() - t0
    check("3.1 deadline 不足跳过退避（无 15s sleep）", elapsed < 10, round(elapsed, 1))
    check("3.2 仍原地重试并成功（rebound_failed 放宽）",
          res == RESULT_OK and len(eng.calls) == 2, (res, len(eng.calls)))
finally:
    app.store.mark = real_mark
    app.store.touch_keepalive = real_touch
    app.store.update_account = real_update
    _restore(saved)

# ---------------- 4. 行为级：重绑成功（rebound=True）原语义不变 ----------------
print("== 4. 行为级：重绑成功路径回归 ==")
pool, eng, tp, saved, real_mark, real_touch, real_update = _setup_case(
    [MuseProxyError("proxy dead"), RESULT_OK],
    [GOOD_PROXY])  # 首次 acquire 即成功（patch17e 原语义）
try:
    res, aid = app._run_generation_locked("一只猫", "video", 600)
    check("4.1 重绑成功原地重试产出（原语义）",
          res == RESULT_OK and len(eng.calls) == 2, (res, len(eng.calls)))
    check("4.2 acquire 恰 1 次（成功路径不退避）", tp.acquire_calls == 1, tp.acquire_calls)
finally:
    app.store.mark = real_mark
    app.store.touch_keepalive = real_touch
    app.store.update_account = real_update
    _restore(saved)

# ---------------- 5. 结构断言（防回退护栏） ----------------
print("== 5. 结构断言 ==")
src = open(os.path.join(_here, "app.py"), encoding="utf-8").read()
check("5.1 rebound_failed 初始化", "rebound_failed = False" in src)
check("5.2 退避有 deadline 闸门", "deadline - 30" in src)
check("5.3 单号池原地重试放宽 (rebound or rebound_failed)",
      bool(re.search(r"and \(rebound or rebound_failed\)", src)))
check("5.4 旧单条件已移除",
      re.search(r"isinstance\(last_exc, MuseProxyError\) and rebound\b", src) is None)
check("5.5 rebind_backoff stage 透传", 'on_stage("rebind_backoff")' in src)

print("\n== 结果: %d PASS / %d FAIL ==" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
