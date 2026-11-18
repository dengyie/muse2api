# -*- coding: utf-8 -*-
"""test_patch22r — review 全量根因修复回归（真实 TestClient 模式）。

patch22r 起本地可装 fastapi（requirements-dev.txt），测试从 fake-ns 平行实现
升级为真实 app 路径（22o-fix 启动循环类事故的门禁就此闭环）。

覆盖（对应 review P1×4 + P2×16 + P3 择要）：
  1. SSE 断连安全 _put（P1-1）
  2. extract stop_event 接线（P1-2，结构）
  3. store 写序 archive→tasks（P1-3）+ fsync + 损坏留证
  4. IdempotencyIndex 双域（P2 image 不对称 + purge）
  5. archive_index 上限淘汰（P3）
  6. delete_task only_terminal + clear_tasks 单快照（P2 TOCTOU）
  7. attempt 双计数拆分（P2，结构）
  8. CORS 接线 + docs 关闭（P3/P2）
  9. key 生命周期（P1/P2：日志脱敏 + runtime.env 双写 + chmod）
  10. GET /v1/videos/{id} 白名单（P2/P3 prompt 收敛）
  11. media 负缓存（P2 放大面）
  12. 心跳占位放行（P3）
  13. queue_timeout ge=1 + QueuePolicy 坏文件容错（P3）
  14. 部署面钉版（P1 compose 漂移 + MANAGED + HEALTHCHECK，结构）
  15. 扩展/admin 面（P2 XSS/?key=，结构）
"""
import hashlib
import importlib
import json
import os
import sys
import tempfile
import threading
import time

_here = os.path.dirname(os.path.abspath(__file__))
# 独立 tmp data dir（在 import app 之前设置——config 读 MUSE2API_HOME）
TMP = tempfile.mkdtemp(prefix="m2a-p22r-test-")
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
import app  # noqa: E402  真实导入（本地装 fastapi 后首次全量可达）
import idempotency as idem_mod  # noqa: E402
import store as store_mod  # noqa: E402
import generation_timing as gt_mod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app.app)

# ---------------- 0. import/启动级冒烟（22o-fix 类事故的门禁） ----------------
check("app imports with real fastapi", callable(app.app))
check("healthz 200", client.get("/healthz").status_code == 200)
check("docs disabled", client.get("/docs").status_code == 404
      and client.get("/openapi.json").status_code == 404)

# ---------------- 1. SSE 断连安全 _put ----------------
src = open(os.path.join(_here, "app.py"), encoding="utf-8").read()
check("SSE _put helper exists", "def _put(kind, val, wait=5.0)" in src)
check("SSE put with timeout loop",
      "q.put((kind, val), timeout=0.5)" in src and "except Full:" in src)
check("SSE stream uses _put for data/done",
      '_put("data", chunk)' in src and '_put("done", None)' in src)
check("SSE stop check before blocking semantics",
      "stop_event.is_set() or time.monotonic() >= deadline" in src)

# 行为级：直接复刻 _put 语义验证（队列满 + stop 置位 → 放弃不阻塞）
import queue as _q  # noqa: E402

q = _q.Queue(maxsize=1)
stop_event = threading.Event()


def put_like_app(kind, val, wait=0.3):
    deadline = time.monotonic() + wait
    while True:
        try:
            q.put((kind, val), timeout=0.05)
            return True
        except _q.Full:
            if stop_event.is_set() or time.monotonic() >= deadline:
                return False


q.put(("data", "first"))  # 填满
stop_event.set()
t0 = time.monotonic()
ok_put = put_like_app("data", "second", wait=1.0)
check("put gives up after stop_event set (no deadlock)",
      ok_put is False and time.monotonic() - t0 < 0.8,
      (ok_put, time.monotonic() - t0))

# ---------------- 2+7. 结构钉：extract 接线 / on_started 拆分 ----------------
eng = open(os.path.join(_here, "engine.py"), encoding="utf-8").read()
check("extract_bytes wired with stop_event",
      "self.extract_bytes(selected_src, expect=expect,\n                                                     stop_event=stop_event)" in eng)
