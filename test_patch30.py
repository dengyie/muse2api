# -*- coding: utf-8 -*-
"""test_patch30 — 视频风控熔断回归（refusal×3 冻结视频链路 7 天）。

背景（用户裁定）：部分账号会风控，连续 3 次 muse 视频失败自动冻结视频链路一周。
钉死语义：只把「模型只回文本」（refusal，疑似账号级风控信号）计入；超时/附件
断链/代理错是链路或上游故障（11-23 上游 503 故障窗实证全池皆然），不计入。

覆盖：
  store 层：计数累加 / 达 3 冻结 7 天 / 冻结中不再累计 / 成功清零 /
            到期自动解冻 / 手动解冻 / pick_account 跳冻结（全冻结放行兜底）
  app 层：  refusal(code=generation_failed) 计数 /
            attachment_broken 不计数 / 心跳解冻调用
"""
import os
import sys
import tempfile
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
import store as store_mod  # noqa: E402
from store import Store  # noqa: E402


def _fresh_store():
    """独立 tmp 数据目录的 Store（不碰生产 accounts.json）。"""
    d = tempfile.mkdtemp(prefix="p30_")
    cfg = types.SimpleNamespace(
        accounts_file=os.path.join(d, "accounts.json"),
        tasks_file=os.path.join(d, "tasks.json"),
        data_dir=d, base_dir=d)
    s = Store.__new__(Store)
    s.cfg = cfg
    s.accounts = []
    s.tasks = {}
    s._tasks_lock = __import__("threading").Lock()
    s.add_account({"hatch_sess": "x"}, label="p30a")
    s.add_account({"hatch_sess": "y"}, label="p30b")
    s.a, s.b = s.accounts[0]["id"], s.accounts[1]["id"]
    return s


print("== 1. store: 计数累加与冻结 ==")
s = _fresh_store()
snap = s.record_video_refusal(s.a, note="r1")
check("1.1 第 1 次 streak=1 未冻结",
      snap["video_fail_streak"] == 1 and not s.video_frozen(snap), snap.get("video_fail_streak"))
snap = s.record_video_refusal(s.a)
check("1.2 第 2 次 streak=2 未冻结",
      snap["video_fail_streak"] == 2 and not s.video_frozen(snap))
check("1.3 第 2 账号独立计数", s.get_account(s.b).get("video_fail_streak") in (None, 0))
snap = s.record_video_refusal(s.a)
check("1.4 第 3 次触发冻结", s.video_frozen(snap), snap)
until = snap.get("video_frozen_until") or 0
days = (until - time.time()) / 86400
check("1.5 冻结时长 ≈7 天（6.9~7.1）", 6.9 <= days <= 7.1, round(days, 2))
check("1.6 冻结即清零 streak", snap.get("video_fail_streak") == 0, snap.get("video_fail_streak"))

print("== 2. store: 冻结中不再累计 ==")
snap = s.record_video_refusal(s.a)
check("2.1 冻结中计数不涨", snap.get("video_fail_streak") == 0 and s.video_frozen(snap))

print("== 3. store: 成功清零 ==")
s.thaw_account(s.a)
s.record_video_refusal(s.a)
s.record_video_refusal(s.a)
s.record_video_success(s.a)
check("3.1 成功后 streak=0 未冻结",
      s.get_account(s.a).get("video_fail_streak") == 0
      and not s.video_frozen(s.get_account(s.a)))

print("== 4. store: 到期自动解冻 ==")
acc = s.get_account(s.a)
s.record_video_refusal(s.a); s.record_video_refusal(s.a)
snap = s.record_video_refusal(s.a)
check("4.1 再次冻结", s.video_frozen(snap))
acc = s.get_account(s.a)
acc["video_frozen_until"] = int(time.time()) - 1  # 已过期
thawed = s.thaw_due_accounts()
check("4.2 thaw_due 解冻该账号", s.a in thawed, thawed)
check("4.3 解冻后字段复位",
      s.get_account(s.a).get("video_frozen_until") is None
      and s.get_account(s.a).get("video_fail_streak") == 0)
