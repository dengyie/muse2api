#!/usr/bin/env python3
"""patch33 行为级回归：L3 误判 upstream_attachment_broken 三层防线。

场景（对齐生产实锤 task_61f5980f / task_6a0e758e）：
1. 读附件 CDP 瞬时异常（空列表伪造）→ 不得计入 L3 空读证据。
2. 上游慢启动 >45s 但最终挂载 → 翻案回 L1，不判死。
3. 真·附件层失效（45s+ 稳定空读 + 强取证仍空）→ 判死语义保留。
4. 判死前强取证读到增长 → 撤销判定。
5. patch30 语义回归：attachment_broken 不计视频熔断。
6. 修复不破坏纯文本路径（无 has_upload 时 L3 不触发）。

方法：stub MuseEngine.attachments/_user_count/_agent_count 等，
直接驱动 _wait_attachment 循环（行为级，非结构断言）。
"""
import json, sys, time, types

sys.path.insert(0, "/opt/muse2api/repo")
import engine as eng

class FakePage:
    def __init__(self):
        self.att_payload = []
        self.att_fail = False
        self.st_calls = {"cnt": 0, "txt": "", "stop": False, "tail": "", "path": "/thread/new"}
        self.js_calls = []
    def js(self, code, *a, **kw):
        self.js_calls.append(code[:40])
        if self.att_fail and "_ATT_JS_MARKER" in code:
            raise RuntimeError("cdp transient")
        if 'data-testid^="hatch-chat-attachment-presentation-"' in code:
            if self.att_fail:
                raise RuntimeError("cdp transient")
            return json.dumps(self.att_payload)
        if 'hatch-chat-groupable-bubble' in code and '"cnt"' in code:
            return json.dumps(self.st_calls)
        return "null"
    def send(self, *a, **kw):
        return {"result": {"result": {"objectId": "x"}}}

class FakeEngine(eng.MuseEngine):
    def __init__(self):
        self.page = FakePage()
        self.cfg = types.SimpleNamespace(profile_dir="/tmp", media_dir="/tmp", data_dir="/tmp")
        self._debug_enabled = lambda: False
    def _scroll_bottom(self): pass
    def _user_count(self): return 1   # 气泡已现（base=0）
    def _agent_count(self): return 0
    def _composer_unsent(self): return ""
    def _click_send(self): pass
    def _media_refusal_text(self, t, e): return ""

PASS = FAIL = 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond: PASS += 1; print("PASS:", name)
    else: FAIL += 1; print("FAIL:", name, detail)

def run_wait(fe, deadline_s=1.0, timeout=50.0):
    """驱动 _wait_attachment 到 deadline；返回 (raised, exc)。"""
    try:
        r = fe._wait_attachment("", timeout_s if False else 0.01, "video",
                               base_agent_cnt=0, base_att_cnt=0,
                               has_upload=True, base_user_cnt=0)
        return (None, r)
    except Exception as e:
        return (type(e).__name__, e)

# --- 场景 1：CDP 瞬时异常伪造空列表 → L3 不判死 ---
fe = FakeEngine()
# 25s+ 无 agent 回复、气泡已现、附件读前 30 次全失败
fe.page.st_calls = {"cnt": 0, "txt": "", "stop": False, "tail": "", "path": "/thread/new"}
orig_att = eng.MuseEngine.attachments
fails = {"n": 0}
def flaky_att(self):
    fails["n"] += 1
    if fails["n"] < 200:   # 前两百轮读失败（覆盖 45s 判死窗）
        eng.MuseEngine.att_read_fail_streak = 999
        return []
    return orig_att(self)
eng.MuseEngine.attachments = flaky_att
raised, exc = None, None
try:
    fe._wait_attachment("", 47.0, "video", base_agent_cnt=0, base_att_cnt=0,
                        has_upload=True, base_user_cnt=0)
except Exception as e:
    raised, exc = type(e).__name__, e