check("on_started split from on_acquired",
      "on_started=None, on_acquired=None" in src.replace("on_started=None, on_acquired=None, on_account", "on_started=None, on_acquired=None")
      or ("on_started" in src and src.count("_generation_callback(on_acquired)") == 1))
check("video worker attempts only in on_acquired",
      "def _on_started():" in src and "attempts[\"n\"] += 1" in src)

# ---------------- 3. store：写序 + fsync + 损坏留证 ----------------
st = open(os.path.join(_here, "store.py"), encoding="utf-8").read()
check("write-order: archive before tasks in prune",
      st.index("self._write_archive_index_locked()\n        _write(self.cfg.tasks_file, self.tasks)\n        return dropped + len(drop)")
      < st.index("return dropped + len(drop)") + 20)
check("fsync in _write", "os.fsync(f.fileno())" in st)
check("corrupt file renamed .corrupt", '".corrupt"' in st)

cfg = store_mod.Config if hasattr(store_mod, "Config") else None
from config import Config as _C  # noqa: E402
c = _C(); c.base_dir = os.path.join(TMP, "store_t"); c.ensure_dirs()
s = store_mod.Store(c)
# 写序行为级：记录两个文件的写入顺序
order = []
orig_write = store_mod._write
def spy_write(path, obj):
    order.append(os.path.basename(path))
    return orig_write(path, obj)
store_mod._write = spy_write
try:
    t1 = s.create_task("video", "sanitized")
    s.update_task(t1["id"], video_request_key="kx", video_request_hash="hx",
                  status="completed", result={"filename": "x.mp4"})
    s.tasks[t1["id"]]["created_at"] = 0
    s.tasks[t1["id"]]["updated_at"] = 0
    with store_mod._LOCK:
        s._prune_tasks_locked()
finally:
    store_mod._write = orig_write
tail = [f for f in order if f in ("tasks.json", "archive_index.json")]
check("archive_index flushed before tasks.json on prune",
      "archive_index.json" in tail[:2] and tail.index("archive_index.json") < len(tail) - 1
      if "tasks.json" in tail else False, tail[-4:])
check("expired task archived + pruned", t1["id"] in s.archive_index
      and t1["id"] not in s.tasks)

# 损坏留证
bad_file = os.path.join(c.data_dir, "tasks.json")
with open(bad_file, "w") as f:
    f.write("{broken json")
loaded = store_mod._read(bad_file, {})
check("corrupt tasks.json -> .corrupt + default",
      loaded == {} and os.path.exists(bad_file + ".corrupt"))

# ---------------- 4. IdempotencyIndex 双域 ----------------
ix = idem_mod.IdempotencyIndex(os.path.join(TMP, "ix1.json"), ttl=3600)
ix.register("k1", "task_a", "h1")
ix.register("k2", "task_b", "h2")
check("register/lookup roundtrip",
      ix.lookup("k1")["task_id"] == "task_a")
check("pop with wrong task_id refuses",
      ix.pop("k1", task_id="task_other") is False
      and ix.lookup("k1") is not None)
check("pop with right task_id clears",
      ix.pop("k1", task_id="task_a") is True and ix.lookup("k1") is None)
# TTL 过期：短 TTL 实例
ix2 = idem_mod.IdempotencyIndex(os.path.join(TMP, "ix2.json"), ttl=1)
ix2.register("kz", "task_z", "hz")
time.sleep(1.1)
check("expired lookup returns None + lazy purge",
      ix2.lookup("kz") is None)
disk = json.load(open(os.path.join(TMP, "ix2.json")))
check("flush purges expired from disk", "kz" not in disk)
# 落盘往返
ix3 = idem_mod.IdempotencyIndex(os.path.join(TMP, "ix3.json"))
ix3.register("kk", "task_k", "hk")
ix4 = idem_mod.IdempotencyIndex(os.path.join(TMP, "ix3.json"))
ix4.load()
check("survives restart via load()", ix4.lookup("kk")["task_id"] == "task_k")
# app 双域接线
check("app has images domain", isinstance(app.IDEMPOTENCY_IMAGES, idem_mod.IdempotencyIndex))

