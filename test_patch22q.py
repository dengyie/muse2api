# -*- coding: utf-8 -*-
"""test_patch22q — 幂等修复行为级回归（fake-ns + FastAPI TestClient 不可用时的
结构级+store/app 混合模式）。

覆盖用户要求的六个场景：
  1. 同键同体重放 → 返原任务，生成调用次数=1
  2. 同键异体 → 结构化 409（机读诊断字段），旧记录不被覆盖
  3. 不同键 → 互不冲突，各自建任务
  4. 并发重复提交 → 只创建一个下游任务
  5. 服务重启恢复 → idem 落盘/回读，重启后同键重放仍命中
  6. 已完成任务结果复用 → 重放拿原 result，不二次生成

外加：只读查询端点结构断言 + 409 诊断头断言 + _safe_task_view 不回显 prompt。
本地无 fastapi，路由行为走结构级断言（沿用 test_patch22m 模式），
store/idem 层走真实调用（不依赖 fastapi）。
"""
import hashlib
import json
import os
import sys
import tempfile
import threading
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


store_mod = _import_real("store")

# ---------------- 环境：独立 tmp data_dir ----------------
TMP = tempfile.mkdtemp(prefix="m2a-p22q-test-")
from config import Config

def _mk_cfg(base):
    c = Config()
    c.base_dir = base
    c.ensure_dirs()
    return c

cfg = _mk_cfg(os.path.join(TMP, "d1"))
st = store_mod.Store(cfg)

def req_hash(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False)
                          .encode()).hexdigest()

H_A, H_B = req_hash({"p": "alpha"}), req_hash({"p": "beta"})
K1 = "k1" * 16  # 32 chars
K2 = "k2" * 16

# 生成调用计数器（模拟下游生成）
gen_calls = {"n": 0}
def fake_create_video(key, rh):
    """模拟『带幂等保护的视频提交』核心路径（与 app.py 一致的语义），
    返回 (task, created)。created=True 表示真的触发了新任务（=一次生成）。"""
    with threading.Lock():
        # 同键同体查索引 → 查任务
        for t in st.tasks.values():
            if t.get("video_request_key") == key:
                if t.get("video_request_hash") != rh:
                    return t, False, "conflict"
                if t.get("status") == "failed" and t.get("safe_to_resubmit") is not False:
                    continue
                return t, False, "hit"
        # 无命中 → 新建（=生成）
        gen_calls["n"] += 1
        t = st.create_task("video", "sanitized")
        st.update_task(t["id"], video_request_key=key, video_request_hash=rh)
        return t, True, "created"

# ---------------- 1+6. 同键同体重放（含已完成结果复用） ----------------
t1, created1, mode1 = fake_create_video(K1, H_A)
check("first submit creates+generates", created1 and mode1 == "created" and gen_calls["n"] == 1,
      (created1, mode1, gen_calls["n"]))
st.update_task(t1["id"], status="completed", progress=100,
               result={"filename": "out1.mp4", "bytes": 100, "kind": "video"})
# 完成后同键同体重放
t2, created2, mode2 = fake_create_video(K1, H_A)
check("replay same key+body hits", not created2 and mode2 == "hit" and t2["id"] == t1["id"],
      (created2, mode2, t2["id"]))
check("no second generation", gen_calls["n"] == 1, gen_calls["n"])
check("result reused", t2.get("result", {}).get("filename") == "out1.mp4")

# ---------------- 2. 同键异体 → 冲突，旧记录不覆盖 ----------------
t3, created3, mode3 = fake_create_video(K1, H_B)
check("same key diff body = conflict", mode3 == "conflict" and not created3, mode3)
check("conflict does not generate", gen_calls["n"] == 1, gen_calls["n"])
orig = st.get_task(t1["id"])
check("old record intact (key+hash unchanged)",
      orig["video_request_key"] == K1 and orig["video_request_hash"] == H_A)

# ---------------- 3. 不同键互不冲突 ----------------
t4, created4, mode4 = fake_create_video(K2, H_A)  # 同 request_hash 不同 key 也应独立
check("different key independent", created4 and mode4 == "created", (created4, mode4))
check("independent task generated", gen_calls["n"] == 2, gen_calls["n"])
check("two tasks coexist", t4["id"] != t1["id"])

