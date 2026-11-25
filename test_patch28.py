# -*- coding: utf-8 -*-
"""test_patch28 — refusal 改写重试链回归（stub 驱动 _run_generation_locked 状态机）。

背景（生产实证）：merge upstream 1.6.11 时循环顶部曾加
isinstance(last_exc, MuseModelRefusalError) 短路——refusal 在下一轮循环开头
直接 raise，下方 attempt>0 的「改写 prompt 重发」分支成死代码（改写日志计数 0，
3 笔只回文本全 attempts=1 落终态）。patch28 移除短路，回归 fork 原生语义。

覆盖：
  1. refusal（只回文本）→ 不 raise、不换号、prompt 被改写、generate 被再次调用
  2. 第二轮成功 → 正常返回，attempts=2（重发那次真实付费已入账）
  3. 连续 3 轮 refusal → 重试耗尽后 raise 的是最后一次 MuseModelRefusalError
  4. upstream_attachment_broken → 快速失败（不烧重试链，generate 只调 1 次）
  5. 非 refusal 异常（auth/proxy）→ 仍走切号逻辑（rotate 被调用）
  6. deadline 临近（<30s 剩余）→ refusal 不再重发，直接 break raise
"""
import os
import sys

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
from engine import MuseModelRefusalError, MuseAuthError  # noqa: E402


class _FakeAccountPool:
    """最小租约语义：acquire 返回预置账号，rotate 计数供断言。"""

    def __init__(self, acc):
        self.acc = acc
        self.rotate_calls = 0
        self.released = []

    def acquire(self, account_id=None, preferred_id=None, rotate=False,
                force_rotate=False, timeout=None, **kw):
        if force_rotate:
            self.rotate_calls += 1
            alt = dict(self.acc)
            alt["id"] = "alt_" + self.acc["id"]   # 切号语义：返回不同账号
            return alt
        return dict(self.acc)

    def release(self, aid):
        self.released.append(aid)

    @property
    def cond(self):
        import threading
        return threading.Lock()

    busy_accounts = set()


def _install(stub, acc_id="f519883a14b2"):
    """把 stub 注入 app 模块全局（测完由调用方恢复）。"""
    saved = {}
    for name, val in (("ACCOUNT_POOL", app.ACCOUNT_POOL),
                      ("engine", app.engine),
                      ("_renew_and_persist", app._renew_and_persist),
                      ("_sync_cookies", app._sync_cookies),
                      ("_bind_account_proxy", app._bind_account_proxy),
                      ("_clear_account_proxy", app._clear_account_proxy)):
        saved[name] = val
        setattr(app, name, stub.get(name, val))
    return saved


def _restore(saved):
    for name, val in saved.items():
        setattr(app, name, val)


