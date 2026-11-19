# -*- coding: utf-8 -*-
"""test_patch23 — 一键导号机制回归（真实 TestClient 模式，对齐 test_patch22r 样板）。

覆盖：
  1. 一次性导入令牌生命周期：创建 → 带令牌导入成功 → 令牌焚毁（二次 401）→ 过期 401
  2. 令牌权限面：proxy/region 等管理字段 403；非导入端点用令牌 401
  3. 单活跃令牌：新建即撤销旧令牌
  4. hatch_sess 去重：同会话 409 不建新号；不同会话正常；store 层 ValueError 语义
  5. 旧路径回归：完整管理 KEY 导入仍可用；命令组装含 --token 不含完整 KEY
  6. 结构断言：admin.html UI 接线 + get_muse_cookie.py --token 支持
"""
import importlib
import json
import os
import sys
import tempfile
import time

_here = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="m2a-p23-test-")
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
import app  # noqa: E402
import store as store_mod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app.app)
ADMIN = {"Authorization": "Bearer testkey123"}
app.CFG.api_key = "testkey123"


def cleanup_accounts():
    with store_mod._LOCK:
        app.store.accounts = []


def get_store():
    return app.store


# ---------------- 1. 令牌生命周期 ----------------
print("== 1. 一次性导入令牌生命周期 ==")
cleanup_accounts()

r = client.post("/admin/import-token", headers=ADMIN, json={"label": "acc-23"})
check("import-token 200", r.status_code == 200, r.text[:120])
tok = r.json().get("token", "")
check("token 前缀 m2a_imp_", tok.startswith("m2a_imp_"), tok[:16])
check("expires_in=900", r.json().get("expires_in") == 900)
cmd = r.json().get("command", "")
check("command 含 --token", "--token " + tok in cmd)
check("command 含 --label", "--label acc-23" in cmd)
check("command 不含管理 KEY", "testkey123" not in cmd)
check("token 不在其它字段泄漏", tok not in json.dumps(
    {k: v for k, v in r.json().items() if k != "token" and k != "command"}))

# 带令牌导入（无 hatch_sess → 400 但令牌已焚毁——导入动作已消耗）
TOK = {"Authorization": f"Bearer {tok}"}
r2 = client.post("/admin/accounts", headers=TOK,
                 json={"label": "acc-23", "cookies": {"hatch_sess": "sessA"}})
check("令牌导入成功 200", r2.status_code == 200, r2.text[:120])
check("账号入库", len(get_store().accounts) == 1)
check("label 生效", get_store().accounts[0]["label"] == "acc-23")

# 令牌已焚毁：同令牌重放 401
r3 = client.post("/admin/accounts", headers=TOK,
                 json={"label": "x", "cookies": {"hatch_sess": "sessB"}})
check("令牌单次有效（重放 401）", r3.status_code == 401, str(r3.status_code))

# 无效令牌 401（不区分过期/不存在）
r4 = client.post("/admin/accounts",
                 headers={"Authorization": "Bearer m2a_imp_" + "0" * 32},
                 json={"label": "x", "cookies": {"hatch_sess": "sessC"}})
check("无效令牌 401", r4.status_code == 401, str(r4.status_code))

# 过期令牌 401
r = client.post("/admin/import-token", headers=ADMIN, json={})
tok5 = r.json()["token"]
app._IMPORT_TOKENS[app._import_token_hash(tok5)]["expires"] = time.monotonic() - 1
r5 = client.post("/admin/accounts", headers={"Authorization": f"Bearer {tok5}"},
                 json={"label": "x", "cookies": {"hatch_sess": "sessD"}})
check("过期令牌 401", r5.status_code == 401, str(r5.status_code))
check("过期令牌残留已清", app._import_token_hash(tok5) not in app._IMPORT_TOKENS)

# ---------------- 2. 令牌权限面 ----------------
print("== 2. 令牌权限面 ==")
r = client.post("/admin/import-token", headers=ADMIN, json={"label": "acc-x"})
tok6 = r.json()["token"]
TOK6 = {"Authorization": f"Bearer {tok6}"}
r6 = client.post("/admin/accounts", headers=TOK6,
                 json={"label": "acc-x", "cookies": {"hatch_sess": "sessE"},
                       "proxy": {"server": "http://1.2.3.4:8080"}})
check("令牌带 proxy 403", r6.status_code == 403, str(r6.status_code))
check("403 后令牌未焚毁（未到导入动作）", True)  # 权限拒绝不消耗令牌
r6b = client.post("/admin/accounts", headers=TOK6,
                  json={"label": "acc-x", "cookies": {"hatch_sess": "sessF"}})
check("403 后令牌仍可用导入", r6b.status_code == 200, str(r6b.status_code))