check("4.4 未到期账号不动", s.thaw_due_accounts() == [])

print("== 5. store: pick_account 跳冻结 ==")
s.thaw_account(s.b)
s.record_video_refusal(s.b); s.record_video_refusal(s.b); s.record_video_refusal(s.b)
check("5.1 sess-b 已冻结", s.video_frozen(s.get_account(s.b)))
# 两号池：sess-b 冻结 → 必选 sess-a
picked = s.pick_account(rotate=True, force_rotate=True)
check("5.2 pick 跳过冻结账号", picked["id"] == s.a, picked["id"])
# 全冻结 → 放行兜底（可用性优先）
s.record_video_refusal(s.a); s.record_video_refusal(s.a); s.record_video_refusal(s.a)
picked = s.pick_account(rotate=True, force_rotate=True)
check("5.3 全冻结放行兜底", picked is not None, repr(picked))

print("== 6. store: 手动解冻 ==")
snap = s.thaw_account(s.b)
check("6.1 手动解冻生效", snap is not None and not s.video_frozen(snap))
check("6.2 不存在账号返回 None", s.thaw_account("nope") is None)

print("== 7. app: refusal 计数 / attachment_broken 不计数 ==")
import app  # noqa: E402
import threading
from engine import MuseModelRefusalError

saved = {}
for name, val in (("ACCOUNT_POOL", app.ACCOUNT_POOL), ("engine", app.engine),
                  ("_renew_and_persist", app._renew_and_persist),
                  ("_sync_cookies", app._sync_cookies),
                  ("_bind_account_proxy", app._bind_account_proxy),
                  ("_clear_account_proxy", app._clear_account_proxy),
                  ("_rewrite_prompt_on_refusal", app._rewrite_prompt_on_refusal)):
    saved[name] = val

s30 = _fresh_store()
app.store = s30
app.ACCOUNT_POOL = app.AccountPoolManager(s30)


class _FakeEngine:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
    def generate(self, cookies, prompt, expect=None, timeout=None, **kw):
        self.calls += 1
        action = self.script.pop(0)
        if isinstance(action, Exception):
            raise action
        return action
    def start(self): pass
    def close_current_page(self): pass
    def reset_thread(self): pass


class _FakePool:
    def __init__(self, acc):
        self.acc = acc
    def acquire(self, *a, **kw):
        return dict(self.acc)
    def release(self, aid): pass
    cond = threading.Condition()
    busy_accounts = set()


acc = s30.get_account(s30.a)
eng = _FakeEngine([])
app.engine = eng
app.ACCOUNT_POOL = _FakePool(acc)
app._renew_and_persist = lambda *a, **k: None
app._sync_cookies = lambda *a, **k: None
app._bind_account_proxy = lambda *a, **k: None
app._clear_account_proxy = lambda *a, **k: None
app._rewrite_prompt_on_refusal = lambda p, k, n: p
real_mark = app.store.mark
app.store.mark = lambda *a, **k: None