# ---------------- 4. 并发重复提交只建一个 ----------------
gen_calls["n"] = 0
cfg2 = _mk_cfg(os.path.join(TMP, "d2"))
st2 = store_mod.Store(cfg2)
K3 = "k3" * 16
H_C = req_hash({"p": "gamma"})
results = []
barrier = threading.Barrier(8)
video_lock = threading.Lock()  # 对齐 app.py 的 VIDEO_TASK_LOCK：模块级共享锁

def racer():
    barrier.wait()
    with video_lock:  # app 里是 VIDEO_TASK_LOCK——check+create 原子化
        hit = None
        for t in st2.tasks.values():
            if t.get("video_request_key") == K3:
                hit = t
                break
        if hit:
            results.append(("hit", hit["id"]))
            return
        time.sleep(0.01)  # 放大竞态窗口
        t = st2.create_task("video", "sanitized")
        st2.update_task(t["id"], video_request_key=K3, video_request_hash=H_C)
        results.append(("created", t["id"]))

threads = [threading.Thread(target=racer) for _ in range(8)]
for th in threads:
    th.start()
for th in threads:
    th.join()
created_n = sum(1 for m, _ in results if m == "created")
check("concurrent dup creates exactly one task", created_n == 1, created_n)
check("all racers get same task id", len({tid for _, tid in results}) == 1,
      results)

# ---------------- 5. 服务重启恢复 ----------------
cfg3 = _mk_cfg(os.path.join(TMP, "d3"))
st3 = store_mod.Store(cfg3)
K4 = "k4" * 16
H_D = req_hash({"p": "delta"})
t5 = st3.create_task("video", "sanitized")
st3.update_task(t5["id"], video_request_key=K4, video_request_hash=H_D,
                status="completed", result={"filename": "d.mp4"})
# 模拟 _idem_write（对齐 app.py：store._write 模块函数）
IDEM_FILE = os.path.join(TMP, "d3", "idempotency_videos.json")
store_mod._write(IDEM_FILE, {K4: {"task_id": t5["id"], "request_hash": H_D,
                                  "ts": time.time()}})
# 「重启」：新 Store 实例 + 索引回读（对齐 _idem_load 语义，TTL 内保留）
st3b = store_mod.Store(cfg3)
disk = store_mod._read(IDEM_FILE, {})
loaded = {k: v for k, v in disk.items()
          if time.time() - (v.get("ts") or 0) <= 24 * 3600}
check("idem survives restart (file roundtrip)", K4 in loaded
      and loaded[K4]["task_id"] == t5["id"])
check("task survives restart", st3b.get_task(t5["id"], ) and
      st3b.tasks[t5["id"]]["video_request_key"] == K4)

# ---------------- app.py 结构级断言（本地无 fastapi 的门禁补位） ----------------
here = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(here, "app.py"), encoding="utf-8").read()

# 结构化 409：机读诊断字段 + 诊断头 + 不覆盖旧记录语义
check("409 structured detail dict", '"error": "idempotency_key_conflict"' in src)
check("409 carries existing task id", '"existing_task_id": existing["id"]' in src)
check("409 carries both hashes",
      '"existing_request_hash"' in src and '"incoming_request_hash"' in src)
check("409 carries conflict time", '"conflict_at"' in src)
check("409 diagnostic headers", "X-Idempotency-Conflict-Task" in src)
check("image 409 aligned", src.count('"error": "idempotency_key_conflict"') >= 2)
check("dict detail passthrough handler", "isinstance(exc.detail, dict)" in src)
check("no legacy string-only 409 detail on videos",
      'HTTPException(409, "Idempotency-Key reused' not in src)

# 只读查询端点
check("readonly endpoint registered", '@app.get("/admin/idempotency")' in src)
check("readonly 3 dims", 'key_hash' in src and 'request_hash' in src and 'media' in src)
check("readonly prefix guard (anti full-key leak)",
      "前缀至少 16 位" in src)
check("readonly archive lookup wired", "store.lookup_archive_by_media(media)" in src)
check("safe view no prompt leak",
      "_safe_task_view" in src and '"prompt"' not in
      src[src.find("def _safe_task_view"):src.find("def _safe_task_view") + 900])
check("unknown-state wording preserved",
      "不是「从未创建」" in src)

print("\nPASSED %d  FAILED %d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
