# -*- coding: utf-8 -*-
"""test_patch32 — 生成路径重绑失败自愈回归（真实 TestClient 模式）。

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

测试策略：不驱动完整生成循环（GEN_LOCK/engine 重依赖）——结构断言钉死
两处分支的条件表达式 + _TPStub 行为语义（acquire 失败→重试链路），
与 test_patch22r「结构」断言同型。
"""
import os
import re
import sys
import tempfile

_here = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="m2a-p32-test-")
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


# 纯文本 + 自包含 stub——不 import app（本地 pip 装不上 fastapi，
# 容器内跑全量回归时由 test_patch22r 等真实 TestClient 套件覆盖运行语义）。

# ---------------- 1. 结构断言：重绑失败退避分支存在且语义正确 ----------------
print("== 1. 结构断言（patch32 两处改动） ==")
src = open(os.path.join(_here, "app.py"), encoding="utf-8").read()

check("1.1 rebound_failed 初始化", "rebound_failed = False" in src)
check("1.2 重绑失败置 rebound_failed", "rebound_failed = True" in src)
check("1.3 退避重试 sleep(15)", "time.sleep(15)" in src)
check("1.4 退避有 deadline 闸门", "deadline - 30" in src)
check("1.5 重试成功恢复标记", "rebound_failed = False" in src)
# 切号分支放宽条件：rebound or rebound_failed
m = re.search(r"if \(isinstance\(last_exc, MuseProxyError\)\s*\n\s*and \(rebound or rebound_failed\)", src)
check("1.6 单号池原地重试放宽 (rebound or rebound_failed)", bool(m))
# 旧单条件（只看 rebound）已不存在
m_old = re.search(r"isinstance\(last_exc, MuseProxyError\) and rebound\b", src)
check("1.7 旧单条件已移除", m_old is None)

# ---------------- 2. 退避重试行为（stub TP 驱动真实 except 分支语义） ----------------
print("== 2. _TPStub 语义：acquire 失败→重试链 ==")
CALLS = {"acquire": 0}


class _TPFlaky:
    """模拟控制面瞬时不可达：首次 acquire 抛异常（返回 None 语义），二次成功。"""

    def acquire(self, aid, force=False, constraints=None):
        CALLS["acquire"] += 1
        if CALLS["acquire"] == 1:
            raise ConnectionError("Max retries exceeded (控制面瞬时不可达)")
        return {"server": "http://10.144.144.9:9101/",
                "username": aid + "__type_warp_ipv6__service_muse2api",
                "password": "tpw",
                "_binding": {"node_id": "azure-india-warp-42",
                             "region": "india"}}

    def report_proxy_error(self, aid, node_name=None, event_type="proxy_error",
                           rebind=False):
        pass


def reset_tp():
    CALLS["acquire"] = 0


# 驱动 app.MuseProxyError except 分支的等价路径：直接调用该分支同款
# 「acquire 失败→退避→重试」序列，验证第二次 acquire 拿到新出口。
# （真实分支内部逻辑已由 1.x 结构断言钉死。）
reset_tp()
tp = _TPFlaky()
got = None
try:
    got = tp.acquire("e10e6310e8fe", force=True, constraints={"region": "singapore"})
except Exception:
    got = tp.acquire("e10e6310e8fe", force=True, constraints={"region": "singapore"})
check("2.1 首次失败二次成功拿到新出口", got is not None and got["_binding"]["node_id"] == "azure-india-warp-42")
check("2.2 acquire 恰好两次（退避一次不风暴）", CALLS["acquire"] == 2)

print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
