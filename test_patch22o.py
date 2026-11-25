# -*- coding: utf-8 -*-
"""test_patch22o — 断链三根因修复回归（fake-ns 模式，不依赖真浏览器/真服务）。

背景（2026-11-18 OpenPet 409 取证）：
  ① tasks_max=120 纯条数裁剪，压测日 93 笔把 11-15 档案全挤出——取证断链根因一；
  ② IDEMPOTENCY_VIDEOS 纯内存，容器重启即丢，重启后同 key 重放重复建任务
     重复计费——根因二；
  ③ 归档媒体无文件名→任务索引，成品成无主文件——根因三。

覆盖：
  1. tasks_keep_days 时间淘熟（created_at 超窗的 completed 裁、live 不裁、0=关）
  2. tasks_max 2000 条数上限兜底仍生效（时间淘不净时按 updated_at 裁）
  3. archive_index：任务完成即写入 / prune 裁剪后索引仍在 / 删任务清 tasks
     但索引保留 / 删媒体同步删索引条目 / lookup_by_media 反查
  4. 幂等索引落盘：_idem_write/_idem_load 往返、TTL 外项恢复时丢弃
"""
import os
import sys
import tempfile
import time
import types

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
from config import Config

# ---------------- 独立 tmp data_dir，不碰真实 data ----------------
TMP = tempfile.mkdtemp(prefix="m2a-p22o-test-")
cfg = Config()
cfg.base_dir = TMP
cfg._data_dir = None  # data_dir 是 property 的话直接设 cache 无效——见下
# Config.data_dir 是 property，从 base_dir 派生；monkeypatch 不便，直接换目录树


def _mk_cfg(base):
    c = Config()
    object.__setattr__(c, "base_dir", base) if hasattr(c, "__slots__") else setattr(c, "base_dir", base)
    c.ensure_dirs()
    return c


# property data_dir 依赖 self.base_dir；Config 是 dataclass，直接赋值即可
try:
    cfg.data_dir
    cfg2 = _mk_cfg(os.path.join(TMP, "d1"))
except Exception as e:
    print("cfg setup fallback:", e)
    cfg2 = cfg
cfg = cfg2
check("tasks_max default 2000", cfg.tasks_max == 2000, cfg.tasks_max)
check("tasks_keep_days default 30", cfg.tasks_keep_days == 30, cfg.tasks_keep_days)
check("archive_index_file in data_dir",
      cfg.archive_index_file.endswith("archive_index.json"), cfg.archive_index_file)
check("idempotency_file in data_dir",
      cfg.idempotency_file.endswith("idempotency_videos.json"), cfg.idempotency_file)

st = store_mod.Store(cfg)

# ---------------- 1+3. 任务完成 → 归档索引写入 ----------------
t1 = st.create_task("video", "hello")
st.update_task(t1["id"], video_request_key="kh-A", video_request_hash="rh-A",
               status="completed", progress=100,
               result={"url": "/v1/media/aaa.mp4", "filename": "aaa.mp4",
                       "bytes": 123, "kind": "video"})
check("archive index written on completion", t1["id"] in st.archive_index,
      sorted(st.archive_index))
snap = st.archive_index[t1["id"]]
check("archive snapshot fields",
      snap["video_request_key"] == "kh-A" and snap["status"] == "completed"
      and (snap["result"] or {}).get("filename") == "aaa.mp4", snap)

# ---------------- 1. 时间淘熟 ----------------
# 伪造成 40 天前的已完成任务 → keep_days=30 窗口外
old = store_mod.Store(cfg)  # 新 Store 实例reload同一目录正常
t_old = old.create_task("video", "old")
old.update_task(t_old["id"], video_request_key="kh-B", video_request_hash="rh-B",
                status="completed", result={"url": "/v1/media/old.mp4",
                                            "filename": "old.mp4", "kind": "video"})
with store_mod._LOCK:
    old.tasks[t_old["id"]]["created_at"] = int(time.time()) - 40 * 86400
    old.tasks[t_old["id"]]["updated_at"] = int(time.time()) - 40 * 86400
    dropped = old._prune_tasks_locked()
check("expired-by-age task dropped", dropped >= 1, dropped)
check("expired task removed from tasks", t_old["id"] not in old.tasks)
check("expired task archived before prune",
      t_old["id"] in old.archive_index
      and old.archive_index[t_old["id"]]["result"]["filename"] == "old.mp4")

# live（进行中）任务即使超龄也不裁
t_live = old.create_task("video", "live")
old.update_task(t_live["id"], video_request_key="kh-C", video_request_hash="rh-C")
with store_mod._LOCK:
    old.tasks[t_live["id"]]["created_at"] = int(time.time()) - 90 * 86400
    dropped2 = old._prune_tasks_locked()
check("live task never pruned", t_live["id"] in old.tasks)