# ---------------- 5. archive_index 上限淘汰 ----------------
c2 = _C(); c2.base_dir = os.path.join(TMP, "store_t2"); c2.ensure_dirs()
c2.archive_index_max = 5
s2 = store_mod.Store(c2)
with store_mod._LOCK:
    for i in range(8):
        s2._archive_snapshot_locked("task_%02d" % i, {
            "kind": "video", "status": "completed", "created_at": i,
            "video_request_key": "k%d" % i, "result": {"filename": "f%d.mp4" % i},
            "archived_at": i})
check("archive_index capped at max", len(s2.archive_index) <= 5, len(s2.archive_index))
check("oldest archived_at evicted first", "task_00" not in s2.archive_index
      and "task_07" in s2.archive_index)

# ---------------- 6. delete_task only_terminal + clear 单快照 ----------------
t2 = s.create_task("video", "sanitized")
s.update_task(t2["id"], status="processing")
check("only_terminal refuses live task",
      s.delete_task(t2["id"], only_terminal=True) is False
      and t2["id"] in s.tasks)
s.update_task(t2["id"], status="completed")
check("only_terminal allows terminal task",
      s.delete_task(t2["id"], only_terminal=True) is True)

# ---------------- 8. CORS 接线 / key 生命周期 ----------------
check("CORS uses configured origins fallback *",
      "allow_origins=(_origins or [\"*\"])" in src)
check("key log masked to prefix", "new_key[:8]" in src
      and "已自动生成初始密钥: %s" not in src)
check("_persist_env writes runtime.env + chmod",
      "runtime.env" in src and "0o600" in src)
check("config loads runtime.env first + empty-env passes through",
      "runtime.env" in open(os.path.join(_here, "config.py"), encoding="utf-8").read()
      and "not os.environ.get(k)" in open(os.path.join(_here, "config.py"), encoding="utf-8").read())

# _persist_env 行为级
os.environ.pop("MUSE2API_TESTP", None)
app._persist_env("MUSE2API_TESTP", "v1")
rt = os.path.join(TMP, "data", "runtime.env")
# Windows 的 os.chmod 只支持只读位——0600 断言仅 POSIX 有效（生产是 Linux 容器）
_mode_ok = (oct(os.stat(rt).st_mode & 0o777) == "0o600" if os.name != "nt" else True)
check("runtime.env written (chmod 0600 on POSIX)",
      os.path.isfile(rt) and "MUSE2API_TESTP=v1" in open(rt).read() and _mode_ok,
      oct(os.stat(rt).st_mode & 0o777))

# ---------------- 10. GET /v1/videos/{id} 白名单 ----------------
CFG = app.CFG
tv = app.store.create_task("video", "SECRET-PROMPT-CONTENT")
app.store.update_task(tv["id"], api_prompt="SECRET-PROMPT-CONTENT",
                      status="completed", progress=100,
                      result={"filename": "w.mp4", "bytes": 1, "kind": "video",
                              "url": "/v1/media/w.mp4"})
app.CFG.api_key = "testkey123"
try:
    r = client.get("/v1/videos/%s" % tv["id"],
                   headers={"Authorization": "Bearer testkey123"})
    d = r.json()
    check("video GET 200", r.status_code == 200)
    check("prompt NOT leaked", "prompt" not in d and "api_prompt" not in d
          and "SECRET" not in json.dumps(d))
    check("contract fields kept", d.get("status") == "completed"
          and d.get("progress") == 100
          and (d.get("result") or {}).get("filename") == "w.mp4"
          and d.get("url"))
finally:
    app.CFG.api_key = ""

# ---------------- 11. media 负缓存 ----------------
check("media miss negative cache present",
      "_MEDIA_MISS" in src and "delivery fetch amplification" not in src)
