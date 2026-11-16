"""TetherPool (IP-Pool) egress integration — 二开模块.

接线模式对齐 opencode2api v1.6.0 先例（Mode B 深度集成）：
- 启动/导号时经控制面 REST `/v1/allocation/acquire` 按 account 绑定出口；
- 拿到的网关代理 URL 写回账号 proxy（server/username/password 三元组），
  复用 muse2api 原生账号级代理链路（Playwright HTTPProxyRelay + requests）；
- 生成链路 MuseProxyError（代理连不上/407）→ 上报 battery 事件主动重绑；
- 控制面不可达 → 保持现有代理继续跑（静默兜底），心跳自愈重试。

配置（.env，全部可选；MUSE2API_TETHERPOOL 不开则本模块零介入）：
    MUSE2API_TETHERPOOL=1
    MUSE2API_TETHERPOOL_URL=<控制面地址>
    MUSE2API_TETHERPOOL_GATEWAY=<网关地址>   # 容器可达的网关地址
    MUSE2API_TETHERPOOL_PASSWORD=tetherpool
    MUSE2API_TETHERPOOL_TIER=standard
    MUSE2API_TETHERPOOL_POLICY=standard_first
"""
from __future__ import annotations

import logging
import threading
import time

import requests

log = logging.getLogger("muse2api.tetherpool")

# 管理面占位值：proxy server 填它即表示「该账号出口由 TetherPool 托管」。
MANAGED_SCHEME = "tetherpool://managed"

_REBIND_MIN_INTERVAL = 120  # 同一账号两次主动重绑的最小间隔（秒），防风暴
MAX_ACCOUNTS_PER_NODE = 5   # 铁律：同一出口节点上 muse2api 账号数上限


def _cfg() -> dict:
    from config import CFG
    return {
        "enabled": getattr(CFG, "tetherpool_enabled", False),
        "url": getattr(CFG, "tetherpool_url", ""),
        "gateway": getattr(CFG, "tetherpool_gateway", ""),
        "password": getattr(CFG, "tetherpool_password", "tetherpool"),
        "tier": getattr(CFG, "tetherpool_tier", "standard"),
        "policy": getattr(CFG, "tetherpool_policy", "standard_first"),
    }


def enabled() -> bool:
    c = _cfg()
    return bool(c["enabled"] and c["url"] and c["gateway"])


def gateway_base() -> str:
    return _cfg()["gateway"].rstrip("/")


def is_managed(proxy: dict | None) -> bool:
    """账号 proxy 是否处于 TetherPool 托管（绑定过真实网关）。"""
    return bool(proxy) and proxy.get("server") == gateway_base() + "/"


def managed_placeholder(proxy: dict | None) -> bool:
    """账号 proxy 是否为托管占位值（尚未 acquire）。"""
    return bool(proxy) and proxy.get("server") == MANAGED_SCHEME


