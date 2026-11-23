# -*- coding: utf-8 -*-
"""test_patch29 — CN 出口封禁三层防御回归（stub 驱动）。

背景（P1 用户裁定）：muse.ai 对国内出口风控极严，CN 出口会被封号；宿主
tencent-lh 本身就是 CN 出口（上海腾讯云），acquire 失败静默直连兜底也是
封号级事故。池内现存 2 个 CN 节点（home-win/laptop-win native v4，tier=
premium，上海移动 183.193.163.187）——premium_only 链路（导号/登录）撞上
风险最高。池侧节点挂 ~400 其它服务账号，不可全局 disable → 服务侧自治。

覆盖：
  层1 acquire 必带 avoid_cidr（constraints 无法覆盖封禁；CN 响应也带）
  层2 acquire 响应 region=cn 拒绝 → release + force_rebalance 重试；
      3 轮全 CN → 返回 None 不落缓存（fail-closed）
  层3 _bind_account_proxy 遇 CN 绑定档案 → MuseProxyError 拒绝出海
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
import tetherpool  # noqa: E402
from tetherpool import TetherPoolClient, is_cn_region  # noqa: E402
from engine import MuseProxyError  # noqa: E402


class _StubCtl:
    """acquire 响应脚本：/acquire 每次消耗队首元素（Exception 则 raise）；
    release 等其余端点不消耗脚本、直接回空 dict（否则 release 会把下一个
    acquire 响应吞掉——首版 stub 的缺陷）。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []          # [(method, path, params)]

    def __call__(self, method, path, timeout=12.0, **kw):
        params = kw.get("params") or {}
        self.calls.append((method, path, params))
        if path == "/v1/allocation/acquire":
            action = self.script.pop(0)
            if isinstance(action, Exception):
                raise action
            return action
        return {}


def _client_with(ctl):
    c = TetherPoolClient.__new__(TetherPoolClient)
    c._lock = __import__("threading").Lock()
    c._bindings = {}
    c._last_rebind = {}
    c._last_ctl_fail = 0.0
    c._ctl = ctl
    return c


def _ok_resp(region="india", ip="20.10.30.40", node="azure-india-warp-10"):
    return {"node_name": node, "region": region, "exit_ip": ip,
            "egress_type": "warp_ipv6", "allocated_at": 1700000000}


print("== 层1: acquire 必带 avoid_cidr ==")
ctl = _StubCtl([_ok_resp()])
tp = _client_with(ctl)
tetherpool.enabled = lambda: True
tetherpool._gateway_proxy = lambda aid, data: {"server": "http://gw/", "username": aid,
                                               "password": "x"}
try:
    p = tp.acquire("a1", constraints={"region": "premium-region-x"})
    check("1.1 成功绑定", p is not None and p["_binding"]["region"] == "india")
    check("1.2 avoid_cidr 注入且不被 constraints 覆盖",
          ctl.calls[0][2].get("avoid_cidr") == tetherpool.CN_AVOID_CIDRS_DEFAULT,
          ctl.calls[0][2].get("avoid_cidr"))
    check("1.3 constraints 透传保留", ctl.calls[0][2].get("region") == "premium-region-x")
finally:
    pass

print("== 层2: CN region 拒绝重试 ==")
ctl = _StubCtl([_ok_resp(region="cn", ip="183.193.163.187", node="home-win-native-v4"),
                _ok_resp(region="cn", node="laptop-win-native-v4"),
                _ok_resp()])
tp = _client_with(ctl)
try:
    p = tp.acquire("a2", force=True)
    check("2.1 第三次成功返回", p is not None and p["_binding"]["region"] == "india")
    acquires = [c for c in ctl.calls if c[1] == "/v1/allocation/acquire"]
    releases = [c for c in ctl.calls if c[1] == "/v1/allocation/release"]
    check("2.2 acquire 3 次", len(acquires) == 3, len(acquires))
    check("2.3 每次 CN 分配都被 release", len(releases) == 2, len(releases))
    check("2.4 重试带 force_rebalance", acquires[1][2].get("force_rebalance") == "true")
    check("2.5 缓存只落最终绑定（无 CN 残留）",
          tp.cached_binding("a2")["_binding"]["region"] == "india")
finally:
    pass

print("== 层2b: 3 轮全 CN → None 不落缓存 ==")
ctl = _StubCtl([_ok_resp(region="cn"), _ok_resp(region="cn"), _ok_resp(region="cn")])
tp = _client_with(ctl)
try:
    p = tp.acquire("a3", force=True)
    check("3.1 返回 None（fail-closed）", p is None, repr(p))
    check("3.2 缓存为空（不落 CN 绑定）", tp.cached_binding("a3") is None)
finally:
    pass

print("== 层2c: is_cn_region 前缀语义 ==")
check("4.1 'cn' 命中", is_cn_region("cn"))
check("4.2 'CN' 大写命中", is_cn_region("CN"))
check("4.3 'cn-north' 前缀命中", is_cn_region("cn-north"))
check("4.4 'india' 不命中", not is_cn_region("india"))
check("4.5 'conn' 不误命中", not is_cn_region("connecticut"))
check("4.6 None/空 不命中", not is_cn_region(None) and not is_cn_region(""))

print("== 层3: _bind_account_proxy 封死 CN 绑定 ==")
saved_proxy_snapshot = {}
orig_cfg_enabled = tetherpool.enabled


class _FakeEngine:
    class _Local:
        proxy_snapshot = None
    def __init__(self):
        self._local = self._Local()
    def _ensure_local(self):
        return self._local


try:
    tetherpool.enabled = lambda: True
    app.engine = _FakeEngine()

    # CN 绑定 → 拒绝出海
    acc_cn = {"id": "cn1", "proxy": {"server": tetherpool.gateway_base() + "/",
                                     "username": "cn1", "password": "x"},
              "binding": {"node_id": "home-win-native-v4", "region": "cn",
                          "exit_ip": "183.193.163.187"}}
    raised = None
    try:
        app._bind_account_proxy(acc_cn)
    except MuseProxyError as exc:
        raised = exc
    check("5.1 CN 绑定抛 MuseProxyError", raised is not None, repr(raised))
    check("5.2 未设置 proxy_snapshot（不出海）",
          app.engine._ensure_local().proxy_snapshot is None)

    # 海外绑定 → 正常 pin
    acc_ok = {"id": "in1", "proxy": {"server": tetherpool.gateway_base() + "/",
                                     "username": "in1", "password": "x"},
              "binding": {"node_id": "azure-india-warp-10", "region": "india"}}
    app.engine._ensure_local().proxy_snapshot = None
    app._bind_account_proxy(acc_ok)
    check("5.3 海外绑定正常 pin", app.engine._ensure_local().proxy_snapshot is not None)
finally:
    tetherpool.enabled = orig_cfg_enabled

print("== 层2d: constraints 尝试覆盖 avoid_cidr 无效 ==")
ctl = _StubCtl([_ok_resp()])
tp = _client_with(ctl)
try:
    tp.acquire("a4", constraints={"avoid_cidr": "1.2.3.4/32"})
    check("6.1 avoid_cidr 始终为 CN 段（constraints 不覆盖）",
          ctl.calls[0][2].get("avoid_cidr") == tetherpool.CN_AVOID_CIDRS_DEFAULT,
          ctl.calls[0][2].get("avoid_cidr"))
finally:
    pass

print("\n== 结果: %d PASS / %d FAIL ==" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