check("media Cache-Control header", '"Cache-Control": "private, max-age=3600"' in src)
# 行为级：miss 一次后短窗口内第二次直接 404（fetch_remote 不再被调用）
class _NoFetch:
    def __init__(self): self.calls = 0
    def fetch_remote(self, name): self.calls += 1; return None

orig_mb = app.MEDIA_BACKUPER
orig_enabled = app.mediabak.enabled
probe = _NoFetch()
app.MEDIA_BACKUPER = probe
app.mediabak.enabled = lambda: True
try:
    nm = "no-such-file-%s.mp4" % hashlib.md5(os.urandom(4)).hexdigest()
    with app._MEDIA_MISS_LOCK:
        app._MEDIA_MISS.pop(nm, None)
    r1 = client.get("/v1/media/%s" % nm)
    r2 = client.get("/v1/media/%s" % nm)
    check("first miss goes remote (404)", r1.status_code == 404 and probe.calls == 1)
    check("second miss served from negative cache", r2.status_code == 404 and probe.calls == 1)
finally:
    app.MEDIA_BACKUPER = orig_mb
    app.mediabak.enabled = orig_enabled

# ---------------- 13. queue_timeout / QueuePolicy 容错 ----------------
check("queue_timeout forbids 0", "ge=1" in open(os.path.join(_here, "generation_timing.py"), encoding="utf-8").read())
bad_q = os.path.join(TMP, "bad-queue.json")
with open(bad_q, "w") as f:
    f.write("{not json")
qp = gt_mod.QueuePolicy(bad_q)
check("broken queue json -> default + .bad rename",
      qp.settings()["queue_timeout"] == 3600
      and os.path.exists(bad_q + ".bad"))

# ---------------- 12. 心跳占位 + 14. 部署面 ----------------
check("heartbeat allows managed placeholder",
      "managed_placeholder(_px)" in src)
compose = open(os.path.join(_here, "docker-compose.yml"), encoding="utf-8").read()
check("compose tasks_max 2000 (P1-4 drift fix)", "MUSE2API_TASKS_MAX:-2000" in compose)
check("compose keep_days explicit", "MUSE2API_TASKS_KEEP_DAYS" in compose)
check("compose healthcheck", "healthcheck:" in compose and "curl" in compose)
check("compose mem limit", "mem_limit: 2500m" in compose)
dockerfile = open(os.path.join(_here, "Dockerfile"), encoding="utf-8").read()
check("Dockerfile MANAGED_DEPLOYMENT=1", "MUSE2API_MANAGED_DEPLOYMENT=1" in dockerfile)
check("Dockerfile HEALTHCHECK", "HEALTHCHECK" in dockerfile)
check("Dockerfile COPY idempotency.py", "idempotency.py" in dockerfile)
req = open(os.path.join(_here, "requirements.txt"), encoding="utf-8").read()
check("requirements pinned upper bounds", "<1" in req and "<3" in req)
check("requirements-dev exists",
      os.path.isfile(os.path.join(_here, "requirements-dev.txt")))

# ---------------- 15. 扩展 / admin 结构 ----------------
pop = open(os.path.join(_here, "extension", "popup.js"), encoding="utf-8").read()
check("popup log uses textContent (server data never innerHTML)",
      "el.textContent = text" in pop
      and "el.innerHTML" not in pop.split("function log(")[1].split("}")[0])
mf = json.load(open(os.path.join(_here, "extension", "manifest.json")))
check("manifest host_permissions narrowed",
      "http://*/*" not in mf.get("host_permissions", [])
      and "http://*/*" in mf.get("optional_host_permissions", []))
adm = open(os.path.join(_here, "admin.html"), encoding="utf-8").read()
check("admin ?key= cleared from URL", "history.replaceState" in adm)
check("admin onclick interpolation removed",
      "onclick=\"renameAcc('${a.id}','${esc(a.label)}')\"" not in adm
      and "data-act=\"rename\"" in adm)
check("admin event delegation bound", "bindPoolActions" in adm)

print("\nPASSED %d  FAILED %d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