r = client.post("/admin/import-token", headers=ADMIN, json={})
tok7 = r.json()["token"]
r7 = client.get("/admin/accounts", headers={"Authorization": f"Bearer {tok7}"})
check("令牌访问其它管理端点 401", r7.status_code == 401, str(r7.status_code))

# ---------------- 3. 单活跃令牌 ----------------
print("== 3. 单活跃令牌 ==")
before = len(app._IMPORT_TOKENS)
r = client.post("/admin/import-token", headers=ADMIN, json={})
tok8 = r.json()["token"]
r = client.post("/admin/import-token", headers=ADMIN, json={})
tok9 = r.json()["token"]
check("新建令牌撤销旧令牌", len(app._IMPORT_TOKENS) == 1 and before >= 1,
      f"len={len(app._IMPORT_TOKENS)}")
r8 = client.post("/admin/accounts", headers={"Authorization": f"Bearer {tok8}"},
                 json={"label": "x", "cookies": {"hatch_sess": "sessG"}})
check("旧令牌已撤销 401", r8.status_code == 401, str(r8.status_code))

# ---------------- 4. hatch_sess 去重 ----------------
print("== 4. hatch_sess 去重 ==")
cleanup_accounts()
r = client.post("/admin/accounts", headers=ADMIN,
                json={"label": "first", "cookies": {"hatch_sess": "dup-sess"}})
check("首次导入 200", r.status_code == 200)
r = client.post("/admin/accounts", headers=ADMIN,
                json={"label": "second", "cookies": {"hatch_sess": "dup-sess"}})
check("同 hatch_sess 409", r.status_code == 409, str(r.status_code))
check("409 提示含已有账号 label", "first" in r.json().get("detail", ""), r.text[:150])
check("未建新号", len(get_store().accounts) == 1)

# 不同会话正常
r = client.post("/admin/accounts", headers=ADMIN,
                json={"label": "other", "cookies": {"hatch_sess": "other-sess"}})
check("不同 hatch_sess 正常入库", r.status_code == 200 and len(get_store().accounts) == 2)

# 无 hatch_sess 的导入不去重（hatch_sess 是唯一核心凭证）
r = client.post("/admin/accounts", headers=ADMIN,
                json={"label": "no-sess-1", "cookies": {"foo": "1"}})
check("无核心 cookie 时 warning 且可导入", r.status_code == 200
      and r.json().get("essential_missing"), r.text[:120])

# store 层 ValueError 语义
try:
    get_store().add_account({"hatch_sess": "dup-sess"}, "store-level")
    check("store 层 duplicate_session 异常", False)
except ValueError as exc:
    check("store 层 duplicate_session 异常",
          str(exc).startswith("duplicate_session:"), str(exc)[:60])

# ---------------- 5. 旧路径回归 ----------------
print("== 5. 旧路径回归（完整 KEY） ==")
cleanup_accounts()
r = client.post("/admin/accounts", headers=ADMIN,
                json={"label": "keypath", "cookies": {"hatch_sess": "kp-sess"}})
check("管理 KEY 导入仍可用", r.status_code == 200, str(r.status_code))
r = client.post("/admin/accounts", json={"label": "x", "cookies": {"hatch_sess": "y"}})
check("无鉴权 401", r.status_code == 401, str(r.status_code))
r = client.post("/admin/accounts", headers={"Authorization": "Bearer wrong"},
                json={"label": "x", "cookies": {"hatch_sess": "y"}})
check("错 KEY 401", r.status_code == 401, str(r.status_code))

# ---------------- 6. 结构断言（UI/脚本接线） ----------------
print("== 6. 结构断言 ==")
admin_src = open(os.path.join(_here, "admin.html"), encoding="utf-8").read()
helper_src = open(os.path.join(_here, "tools", "get_muse_cookie.py"),
                  encoding="utf-8").read()
check("admin UI 有 genImportToken", "async function genImportToken" in admin_src)
check("admin UI 调 import-token", "'/admin/import-token'" in admin_src
      or '"/admin/import-token"' in admin_src)
check("admin UI 令牌命令框", 'id="tokCmdBox"' in admin_src)
check("admin UI i18n EN 词条", '"gen-token-btn"' in admin_src)
check("UI 不再拼 --key 命令", "--key ${realKey" not in admin_src)
check("helper 支持 --token", '"--token"' in helper_src)
check("helper 令牌优先", "args.token or args.key" in helper_src)
check("helper 401 提示", "一次性令牌导入成功即失效" in helper_src)
check("helper 409 提示", "已经在账号池里" in helper_src)
check("app 有 _import_token_verify", "def _import_token_verify" in
      open(os.path.join(_here, "app.py"), encoding="utf-8").read())

# ---------------- 汇总 ----------------
print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
