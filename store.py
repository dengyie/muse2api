"""账号池与任务存储（JSON 落盘，原子写）。"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid

from proxy import public_proxy

_LOCK = threading.Lock()

# 登录会话存在性检查；可用性仍须经上游认证（与 engine 一致）
# hatch_vml 是可选工作区令牌，缺失不能据此拒绝会话导入
ESSENTIAL_COOKIES = ("hatch_sess",)


# ---------------- patch17：每账号稳定指纹档案 ----------------
# 目的：号池规模化时，账号之间互不相同、单账号跨重启稳定（设备身份隔离）。
# 派生是纯函数：seed 相同 → 档案相同；seed 不同 → 档案不同。
# seed 用 secrets 生成并落库在账号上，是唯一随机源。

_FP_CHROME_MAJOR_MIN, _FP_CHROME_MAJOR_MAX = 130, 135
_FP_PLATFORMS = ("Windows", "macOS", "Linux")
_FP_LOCALES = ("en-US", "en-GB", "zh-CN", "zh-TW", "ja-JP", "de-DE", "fr-FR")
_FP_TIMEZONES = ("America/New_York", "America/Chicago", "America/Los_Angeles",
                 "Europe/London", "Europe/Berlin", "Asia/Tokyo",
                 "Asia/Shanghai", "Asia/Hong_Kong")
_FP_SCREENS = ((1920, 1080), (2560, 1440), (1440, 900), (1680, 1050),
               (1366, 768), (1536, 864))
_FP_WEBGL_VENDORS = ("Google Inc. (NVIDIA)", "Google Inc. (AMD)",
                     "Google Inc. (Intel)", "Google Inc. (Apple)")
_FP_WEBGL_RENDERERS = (
    "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "ANGLE (Intel, Intel(R) UHD Graphics 770 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified Version)",
    "ANGLE (Intel, Mesa Intel(R) Iris(R) Xe Graphics (CML GT2), OpenGL 4.6)",
)


def _fp_pick(seq: str, options):
    """从 seed 确定性挑选一个选项（sha256 流式派生，无全局状态）。"""
    import hashlib
    h = hashlib.sha256(("fp:" + seq).encode()).digest()
    return options[h[0] % len(options)]


def _fp_int(seq: str, lo: int, hi: int) -> int:
    import hashlib
    h = hashlib.sha256(("int:" + seq).encode()).digest()
    return lo + (int.from_bytes(h[:4], "big") % (hi - lo + 1))


def derive_fingerprint(seed: str) -> dict:
    """seed → 稳定设备身份档案（纯函数，可单测）。"""
    import hashlib
    platform = _fp_pick("platform:" + seed, _FP_PLATFORMS)
    chrome_major = _fp_int("chrome:" + seed,
                           _FP_CHROME_MAJOR_MIN, _FP_CHROME_MAJOR_MAX)
    ua_platform = {
        "Windows": "Windows NT 10.0; Win64; x64",
        "macOS": "Macintosh; Intel Mac OS X 10_15_7",
        "Linux": "X11; Linux x86_64",
    }[platform]
    ua = (f"Mozilla/5.0 ({ua_platform}) AppleWebKit/537.36 "
          f"(KHTML, like Gecko) Chrome/{chrome_major}.0.0.0 Safari/537.36")
    locale = _fp_pick("locale:" + seed, _FP_LOCALES)
    timezone = _fp_pick("tz:" + seed, _FP_TIMEZONES)
    w, h = _fp_pick("screen:" + seed, _FP_SCREENS)
    # client hints platform 版本：Windows 固定 "15.0.0"（Win11 报法），
    # 其它平台给个稳定的低位版本号
    ua_platform_version = "15.0.0" if platform == "Windows" else (
        f"10.15.{_fp_int('pv:' + seed, 0, 7)}" if platform == "macOS" else "6.5.0")
    return {
        "seed": seed,
        "ua": ua,
        "platform": platform,
        "ua_platform_version": ua_platform_version,
        "chrome_major": chrome_major,
        "locale": locale,
        "timezone": timezone,
        "accept_language": locale,
        "screen": {"width": w, "height": h},
        "device_scale_factor": _fp_pick("dsf:" + seed, (1, 1, 1.25, 1.5)),
        "hardware_concurrency": _fp_pick("cores:" + seed, (4, 6, 8, 12, 16)),
        "device_memory": _fp_pick("mem:" + seed, (4, 8, 8, 16)),
        "webgl_vendor": _fp_pick("glv:" + seed, _FP_WEBGL_VENDORS),
        "webgl_renderer": _fp_pick("glr:" + seed, _FP_WEBGL_RENDERERS),
        # 噪声种子：canvas/audio 微扰强度派生
        "noise": hashlib.sha256(("noise:" + seed).encode()).hexdigest()[:16],
    }


def public_fingerprint(fp: dict | None) -> dict | None:
    """指纹档案对外可见摘要——绝不含 seed（敏感派生源，永不回显）。"""
    if not fp:
        return None
    return {k: fp.get(k) for k in
            ("platform", "chrome_major", "locale", "timezone", "screen",
             "device_scale_factor", "hardware_concurrency", "device_memory",
             "webgl_vendor")} if fp else None


def _is_pos(v) -> bool:
    try:
        return int(v) > 0
    except (TypeError, ValueError):
        return False


# hatch_vml 固定约 2 天，且不因使用而延长 —— 它就是决定账号寿命的那条 cookie。
# 实测对照：生成前后 synced_at 变了、也捞到新 cookie，但 4 条核心 cookie 的
# expires 一动不动。所以拿不到 hatch_vml 的 expires 时，按「导入时间 + 2 天」估算，
# 而不是退回到 hatch_native_auth_device 的 30 天（那个不决定生死，会误导用户）。
VML_TTL = 2 * 86400


def min_expiry(cookies_exp: dict | None) -> int | None:
    """核心 cookie 里最早过期的那个（通用兜底，不区分哪条决定寿命）。"""
    if not cookies_exp:
        return None
    vals = []
    for name in ESSENTIAL_COOKIES:
        v = cookies_exp.get(name)
        try:
            v = int(v)
        except (TypeError, ValueError):
            continue
        if v > 0:
            vals.append(v)
    if not vals:
        for v in cookies_exp.values():
            try:
                v = int(v)
            except (TypeError, ValueError):
                continue
            if v > 0:
                vals.append(v)
    return min(vals) if vals else None


def account_expiry(cookies_exp: dict | None,
                   base_ts: int | None = None) -> int | None:
    """账号实际失效时间。

    优先级：
      1) hatch_vml 的真实 expires（最准）；
      2) 拿不到就用 base_ts(导入/同步时刻) + VML_TTL 估算 —— 因为寿命就是 2 天；
      3) 都没有再退回核心 cookie 最早过期。
    """
    ce = cookies_exp or {}
    vml = ce.get("hatch_vml")
    if _is_pos(vml):
        return int(vml)
    if base_ts:
        return int(base_ts) + VML_TTL
    return min_expiry(ce)


def _read(path: str, default):
    if not os.path.isfile(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def _write(path: str, obj):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class Store:
    def __init__(self, cfg):
        self.cfg = cfg
        self.accounts: list[dict] = _read(cfg.accounts_file, [])
        for a in self.accounts:
            ce = a.get("cookies_exp") or {}
            if _is_pos(ce.get("hatch_vml")):
                a["expires_at"] = int(ce["hatch_vml"])
        self.tasks: dict[str, dict] = _read(cfg.tasks_file, {})

    # ---------- 账号 ----------
    def add_account(self, cookies: dict, label: str = "",
                    cookies_exp: dict | None = None) -> dict:
        with _LOCK:
            aid = uuid.uuid4().hex[:12]
            exp = {k: int(v) for k, v in (cookies_exp or {}).items()
                   if _is_pos(v)}
            now = int(time.time())
            import secrets
            acc = {"id": aid, "label": label or aid, "cookies": cookies,
                   "cookies_exp": exp, "expires_at": account_expiry(exp, now),
                   "expiry_anchor": None,
                   "enabled": True, "created_at": now,
                   "last_used": None, "last_keepalive": None, "use_count": 0,
                   "ok": None, "note": "", "synced_at": None}
            # patch17：新账号随注册生成稳定指纹档案（seed 落库，设备身份隔离）
            acc["fingerprint"] = derive_fingerprint(secrets.token_hex(16))
            acc["binding"] = None
            self.accounts.append(acc)
            _write(self.cfg.accounts_file, self.accounts)
            return acc

    @staticmethod
    def public_account(account: dict) -> dict:
        item = {k: v for k, v in account.items()
                if k not in ("cookies", "proxy", "fingerprint")}
        item["proxy"] = public_proxy(account.get("proxy"))
        item["cookie_count"] = len(account.get("cookies", {}))
        # patch17：指纹只出摘要（绝不含 seed）；binding 直接可见
        item["fingerprint"] = public_fingerprint(account.get("fingerprint"))
        item["binding"] = account.get("binding")
        return item

    def list_accounts(self) -> list[dict]:
        out = []
        for a in self.accounts:
            item = self.public_account(a)
            ce = a.get("cookies_exp") or {}
            if _is_pos(ce.get("hatch_vml")):
                item["expires_at"] = int(ce["hatch_vml"])
            item["cookie_count"] = len(a.get("cookies", {}))
            item["essential_ok"] = all(
                a.get("cookies", {}).get(n) for n in ESSENTIAL_COOKIES)
            # 有效期是 hatch_vml 的实测 expires，还是按「2 天寿命」推算的
            item["expires_estimated"] = not _is_pos(ce.get("hatch_vml"))
            out.append(item)
        return out

    def delete_account(self, aid: str) -> bool:
        with _LOCK:
            n = len(self.accounts)
            self.accounts = [a for a in self.accounts if a["id"] != aid]
            _write(self.cfg.accounts_file, self.accounts)
            return len(self.accounts) != n

    def pick_account(self, preferred_id: str | None = None, rotate: bool = True,
                     force_rotate: bool = False, exclude_id: str | None = None,
                     exclude_ids: set[str] | list[str] | None = None,
                     record_use: bool = True) -> dict | None:
        """选择可用账号：小时粒度粘性（last_used//3600）+ 周额度优先排序（patch16）；
        同账号连续复用由排序主键 last_used 小时粘性保证；exclude_ids: 需要排除
        的账号 ID 集合（例如正在被其他并发线程占用的账号）。"""
        with _LOCK:
            live = [a for a in self.accounts if a.get("enabled", True) and a.get("cookies")]
            if not live:
                return None
            blocked = set(exclude_ids or [])
            if exclude_id:
                blocked.add(exclude_id)
            if blocked:
                available = [a for a in live if a["id"] not in blocked]
                if available:
                    live = available
                elif exclude_ids is not None:
                    # 并发模式下要求避开所有繁忙账号，但当前无剩余空闲账号
                    return None
                elif len(live) > 1:
                    live = [a for a in live if a["id"] != exclude_id] or live
            if preferred_id and not rotate and not force_rotate:
                for a in live:
                    if a["id"] == preferred_id and a.get("ok") is not False:
                        if record_use:
                            a["last_used"] = time.time()
                            a["use_count"] = a.get("use_count", 0) + 1
                            _write(self.cfg.accounts_file, self.accounts)
                        return a
            healthy = [a for a in live if a.get("ok") is not False]
            candidates = healthy if healthy else live

            # patch16：周额度优先——同优先级（last_used 相近）下周额度用量低者
            # 先用；weekly_used_pct>=100（本周额度耗尽）沉底。quota 缓存缺失按
            # 0 处理（不惩罚未读数账号）。last_used 仍是主键（粘性预热优先）。
            def _quota_key(a):
                q = a.get("quota") or {}
                pct = q.get("weekly_used_pct")
                if not isinstance(pct, (int, float)):
                    return 0
                return pct if pct < 100 else 10000

            candidates.sort(key=lambda a: (
                (a.get("last_used") or 0.0) // 3600,  # 小时粒度粘性：同小时内比额度
                _quota_key(a),
                a.get("use_count") or 0,
            ))
            acc = candidates[0]
            if record_use:
                acc["last_used"] = time.time()
                acc["use_count"] = acc.get("use_count", 0) + 1
                _write(self.cfg.accounts_file, self.accounts)
            return acc

    def touch_keepalive(self, aid: str, ok: bool | None = True, note: str = ""):
        """ok=None 仅记录未确认的检测；保留上次账号状态、成功保活时间和有效期。"""
        with _LOCK:
            now_ts = int(time.time())
            for a in self.accounts:
                if a["id"] == aid:
                    if ok is not None:
                        a["ok"] = ok
                    if ok is True:
                        a["last_keepalive"] = now_ts
                        # ponytail: 只信实际 Cookie 到期时间或原导入锚点，不凭检测成功虚推 48h。
                        a["expires_at"] = account_expiry(
                            a.get("cookies_exp"), a.get("expiry_anchor") or a.get("created_at"))
                    if note:
                        a["note"] = note[:300]
                    a["checked_at"] = now_ts
            _write(self.cfg.accounts_file, self.accounts)

    def mark(self, aid: str, ok: bool, note: str = ""):
        with _LOCK:
            for a in self.accounts:
                if a["id"] == aid:
                    a["ok"] = ok
                    a["note"] = note[:300]
                    a["checked_at"] = int(time.time())
            _write(self.cfg.accounts_file, self.accounts)

    def get_account(self, aid: str) -> dict | None:
        for a in self.accounts:
            if a["id"] == aid:
                return a
        return None

    def update_account(self, aid: str, **kw) -> dict | None:
        """更新标签 / 启用状态 / cookies / 有效期等；值为 None 的字段跳过。

        传了 cookies_exp 会自动重算 expires_at，不用调用方自己算。
        估算锚点 expiry_anchor 的维护规则（避免有效期被反复虚推）：
          - 读到了 hatch_vml 的真实 expires  -> 清空锚点，直接用真值；
          - 读不到且锚点为空                -> 把锚点钉在当下（只钉一次）；
          - 补入全新会话 cookie（重登）      -> 调用方显式传 expiry_anchor=now 重置。
        """
        with _LOCK:
            for a in self.accounts:
                if a["id"] == aid:
                    for k, v in kw.items():
                        if v is not None or k == "ok":
                            a[k] = v
                    if "cookies_exp" in kw and kw["cookies_exp"] is not None:
                        ce = a["cookies_exp"]
                        if _is_pos(ce.get("hatch_vml")):
                            a["expiry_anchor"] = None
                        elif not a.get("expiry_anchor"):
                            a["expiry_anchor"] = int(time.time())
                        a["expires_at"] = account_expiry(
                            ce, a.get("expiry_anchor") or a.get("created_at"))
                    _write(self.cfg.accounts_file, self.accounts)
                    return a
        return None

    def stats(self) -> dict:
        now = int(time.time())
        total = len(self.accounts)
        enabled = sum(1 for a in self.accounts if a.get("enabled", True))
        bad = sum(1 for a in self.accounts
                  if a.get("enabled", True) and a.get("ok") is False)
        healthy = sum(1 for a in self.accounts
                      if a.get("enabled", True) and a.get("ok") is True)
        expiring = sum(1 for a in self.accounts
                       if a.get("enabled", True)
                       and a.get("expires_at")
                       and a["expires_at"] - now < 3 * 86400)
        expired = sum(1 for a in self.accounts
                      if a.get("enabled", True)
                      and a.get("expires_at") and a["expires_at"] <= now)
        return {"total": total, "enabled": enabled, "disabled": total - enabled,
                "healthy": healthy, "error": bad,
                "expiring": expiring, "expired": expired}

    # ---------- 任务 ----------
    def _prune_tasks_locked(self, keep: int | None = None) -> int:
        """tasks.json 超上限自动裁最老（调用方必须已持有 _LOCK）。

        tasks 文件每次 update 全量重写，上限受控是写放大问题的根修；
        已完成任务才可裁，进行中的（queued/processing）永远保留。
        """
        limit = self.cfg.tasks_max if keep is None else keep
        if len(self.tasks) <= limit:
            return 0
        done = {k: v for k, v in self.tasks.items()
                if (v.get("status") or "").startswith(("completed", "failed"))}
        live = {k: v for k, v in self.tasks.items() if k not in done}
        if len(live) + len(done) <= limit:
            return 0
        order = sorted(done.items(), key=lambda kv: kv[1].get("updated_at") or 0)
        overflow = len(live) + len(done) - limit
        drop = {k for k, _ in order[:overflow]}
        self.tasks = {**live, **{k: v for k, v in done.items() if k not in drop}}
        return len(drop)

    def create_task(self, kind: str, prompt: str) -> dict:
        with _LOCK:
            tid = "task_" + uuid.uuid4().hex[:20]
            t = {"id": tid, "object": "task", "kind": kind, "prompt": prompt,
                 "status": "queued", "created_at": int(time.time()),
                 "updated_at": int(time.time()), "result": None, "error": None}
            self.tasks[tid] = t
            self._prune_tasks_locked()
            _write(self.cfg.tasks_file, self.tasks)
            return t

    def update_task(self, tid: str, **kw):
        with _LOCK:
            t = self.tasks.get(tid)
            if not t:
                return None
            t.update(kw)
            t["updated_at"] = int(time.time())
            self._prune_tasks_locked()
            _write(self.cfg.tasks_file, self.tasks)
            return t

    def get_task(self, tid: str) -> dict | None:
        return self.tasks.get(tid)

    def list_tasks(self, limit: int = 50) -> list[dict]:
        items = sorted(self.tasks.values(),
                       key=lambda t: t.get("created_at") or 0, reverse=True)
        return items[:limit]

    def delete_task(self, tid: str) -> bool:
        with _LOCK:
            if tid in self.tasks:
                del self.tasks[tid]
                _write(self.cfg.tasks_file, self.tasks)
                return True
            return False

    def clear_tasks(self, keep: int = 0) -> int:
        with _LOCK:
            items = sorted(self.tasks.values(),
                           key=lambda t: t.get("created_at") or 0, reverse=True)
            keep_ids = {t["id"] for t in items[:keep]}
            removed = len(self.tasks) - len(keep_ids)
            self.tasks = {k: v for k, v in self.tasks.items() if k in keep_ids}
            _write(self.cfg.tasks_file, self.tasks)
            return max(0, removed)