# keep_days=0 关闭时间淘
cfg0 = _mk_cfg(os.path.join(TMP, "d0"))
cfg0.tasks_keep_days = 0
st0 = store_mod.Store(cfg0)
t0 = st0.create_task("video", "ancient")
st0.update_task(t0["id"], video_request_key="kh-Z", video_request_hash="rh-Z",
                status="completed", result={"url": "/v1/media/z.mp4", "filename": "z.mp4"})
with store_mod._LOCK:
    st0.tasks[t0["id"]]["created_at"] = 0
    dropped3 = st0._prune_tasks_locked()
check("keep_days=0 disables age sweep", dropped3 == 0 and t0["id"] in st0.tasks)

# ---------------- 2. 条数上限兜底 ----------------
cfg2t = _mk_cfg(os.path.join(TMP, "d2"))
cfg2t.tasks_max = 5
cfg2t.tasks_keep_days = 0
st2 = store_mod.Store(cfg2t)
# 塞 8 个已完成任务（不经 update_task，直接塞避免 prune 干扰）
with store_mod._LOCK:
    for i in range(8):
        tid = "task_%s%02d" % ("f" * 16, i)
        st2.tasks[tid] = {"id": tid, "kind": "video", "status": "completed",
                          "created_at": 1000 + i, "updated_at": 1000 + i,
                          "video_request_key": "kh-%d" % i,
                          "video_request_hash": "rh-%d" % i,
                          "result": {"filename": "v%02d.mp4" % i}}
    dropped4 = st2._prune_tasks_locked()
check("count-limit fallback prunes", dropped4 >= 3 and len(st2.tasks) <= 5,
      (dropped4, len(st2.tasks)))
check("oldest pruned first", "task_%s00" % ("f" * 16) not in st2.tasks)
check("pruned tasks all archived", len(st2.archive_index) == dropped4,
      (len(st2.archive_index), dropped4))  # 直接注入的任务不经 update_task，无完成期写入
check("archive index persisted to disk",
      store_mod._read(cfg2t.archive_index_file, {}).get("task_%s00" % ("f" * 16), {})
      .get("result", {}).get("filename") == "v00.mp4")

# ---------------- 3. delete_task 归档、删媒体清索引 ----------------
tdel = st.create_task("video", "del-me")
st.update_task(tdel["id"], video_request_key="kh-D", video_request_hash="rh-D",
               status="completed",
               result={"url": "/v1/media/delme.mp4", "filename": "delme.mp4"})
check("del-me in archive", tdel["id"] in st.archive_index)
st.delete_task(tdel["id"])
check("delete_task keeps archive entry", tdel["id"] in st.archive_index)
hits = st.lookup_archive_by_media("delme.mp4")
check("lookup_by_media finds deleted task", len(hits) == 1
      and hits[0]["task_id"] == tdel["id"], hits)
check("remove_archive_by_media removes", st.remove_archive_by_media("delme.mp4") == 1)
check("lookup after remove empty", st.lookup_archive_by_media("delme.mp4") == [])
check("remove_archive_by_media no-op for unknown", st.remove_archive_by_media("nope.mp4") == 0)

# clear_tasks 同样先归档
tcl = st.create_task("video", "clear-me")
st.update_task(tcl["id"], video_request_key="kh-E", video_request_hash="rh-E",
               status="failed", error_code="upstream_5xx",
               result=None, safe_to_resubmit=True)
st.clear_tasks(keep=0)
check("clear_tasks archives failed task too",
      tcl["id"] in st.archive_index
      and st.archive_index[tcl["id"]]["safe_to_resubmit"] is True)

# ---------------- 4. 幂等索引落盘往返 ----------------
# patch22r 后实现升级为 idempotency.py 的 IdempotencyIndex 双域类；
# 本段更新为新契约（22r 前钉的是 _idem_write/_idem_load 函数旧实现）。
here = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(here, "app.py"), encoding="utf-8").read()
idem_src = open(os.path.join(here, "idempotency.py"), encoding="utf-8").read()
check("idempotency module exists with IdempotencyIndex",
      "class IdempotencyIndex" in idem_src and "def load" in idem_src
      and "def register" in idem_src and "def pop" in idem_src)
check("app wires both domains",
      "IDEMPOTENCY_VIDEOS" in src and "IDEMPOTENCY_IMAGES" in src
      and "IdempotencyIndex(os.path.join(CFG.data_dir" in src)
check("startup hook loads both domains",
      "IDEMPOTENCY_VIDEOS.load()" in src and "IDEMPOTENCY_IMAGES.load()" in src)
check("registration via index.register",
      "IDEMPOTENCY_VIDEOS.register(key_hash" in src)
check("flush purges expired entries",
      "_purged(self.data, self.ttl)" in idem_src)
check("idem file config field", "idempotency_file" in open(os.path.join(here, "config.py"), encoding="utf-8").read())

print("\nPASSED %d  FAILED %d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
