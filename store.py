"""账号池与任务存储（JSON 落盘，原子写）。"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid

from proxy import public_proxy


def persist_env(key: str, value: str):
    """patch22r-review：配置持久化（从 app.py 迁入——与 _read/_write 同族的
    存储职责）。双路径：data/runtime.env（data 卷内，容器重建不丢）+ base/.env
    兼容；均 chmod 0600（此前 umask 默认 0644）。

    消费方：app 启动 key 自生成 / rotate_apikey / updater.push 的 GITHUB_TOKEN。
    加载侧是 config._load_dotenv 的 runtime.env > .env 顺序。"""
    import logging
    _log = logging.getLogger("muse2api.store")
    from config import CFG  # 函数内导入：config 不依赖 store，无循环
    targets = [os.path.join(CFG.data_dir, "runtime.env"),
               os.path.join(CFG.base_dir, ".env")]
    for path in targets:
        try:
            try:
                with open(path, encoding="utf-8") as f:
                    lines = f.read().splitlines()
            except OSError:
                lines = []
            found = False
            for i, ln in enumerate(lines):
                if ln.strip().startswith(key + "="):
                    lines[i] = f"{key}={value}"
                    found = True
            if not found:
                lines.append(f"{key}={value}")
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("\n".join(lines).strip() + "\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
            os.chmod(path, 0o600)
        except OSError as exc:
            _log.warning("【配置持久化】%s 写入失败: %s", path, str(exc)[:80])

_LOCK = threading.Lock()

# 登录会话存在性检查；可用性仍须经上游认证（与 engine 一致）
# hatch_vml 是可选工作区令牌，缺失不能据此拒绝会话导入
ESSENTIAL_COOKIES = ("hatch_sess",)

# patch30：账号视频风控熔断——连续 3 次「模型只回文本」（refusal，疑似账号级
# 风控信号）冻结该账号视频链路 7 天（VIDEO_FREEZE_DAYS）。超时/附件断链/
# 代理错等链路或上游故障不计入（11-23 上游 503 故障窗实证：全池皆然，
# 计入会连环误冻）。冻结 = pick/acquire 跳过（不接新任务），不动 cookies/
# 出口绑定，到期自动解冻（thaw_due），成功一次即清零计数。
VIDEO_REFUSAL_FREEZE_AT = 3
VIDEO_FREEZE_DAYS = 7


# ---------------- patch17：每账号稳定指纹档案 ----------------
# 目的：号池规模化时，账号之间互不相同、单账号跨重启稳定（设备身份隔离）。
# 派生是纯函数：seed 相同 → 档案相同；seed 不同 → 档案不同。
# seed 用 secrets 生成并落库在账号上，是唯一随机源。

_FP_CHROME_MAJOR_MIN, _FP_CHROME_MAJOR_MAX = 130, 135
_FP_PLATFORMS = ("Windows", "macOS", "Linux")
# patch22i：muse.ai composer 在非英语 Accept-Language 下附件处理树不挂载
# （容器内对照实验：de-DE → setFileInputFiles 0/1、form_len=0；en-US → OK）。
# 指纹多样性以设备/时区/屏幕维度保留，语言维度锁英语系。
_FP_LOCALES = ("en-US", "en-GB")
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
        # patch22r：损坏文件改名留证 + 告警，绝不静默回空后又被下一次写入覆盖
        # （否则 tasks.json 损坏 = 全量档案无声清零）。
        try:
            os.replace(path, path + ".corrupt")
            print(f"[store] WARNING: {path} 损坏，已改名 .corrupt 留证，按空库启动",
                  flush=True)
        except OSError:
            pass
        return default


def _write(path: str, obj):
    """原子写 + fsync（patch22r）：os.replace 原子性 + 强制刷盘，掉电场景
    终态/幂等/归档记录不丢（对齐 QueuePolicy.save 的持久化标准）。
    patch22r-final：紧凑序列化——indent=1 占 ~32% 体积（tasks.json 442KB→~300KB），
    tasks/归档/幂等/账号全部写路径受益；文件只被本服务读写（人读用 python -m json.tool）。"""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
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
        # patch22o：归档索引（文件名 → 任务溯源档案）。任务库受 tasks_max 裁剪，
        # 但成品媒体永久备份在 OpenList——没有索引时「任务档案没了 → 成品成了
        # 无主文件」，取证只能靠时间窗猜（2026-11-18 OpenPet 409 取证实证断链）。
        # 索引随任务完成时写入，随 delete_media 删除，永不随 prune 蒸发。
        self.archive_index: dict[str, dict] = _read(cfg.archive_index_file, {})
        # patch22r-final：tasks 落盘节流时间戳（monotonic；0 = 尚未写过）
        self._last_tasks_flush = 0.0

    # ---------- 账号 ----------
    def add_account(self, cookies: dict, label: str = "",
                    cookies_exp: dict | None = None,
                    aid: str | None = None) -> dict:
        """aid：patch27 远程导号预分配的账号 ID（出口已按它绑定）。
        传入时用之，保证 TP 绑定/心跳与账号档案同键；冲突由调用方自查。
        缺省行为不变（uuid4 hex 12 位）。"""
        with _LOCK:
            # patch23：同一 muse.ai 会话（hatch_sess 是唯一核心凭证）拒绝重复
            # 导入——防同号双档案双计费。锁内比较，竞态安全。
            sess = (cookies or {}).get("hatch_sess", "")
            if sess:
                for existing in self.accounts:
                    if (existing.get("cookies") or {}).get("hatch_sess") == sess:
                        raise ValueError(f"duplicate_session:{existing.get('id')}:{existing.get('label')}")
            aid = (aid or uuid.uuid4().hex[:12]).strip() or uuid.uuid4().hex[:12]
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
            # patch30：视频熔断冻结中的账号不接新任务（懒判定：到期即视为可用，
            # 无需等心跳解冻）。仅当存在非冻结候选时收紧；全冻结则放行兜底
            # （可用性优先——全池熔断时宁可碰风控也不整体停摆）。
            not_frozen = [a for a in live if not self.video_frozen(a)]
            if not_frozen:
                live = not_frozen
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

    # ---------------- patch30：视频风控熔断（refusal×3 冻结 7 天） ----------------
    @staticmethod
    def video_frozen(account: dict) -> bool:
        """账号视频链路是否处于熔断冻结期（到期为假，懒判定不落盘）。"""
        until = account.get("video_frozen_until") or 0
        return bool(until) and time.time() < until

    def record_video_refusal(self, aid: str, note: str = "") -> dict | None:
        """视频 refusal（模型只回文本）计数 +1；达阈值冻结视频链路 VIDEO_FREEZE_DAYS 天。

        返回账号快照（含 frozen 标志）供日志/审计；账号不存在返回 None。
        成功生成（video_fail_streak 清零）见 record_video_success。
        """
        with _LOCK:
            for a in self.accounts:
                if a["id"] == aid:
                    # 已在冻结期：不再累计（冻结本身就是隔离，避免解冻前堆计数）
                    if self.video_frozen(a):
                        return dict(a)
                    streak = (a.get("video_fail_streak") or 0) + 1
                    a["video_fail_streak"] = streak
                    frozen = streak >= VIDEO_REFUSAL_FREEZE_AT
                    if frozen:
                        a["video_frozen_until"] = int(
                            time.time() + VIDEO_FREEZE_DAYS * 86400)
                        a["video_fail_streak"] = 0  # 冻结即清零，解冻后重新计
                    if note:
                        a["note"] = note[:300]
                    a["checked_at"] = int(time.time())
                    _write(self.cfg.accounts_file, self.accounts)
                    return dict(a)
            return None

    def record_video_success(self, aid: str):
        """视频生成成功 → 清零 refusal 连败计数（隔离期间不调用——成功即不在隔离）。"""
        with _LOCK:
            for a in self.accounts:
                if a["id"] == aid and a.get("video_fail_streak"):
                    a["video_fail_streak"] = 0
                    _write(self.cfg.accounts_file, self.accounts)

    def thaw_due_accounts(self) -> list[str]:
        """冻结到期的账号自动解冻（心跳每轮调用）；返回本次解冻的账号 ID。"""
        now_ts = int(time.time())
        thawed = []
        with _LOCK:
            for a in self.accounts:
                until = a.get("video_frozen_until") or 0
                if until and now_ts >= until:
                    a["video_frozen_until"] = None
                    a["video_fail_streak"] = 0
                    a["note"] = "视频熔断到期自动解冻"[:300]
                    thawed.append(a["id"])
            if thawed:
                _write(self.cfg.accounts_file, self.accounts)
        return thawed

    def thaw_account(self, aid: str) -> dict | None:
        """手动解冻（admin 操作）；返回更新后的账号，不存在返回 None。"""
        with _LOCK:
            for a in self.accounts:
                if a["id"] == aid:
                    a["video_frozen_until"] = None
                    a["video_fail_streak"] = 0
                    a["note"] = "人工解冻视频熔断"[:300]
                    a["checked_at"] = int(time.time())
                    _write(self.cfg.accounts_file, self.accounts)
                    return dict(a)
            return None

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
        patch22o：先按 tasks_keep_days 淘熟（0=不按时间淘，纯条数上限），
        再按条数裁——压测日 93 笔/天把 120 上限一天挤爆，2026-11-15 的任务
        档案全灭导致 OpenPet 409 取证断链。
        """
        limit = self.cfg.tasks_max if keep is None else keep
        done = {k: v for k, v in self.tasks.items()
                if (v.get("status") or "").startswith(("completed", "failed"))}
        live = {k: v for k, v in self.tasks.items() if k not in done}
        dropped = 0
        expired = set()  # patch22o：时间淘熟集合（keep_days=0 时为空）
        # patch22o：时间淘熟（保留窗口外的已完成任务整体移除）
        keep_days = getattr(self.cfg, "tasks_keep_days", 0)
        if keep_days:
            cutoff = time.time() - keep_days * 86400
            expired = {k for k, v in done.items()
                       if (v.get("created_at") or 0) < cutoff}
            if expired:
                for k in expired:
                    self._archive_snapshot_locked(k, done[k])  # 裁前先写归档索引
                done = {k: v for k, v in done.items() if k not in expired}
                dropped += len(expired)
        if len(live) + len(done) <= limit:
            if dropped:
                self.tasks = {**{k: v for k, v in self.tasks.items()
                                 if k not in expired}}
                # patch22r：写序钉死——archive_index 先落盘、tasks 后落盘。
                # 两写之间被 kill 最坏只留多余归档条目（delete_media 可清），
                # 绝不出现「tasks 已裁、归档未落盘」的溯源永久丢失窗口。
                self._write_archive_index_locked()
                _write(self.cfg.tasks_file, self.tasks)
            return dropped
        order = sorted(done.items(), key=lambda kv: kv[1].get("updated_at") or 0)
        overflow = len(live) + len(done) - limit
        drop = {k for k, _ in order[:overflow]}
        for k in drop:
            self._archive_snapshot_locked(k, done[k])  # 裁前先写归档索引
        doomed = expired | drop
        self.tasks = {**live, **{k: v for k, v in done.items() if k not in doomed}}
        # patch22r：同上，archive 先行（见 prune 早退分支注释）
        self._write_archive_index_locked()
        _write(self.cfg.tasks_file, self.tasks)
        return dropped + len(drop)

    # ---------- patch22o：归档索引 ----------
    def _archive_snapshot_locked(self, tid: str, t: dict):
        """任务档案被裁/删前，把溯源要素写进 archive_index（永不 prune）。

        记录幂等键/请求体指纹/终态/成品文件——后续同 key 重放或成品文件
        归属查询有权威依据（OpenPet 409 取证教训：档案没了只剩 nginx 时间窗）。
        patch22r：条数上限淘汰（archive_index_max，按 archived_at 淘最老）——
        索引永不 prune ≠ 无限增长，否则每次终态全量重写的体积随服务年龄
        线性膨胀（_LOCK 内写放大重演 tasks.json 旧病）。
        """
        if not (t.get("video_request_key") or t.get("image_request_key")):
            return
        self.archive_index[tid] = {
            "kind": t.get("kind"), "status": t.get("status"),
            "created_at": t.get("created_at"),
            "updated_at": t.get("updated_at"),
            "video_request_key": t.get("video_request_key"),
            "video_request_hash": t.get("video_request_hash"),
            "image_request_key": t.get("image_request_key"),
            "image_request_hash": t.get("image_request_hash"),
            "error_code": t.get("error_code"),
            "safe_to_resubmit": t.get("safe_to_resubmit"),
            "result": t.get("result"),
            "archived_at": int(time.time()),
        }
        mx = getattr(self.cfg, "archive_index_max", 0)
        if mx and len(self.archive_index) > mx:
            by_age = sorted(self.archive_index.items(),
                            key=lambda kv: kv[1].get("archived_at") or 0)
            for k, _ in by_age[:len(self.archive_index) - mx]:
                del self.archive_index[k]

    def _write_archive_index_locked(self):
        _write(self.cfg.archive_index_file, self.archive_index)

    def lookup_archive_by_media(self, filename: str) -> list[dict]:
        """按成品文件名反查溯源档案（patch22o 取证接口后端）。"""
        with _LOCK:
            out = []
            for tid, a in self.archive_index.items():
                fn = ((a.get("result") or {}).get("filename")
                      or ((a.get("result") or {}).get("url") or "").rstrip("/").rsplit("/", 1)[-1])
                if fn == filename:
                    out.append({"task_id": tid, **a})
            return out

    def remove_archive_by_media(self, filename: str) -> int:
        """删除媒体时清掉对应归档索引条目（patch22o）。"""
        with _LOCK:
            tids = [tid for tid, a in self.archive_index.items()
                    if (((a.get("result") or {}).get("filename")
                         or ((a.get("result") or {}).get("url") or "").rstrip("/").rsplit("/", 1)[-1])
                        == filename)]
            if not tids:
                return 0
            for tid in tids:
                del self.archive_index[tid]
            self._write_archive_index_locked()
            return len(tids)

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

    # patch22r-final：进度类更新的落盘节流窗口（秒）。非终态轻量更新在此窗口
    # 内只改内存不写盘，下一个超出窗口的更新或任何 create/delete/prune 落盘
    # 时捎带——崩溃窗口 ≤ 节流窗口，progress 回退对 sweep 语义无影响
    # （中断判定只看 status/stage，不看 progress）。证据：线上 183 笔 × ~15
    # 次生命周期更新 × 442KB ≈ 1.2GB 累计写/3 天，其中进度类占大头。
    TASK_FLUSH_THROTTLE = 2.0
    _TERMINAL_STATUS = ("completed", "failed", "succeeded", "success", "done", "cancelled")

    @classmethod
    def _is_progress_only(cls, kw: dict) -> bool:
        return bool(kw) and all(k in ("progress", "stage", "attempt_events",
                                      "generation_attempts", "quota_snapshot")
                                for k in kw)

    def _flush_tasks_if_due_locked(self, force: bool):
        """节流落盘：force（终态/结构性变更）立即写；进度类在窗口内合并。"""
        now = time.monotonic()
        if force or now - self._last_tasks_flush >= self.TASK_FLUSH_THROTTLE:
            _write(self.cfg.tasks_file, self.tasks)
            self._last_tasks_flush = now

    def update_task(self, tid: str, **kw):
        with _LOCK:
            t = self.tasks.get(tid)
            if not t:
                return None
            t.update(kw)
            t["updated_at"] = int(time.time())
            terminal = (t.get("status") or "").startswith(self._TERMINAL_STATUS)
            # patch22o：任务带成品落终态时同步归档索引（不依赖被裁才补写）
            if terminal and ((t.get("result") or {}).get("filename")
                             or (t.get("result") or {}).get("url")):
                self._archive_snapshot_locked(tid, t)
                self._write_archive_index_locked()
            self._prune_tasks_locked()
            # patch22r-final：进度类非终态更新节流落盘（写合并）；终态/含
            # 幂等键/归档/结构性字段立即落盘（恢复语义依赖它们的持久性）
            force = (not self._is_progress_only(kw)) or terminal
            self._flush_tasks_if_due_locked(force)
            return t

    def update_tasks_bulk(self, items: list[tuple[str, dict]]) -> int:
        """patch22r-final：批量更新 + 单次落盘——startup sweep 等批处理路径
        专用。原 sweep 对每个中断任务各调一次 update_task = N 次全量重写
        （92 任务 × 442KB ≈ 40MB 串行写 + event loop 阻塞秒级）。"""
        n = 0
        with _LOCK:
            for tid, kw in items:
                t = self.tasks.get(tid)
                if not t:
                    continue
                t.update(kw)
                t["updated_at"] = int(time.time())
                terminal = (t.get("status") or "").startswith(self._TERMINAL_STATUS)
                if terminal and ((t.get("result") or {}).get("filename")
                                 or (t.get("result") or {}).get("url")):
                    self._archive_snapshot_locked(tid, t)
                n += 1
            self._write_archive_index_locked()
            self._prune_tasks_locked()
            _write(self.cfg.tasks_file, self.tasks)
            self._last_tasks_flush = time.monotonic()
        return n

    def get_task(self, tid: str) -> dict | None:
        return self.tasks.get(tid)

    def list_tasks(self, limit: int = 50) -> list[dict]:
        # patch22r：快照 + 锁——原无锁 sorted 与并发建删任务交错可 RuntimeError
        # （sorted 内部迭代 dict values）。
        with _LOCK:
            items = sorted(self.tasks.values(),
                           key=lambda t: t.get("created_at") or 0, reverse=True)
        return items[:limit]

    def delete_task(self, tid: str, only_terminal: bool = False) -> bool:
        """patch22r：only_terminal=True 时锁内复查终态——堵 del_task/clear_tasks
        的「读状态→删」TOCTOU 窗口（queued 翻转 processing 后被删 = 付费孤儿
        媒体 + 幂等键蒸发，patch13(F12) 想防而未堵死的操作）。"""
        with _LOCK:
            t = self.tasks.get(tid)
            if not t:
                return False
            if only_terminal and (t.get("status") or "") in ("queued", "processing"):
                return False
            self._archive_snapshot_locked(tid, t)
            self._write_archive_index_locked()
            del self.tasks[tid]
            _write(self.cfg.tasks_file, self.tasks)
            return True

    def clear_tasks(self, keep: int = 0) -> int:
        with _LOCK:
            items = sorted(self.tasks.values(),
                           key=lambda t: t.get("created_at") or 0, reverse=True)
            keep_ids = {t["id"] for t in items[:keep]}
            removed = len(self.tasks) - len(keep_ids)
            for tid, t in self.tasks.items():
                if tid not in keep_ids:
                    self._archive_snapshot_locked(tid, t)
            self.tasks = {k: v for k, v in self.tasks.items() if k in keep_ids}
            # patch22r：archive 先行（与 _prune_tasks_locked 写序统一）
            self._write_archive_index_locked()
            _write(self.cfg.tasks_file, self.tasks)
            return max(0, removed)