class _FakeEngine:
    """generate 行为由外部脚本控制；页面管理方法全部空转。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []            # [(prompt, attempt_no)] 由 on_acquired 侧记录
        self.started = 0

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


def _setup_case(script, acc=None):
    acc = acc or {"id": "f519883a14b2", "label": "p28", "enabled": True,
                  "cookies": {"hatch_sess": "s28"}, "cookies_exp": {},
                  "proxy": {}}
    pool = _FakeAccountPool(acc)
    eng = _FakeEngine(script)
    saved = _install({"ACCOUNT_POOL": pool, "engine": eng,
                      "_renew_and_persist": lambda *a, **k: None,
                      "_sync_cookies": lambda *a, **k: None,
                      "_bind_account_proxy": lambda *a, **k: None,
                      "_clear_account_proxy": lambda *a, **k: None})
    # store.mark/touch_keepalive 打桩（真实 store 会写盘）
    real_mark = app.store.mark
    app.store.mark = lambda *a, **k: None
    return pool, eng, saved, real_mark


print("== 1. refusal 改写重发 ==")
pool, eng, saved, real_mark = _setup_case(
    [MuseModelRefusalError("only text"), {"url": "http://x/video.mp4"}])
try:
    prompts_seen = []
    orig_rewrite = app._rewrite_prompt_on_refusal

    def spy_rewrite(prompt, kind, attempt_no):
        prompts_seen.append((prompt, attempt_no))
        return orig_rewrite(prompt, kind, attempt_no)

    app._rewrite_prompt_on_refusal = spy_rewrite
    res, aid = app._run_generation_locked("一只猫", "video", 120)
    check("1.1 第二轮成功返回", res == {"url": "http://x/video.mp4"} and aid == "f519883a14b2")
    check("1.2 generate 恰好 2 次", len(eng.calls) == 2, len(eng.calls))
    check("1.3 prompt 被改写（含强制出片指令）",
          len(prompts_seen) == 1 and prompts_seen[0][0] == "一只猫"
          and len(eng.calls[1]) > len("一只猫"), (prompts_seen, eng.calls))
    check("1.4 不换号（rotate 未调用）", pool.rotate_calls == 0, pool.rotate_calls)
finally:
    app._rewrite_prompt_on_refusal = orig_rewrite
    app.store.mark = real_mark
    _restore(saved)

print("== 2. 连续 3 轮 refusal → 耗尽 raise 最后一次 refusal ==")
pool, eng, saved, real_mark = _setup_case([
    MuseModelRefusalError("r1"), MuseModelRefusalError("r2"), MuseModelRefusalError("r3")])
try:
    app.store.mark = lambda *a, **k: None
    raised = None
    try:
        app._run_generation_locked("一只猫", "video", 600)
    except MuseModelRefusalError as exc:
        raised = str(exc)
    except Exception as exc:                            # noqa: BLE001
        raised = "WRONG:" + type(exc).__name__
    check("2.1 raise 的是 MuseModelRefusalError", raised is not None and "WRONG" not in str(raised),
          str(raised))
    check("2.2 generate 3 次全烧完", len(eng.calls) == 3, len(eng.calls))
finally:
    app.store.mark = real_mark
    _restore(saved)

print("== 3. upstream_attachment_broken 快速失败 ==")
pool, eng, saved, real_mark = _setup_case([
    MuseModelRefusalError("attachment gone", )])
# 给异常打上 code（模拟 patch9 的 upstream_attachment_broken）
try:
    app.store.mark = lambda *a, **k: None
    pool, eng, saved, real_mark = _setup_case([
        MuseModelRefusalError("attachment gone")])
    exc = MuseModelRefusalError("attachment gone")
    exc.code = "upstream_attachment_broken"
    pool, eng, saved2, real_mark = _setup_case([exc])
    raised = None
    try:
        app._run_generation_locked("一只猫", "video", 600)
    except Exception as e:                              # noqa: BLE001
        raised = e
    check("3.1 原样上抛 attachment_broken", raised is exc, repr(raised))
    check("3.2 不烧重试链（generate 1 次）", len(eng.calls) == 1, len(eng.calls))
finally:
    app.store.mark = real_mark
    _restore(saved)
    try:
        _restore(saved2)
    except Exception:                                   # noqa: BLE001
        pass

print("== 4. 非 refusal（auth）仍走切号 ==")
pool, eng, saved, real_mark = _setup_case([
    MuseAuthError("auth dead"), {"url": "http://x/img.png"}])
try:
    app.store.mark = lambda *a, **k: None
    res, _ = app._run_generation_locked("一只猫", "image", 300)
    check("4.1 切号后成功", res == {"url": "http://x/img.png"})
    check("4.2 force_rotate 被调用", pool.rotate_calls >= 1, pool.rotate_calls)
finally:
    app.store.mark = real_mark
    _restore(saved)

print("== 5. deadline 临近 refusal 不再重发 ==")
pool, eng, saved, real_mark = _setup_case([
    MuseModelRefusalError("late refusal"), MuseModelRefusalError("late refusal 2")])
try:
    app.store.mark = lambda *a, **k: None
    raised = None
    try:
        import time as _t
        # deadline 剩余 <30s：refusal 改写分支的「剩余时间不足再出一轮就放弃」
        # 兜底生效——不再重发直接 break raise 原异常（而非烧满 3 轮）
        app._run_generation_locked("一只猫", "video", 600,
                                   deadline=_t.monotonic() + 10)
    except Exception as e:                              # noqa: BLE001
        raised = e
    check("5.1 剩余不足放弃重发，raise 原 refusal",
          isinstance(raised, MuseModelRefusalError), repr(raised))
    check("5.2 generate 只烧 1 次（不重发）", len(eng.calls) == 1, len(eng.calls))
finally:
    app.store.mark = real_mark
    _restore(saved)

print("\n== 结果: %d PASS / %d FAIL ==" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