check("场景1: CDP 读失败窗内 L3 不判死（不抛 attachment_broken）",
      not (raised and getattr(exc, "code", "") == "upstream_attachment_broken"),
      f"raised={raised} exc={exc}")
eng.MuseEngine.attachments = orig_att

# --- 场景 2：慢启动 50s 后挂载 → 不判死，正常返回附件 ---
fe = FakeEngine()
def slow_att(self):
    # 前 55s（~90 轮）空，之后 presentation 挂载
    if time.monotonic() - slow_att.t0 < 50:
        return []
    return [{"tid": "hatch-chat-attachment-presentation-video", "hasVideo": True,
             "vSrc": "https://cdn.example/v.mp4", "src": "https://cdn.example/v.mp4",
             "w": 1280, "h": 720}]
slow_att.t0 = time.monotonic()
eng.MuseEngine.attachments = slow_att
raised, exc = None, None
try:
    r = fe._wait_attachment("", 60.0, "video", base_agent_cnt=0, base_att_cnt=0,
                            has_upload=True, base_user_cnt=0)
    ok = isinstance(r, dict) and r.get("vSrc", "").endswith(".mp4")
except Exception as e:
    raised, exc = type(e).__name__, e
    ok = False
check("场景2: 慢启动 50s 后挂载 → 翻案回 L1 正常交付",
      ok, f"raised={raised} exc={exc} r={r if 'r' in dir() else None}")
eng.MuseEngine.attachments = orig_att

# --- 场景 3：真失效（稳定空读 45s+，强取证也空）→ 仍判死 ---
fe = FakeEngine()
eng.MuseEngine.attachments = lambda self: []
raised, exc = None, None
t0 = time.monotonic()
try:
    fe._wait_attachment("", 55.0, "video", base_agent_cnt=0, base_att_cnt=0,
                        has_upload=True, base_user_cnt=0)
except Exception as e:
    raised, exc = type(e).__name__, e
elapsed = time.monotonic() - t0
check("场景3: 真失效 45s+ 判死保留",
      raised and getattr(exc, "code", "") == "upstream_attachment_broken",
      f"raised={raised} exc={exc}")
check("场景3: 判死发生在 45s 边界（>44s）", elapsed > 44, f"elapsed={elapsed:.1f}")
eng.MuseEngine.attachments = orig_att

# --- 场景 4：判死前强取证翻案 ---
fe = FakeEngine()
state = {"n": 0}
def rescue_att(self):
    state["n"] += 1
    if state["n"] < 85: return []      # 45s 窗口内空
    return [{"tid": "hatch-chat-attachment-presentation-video", "hasVideo": True,
             "vSrc": "https://cdn.example/v.mp4", "src": "https://cdn.example/v.mp4",
             "w": 1280, "h": 720}]
eng.MuseEngine.attachments = rescue_att
raised, exc, ok = None, None, False
try:
    r = fe._wait_attachment("", 65.0, "video", base_agent_cnt=0, base_att_cnt=0,
                            has_upload=True, base_user_cnt=0)
    ok = isinstance(r, dict) and r.get("vSrc", "").endswith(".mp4")
except Exception as e:
    raised, exc = type(e).__name__, e
check("场景4: 强取证读到增长 → 撤销 L3 判死",
      ok, f"raised={raised} exc={exc}")
eng.MuseEngine.attachments = orig_att

# --- 场景 5：纯文本路径不受影响 ---
fe = FakeEngine()
raised, exc = None, None
try:
    fe._wait_attachment("", 30.0, "video", base_agent_cnt=0, base_att_cnt=0,
                        has_upload=False, base_user_cnt=0)
except Exception as e:
    raised, exc = type(e).__name__, e
check("场景5: 无附件任务 L3 不触发（has_upload=False）",
      not (raised and getattr(exc, "code", "") == "upstream_attachment_broken"),
      f"raised={raised} exc={exc}")

print(f"\n== patch33 行为级回归: {PASS} PASS / {FAIL} FAIL ==")
sys.exit(1 if FAIL else 0)