class TetherPoolClient:
    """控制面 REST 客户端 + 进程内绑定缓存。线程安全。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._bindings: dict[str, dict] = {}   # aid -> acquire 响应
        self._last_rebind: dict[str, float] = {}
        self._last_ctl_fail = 0.0

    # ---- 基础请求 ----
    def _ctl(self, method: str, path: str, timeout: float = 12.0, **kw) -> dict:
        c = _cfg()
        r = requests.request(method, c["url"] + path, timeout=timeout, **kw)
        r.raise_for_status()
        return r.json()

    # ---- 绑定 ----
    def acquire(self, aid: str, force: bool = False,
                only_if_missing: bool = False,
                constraints: dict | None = None) -> dict | None:
        """为账号绑定出口；成功返回网关 proxy dict 并写缓存。失败返回 None（调用方兜底）。

        only_if_missing=True：进程内已有缓存绑定即直接返回（心跳自愈语义，
        避免每 300s 无谓 churn 出口——已绑定的账号保持原节点粘性）。
        constraints：账号级出口约束（region/region_strict/egress_preference），
        透传控制面 acquire——ip-pool 侧会把约束落库 bound_* 列，重绑也守约束。
        返回的 proxy dict 附带 _binding 档案（node/exit_ip/bound_at）。
        """
        if not enabled():
            return None
        with self._lock:
            cur = self._bindings.get(aid)
            if cur:
                if only_if_missing:
                    return cur["proxy"]
                if not force and time.monotonic() - self._last_rebind.get(aid, 0) < _REBIND_MIN_INTERVAL:
                    return cur["proxy"]
        try:
            c = _cfg()
            data = None
            # patch17：同出口 ≤5 号守卫——分配出口上 muse2api 账号已满则
            # 换绑重试；最多 2 次重试，仍超限接受共享（池小时可用性优先）。
            # patch17e（final review P1 根修）：计数改读本服务 allocation
            # 列表并按出口 exit_ip 聚合。旧实现读 /v1/allocation/status 的
            # node_stats——该聚合不含 service 过滤（全服务合计，实测某节点
            # 446），守卫每次必误判满载 → 每次绑定都 3 次 release+rebalance
            # churn 且丢粘性。铁律口径是同出口 IP ≤5：同 IP 多节点监听按同
            # 一出口计；返回计数含自身 → 接受条件 cnt ≤ 上限。
            for attempt in (0, 1, 2):
                params = {"service": "muse2api", "account_id": aid,
                          "tier": c["tier"], "tier_policy": c["policy"]}
                if constraints:
                    params.update({k: v for k, v in constraints.items() if v})
                if force or attempt > 0:
                    params["force_rebalance"] = "true"
                data = self._ctl("POST", "/v1/allocation/acquire", params=params, timeout=20.0)
                exit_ip = data.get("exit_ip") or ""
                cnt = self.exit_account_count(exit_ip) if exit_ip else 0
                if cnt <= MAX_ACCOUNTS_PER_NODE:
                    break
                log.warning("【TetherPool】出口 %s 已有 %d 个 muse2api 账号（上限 %d），第 %d 次换绑",
                            exit_ip or "?", cnt, MAX_ACCOUNTS_PER_NODE, attempt + 1)
                try:
                    self._ctl("POST", "/v1/allocation/release",
                              params={"service": "muse2api", "account_id": aid}, timeout=10.0)
                except Exception:  # noqa: BLE001
                    pass
            else:
                log.warning("【TetherPool】账号 %s 三次分配均落在满载出口，接受共享出口 %s",
                            aid, data.get("exit_ip") or data.get("node_name"))
            proxy = _gateway_proxy(aid, data)
            with self._lock:
                self._bindings[aid] = {"data": data, "proxy": proxy}
                self._last_rebind[aid] = time.monotonic()
                self._last_ctl_fail = 0.0
            log.info("【TetherPool】账号 %s 绑定出口 %s (%s, %s)", aid,
                     data.get("node_name"), data.get("region"), data.get("exit_ip"))
            # patch17：附绑定档案（node/exit_ip），app 层写账号 binding
            proxy["_binding"] = {
                "node_id": data.get("node_name") or data.get("node_id"),
                "exit_ip": data.get("exit_ip"),
                "region": data.get("region"),
                "egress_type": data.get("egress_type"),
                "bound_at": data.get("allocated_at") or int(time.time()),
            }
            return proxy
        except Exception as exc:  # noqa: BLE001 — 控制面故障必须静默兜底
            with self._lock:
                self._last_ctl_fail = time.monotonic()
            log.warning("【TetherPool】acquire 失败（保持现有出口继续跑）: %s", str(exc)[:160])
            return None

    def exit_account_count(self, exit_ip: str) -> int:
        """muse2api 服务在该出口 IP 上的账号数（含刚 acquire 的自身；查询失败按 0 放行）。

        patch17e 根修：旧实现读 /v1/allocation/status 的 node_stats——该聚合
        不带 service 过滤（全服务合计 400+），守卫永远误判。改用本服务
        allocation 列表按 exit_ip 计数；exit_ip 为空（控制面未回）按 0 放行。
        """
        if not exit_ip:
            return 0
        try:
            rows = self._ctl("GET", "/v1/allocation/accounts",
                             params={"service": "muse2api"}, timeout=8.0)
            return sum(1 for r in rows or []
                       if (r.get("exit_ip") or "") == exit_ip)
        except Exception:  # noqa: BLE001 — 查询失败不阻塞绑定
            pass
        return 0

    def release(self, aid: str) -> bool:
        """删除账号时释放 ip-pool 绑定（防止 allocation 泄漏）；失败仅告警。"""
        if not enabled():
            return False
        try:
            self._ctl("POST", "/v1/allocation/release",
                      params={"service": "muse2api", "account_id": aid}, timeout=10.0)
        except Exception as exc:  # noqa: BLE001
            log.warning("【TetherPool】释放账号 %s 绑定失败: %s", aid, str(exc)[:120])
            return False
        finally:
            with self._lock:
                self._bindings.pop(aid, None)
        return True

    def report_proxy_error(self, aid: str, node_name: str | None = None,
                           event_type: str = "proxy_error") -> dict | None:
        """生成链路代理异常 → battery 事件 + 强制重绑（opencode2api 先例）。

        event_type 是自由字符串（控制面不做枚举校验）；默认 proxy_error
        如实描述"账号代理异常"，不再冒充 429 限流污染节点质量统计。
        """
        if not enabled():
            return None
        try:
            self._ctl("POST", "/v1/battery/event", json={
                "account_id": aid, "event_type": event_type,
                "node_name": node_name, "target_service": "muse2api",
                "force_rebind": True}, timeout=10.0)
        except Exception as exc:  # noqa: BLE001
            log.warning("【TetherPool】battery 事件上报失败: %s", str(exc)[:120])
        return self.acquire(aid, force=True)

    def cached_binding(self, aid: str) -> dict | None:
        """进程内绑定缓存查询（心跳用：None = 该账号当前无绑定，需要补绑）。"""
        with self._lock:
            v = self._bindings.get(aid)
            return dict(v["proxy"]) if v else None

    def status(self, aid: str | None = None) -> dict:
        c = _cfg()
        out = {"enabled": enabled(), "control_plane": c["url"], "gateway": c["gateway"],
               "service": "muse2api", "bindings": {}}
        with self._lock:
            out["control_plane_alive"] = (time.monotonic() - self._last_ctl_fail) > 60 \
                if self._last_ctl_fail else True
            for k, v in self._bindings.items():
                if aid and k != aid:
                    continue
                out["bindings"][k] = {
                    "node_name": v["data"].get("node_name"),
                    "exit_ip": v["data"].get("exit_ip"),
                    "region": v["data"].get("region"),
                    "egress_type": v["data"].get("egress_type"),
                    "allocated_at": v["data"].get("allocated_at"),
                }
        if aid and enabled():
            try:
                out["control_plane_view"] = self._ctl(
                    "GET", f"/v1/allocation/status/account",
                    params={"service": "muse2api", "account": aid}, timeout=8.0)
            except Exception:
                out["control_plane_view"] = None
        return out


def _gateway_proxy(aid: str, data: dict) -> dict:
    """acquire 响应 → muse2api 账号 proxy 三元组（走参数化网关，Mode A 语义）。"""
    user = aid
    meta = data.get("gateway_proxy_url", "")
    if "@" in meta:
        encoded = meta.split("//", 1)[-1].rsplit("@", 1)[0]
        user = encoded.rsplit(":", 1)[0] or aid
    return {"server": gateway_base() + "/", "username": user,
            "password": _cfg()["password"]}


TP = TetherPoolClient()