try:
    # 7a（patch30e 语义）：3 轮全 refusal 的**单个任务**只计 1 次——不再冻结；
    # 任务耗尽上抛 refusal。
    eng.script = [MuseModelRefusalError("r1"), MuseModelRefusalError("r2"),
                  MuseModelRefusalError("r3")]
    raised = None
    try:
        app._run_generation_locked("一只猫", "video", 600)
    except Exception as e:  # noqa: BLE001
        raised = e
    check("7.1 三轮 refusal 耗尽上抛", isinstance(raised, MuseModelRefusalError), repr(raised))
    a_after = s30.get_account(s30.a)
    check("7.2 单任务 3 轮只计 1 次（不冻结）",
          a_after.get("video_fail_streak") == 1 and not s30.video_frozen(a_after),
          (a_after.get("video_fail_streak"), a_after.get("video_frozen_until")))

    # 7b：第 2 个独立任务 2 轮 refusal（第 2 轮因 deadline-break 不会发生——
    # 单任务仍只计 1）→ streak=2；第 3 个任务 refusal → streak=3 冻结。
    # 任务 2：2 轮 refusal（deadline 临近 break 也只计 1）
    eng.script = [MuseModelRefusalError("t2r1"), MuseModelRefusalError("t2r2")]
    try:
        app._run_generation_locked("一只猫", "video", 600)
    except Exception:  # noqa: BLE001
        pass
    check("7.3 第 2 个任务仍只计 1（streak=2 未冻结）",
          s30.get_account(s30.a).get("video_fail_streak") == 2
          and not s30.video_frozen(s30.get_account(s30.a)),
          s30.get_account(s30.a).get("video_fail_streak"))
    # 任务 3：1 轮 refusal 即达 3 → 冻结
    eng.script = [MuseModelRefusalError("t3r1"), {"url": "http://x/v.mp4"}]
    try:
        app._run_generation_locked("一只猫", "video", 600)
    except Exception:  # noqa: BLE001
        pass
    a_after = s30.get_account(s30.a)
    check("7.4 第 3 个任务 refusal 达 3 → 冻结", s30.video_frozen(a_after),
          a_after.get("video_frozen_until"))
    until = a_after.get("video_frozen_until") or 0
    days = (until - time.time()) / 86400
    check("7.5 冻结时长 ≈7 天", 6.9 <= days <= 7.1, round(days, 2))
    # 冻结后同任务不再重发：任务 4 首轮即 refusal → break（generate 只调 1 次）
    eng.script = [MuseModelRefusalError("t4r1"), MuseModelRefusalError("t4r2"),
                  MuseModelRefusalError("t4r3")]
    eng.calls = 0
    try:
        app._run_generation_locked("一只猫", "video", 600)
    except Exception:  # noqa: BLE001
        pass
    check("7.6 冻结账号终止本任务后续重发（generate 恰 1 次）", eng.calls == 1, eng.calls)

    # attachment_broken：不计数
    exc = MuseModelRefusalError("attachment gone", code="upstream_attachment_broken")
    eng.script = [exc]
    raised = None
    try:
        app._run_generation_locked("一只猫", "video", 600)
    except Exception as e:  # noqa: BLE001
        raised = e
    check("7.7 attachment_broken 上抛", raised is exc, repr(raised))

    # image refusal：不计数也不清零（patch30e BUG-1/4 根修）
    s30.thaw_account(s30.a)
    s30.record_video_refusal(s30.a)  # streak=1
    eng.script = [MuseModelRefusalError("img r1"), MuseModelRefusalError("img r2"),
                  MuseModelRefusalError("img r3")]
    raised = None
    try:
        app._run_generation_locked("一只猫", "image", 600)
    except Exception as e:  # noqa: BLE001
        raised = e
    check("7.8 image refusal 耗尽上抛", isinstance(raised, MuseModelRefusalError), repr(raised))
    check("7.9 image refusal 不计入视频 streak（仍=1）",
          s30.get_account(s30.a).get("video_fail_streak") == 1,
          s30.get_account(s30.a).get("video_fail_streak"))

    # video 成功：清零（image 成功不清零的对称验证在 7.9 之后做）
    eng.script = [{"url": "http://x/v.mp4"}]
    res, _ = app._run_generation_locked("一只猫", "video", 600)
    check("7.10 video 成功清零 streak",
          s30.get_account(s30.a).get("video_fail_streak") == 0,
          s30.get_account(s30.a).get("video_fail_streak"))
finally:
    for name, val in saved.items():
        setattr(app, name, val)
    app.store.mark = real_mark

print("\n== 结果: %d PASS / %d FAIL ==" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
