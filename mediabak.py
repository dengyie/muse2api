"""muse2api 媒体云备份（MangoDrive / OpenList）—— patch8 二开模块。

设计（对齐 MangoDrive 业务方接入指南）：
- 生成完成的媒体文件 → 目录扫描 → OpenList REST `PUT /api/fs/put` 上传 →
  `fs/get` 回读 sha256 校验 → 校验一致才删本地；失败/不一致保留本地下轮重试。
- 双通道：mesh `10.144.144.3:5244` 优先，公网 `api-drive.mangoqwq.com` 自动回退
  （对齐 zcode-hub 备份先例）；公网需带类浏览器 UA（WAF）。
- 服务端异常（如当前 PROPFIND 405）→ 连续失败计数 ≥5 进入退避（30min 暂停扫描），
  本地文件永不因备份失败而删除（不丢片红线）。
- 目录布局：/Apps/<base>/muse2api/<YYYY-MM-DD>/<filename>，同名冲突跳过上传直接校验
  （幂等：上轮传完未删的文件本轮直接进入校验环节）。
- 配置全走 env（默认关闭，零侵入）：
    MUSE2API_BACKUP_ENABLED=0
    MUSE2API_BACKUP_APP_USER=app-muse2api
    MUSE2API_BACKUP_APP_PASS=          # 建议宿主 .env，不入 compose 明文
    MUSE2API_BACKUP_BASE=/Apps/muse2api
    MUSE2API_BACKUP_MESH_URL=http://10.144.144.3:5244
    MUSE2API_BACKUP_PUBLIC_URL=https://api-drive.mangoqwq.com
    MUSE2API_BACKUP_INTERVAL=300       # 扫描周期秒
    MUSE2API_BACKUP_DELETE_LOCAL=1     # 校验通过后删本地；0=只备份不删（服务端未验证期用）
    MUSE2API_BACKUP_MAX_AGE_HOURS=0    # 0=不限；>0 则超龄未备份文件直接删（磁盘保护）
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from urllib.parse import quote

import requests

log = logging.getLogger("muse2api.mediabak")

_UA = {"User-Agent": "Mozilla/5.0 (MangoDrive-consumer)"}
_TOKEN_TTL = 48 * 3600
_FAIL_BACKOFF_THRESHOLD = 5  # 连续 5 次才退避（生产实测 mesh 有单笔瞬时抖动，3 太敏感）
_FAIL_BACKOFF_SECS = 1800


def _cfg() -> dict:
    from config import CFG
    return {
        "enabled": getattr(CFG, "backup_enabled", False),
        "user": getattr(CFG, "backup_app_user", "app-muse2api"),
        "pass": getattr(CFG, "backup_app_pass", ""),
        "base": (getattr(CFG, "backup_base", "/Apps/muse2api") or "/Apps/muse2api").rstrip("/"),
        "mesh": getattr(CFG, "backup_mesh_url", "http://10.144.144.3:5244").rstrip("/"),
        "public": getattr(CFG, "backup_public_url", "https://api-drive.mangoqwq.com").rstrip("/"),
        "interval": max(60, int(getattr(CFG, "backup_interval", 300))),
        "delete_local": bool(getattr(CFG, "backup_delete_local", True)),
        "max_age_hours": max(0, int(getattr(CFG, "backup_max_age_hours", 0))),
        "media_dir": getattr(CFG, "media_dir", ""),
    }


def enabled() -> bool:
    c = _cfg()
    return bool(c["enabled"] and c["pass"] and c["media_dir"])


class MediaBackuper:
    """OpenList REST 客户端 + 备份执行器。线程安全（单守护线程消费）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._token: str | None = None
        self._token_at: float = 0.0
        self._chan: str | None = None      # 当前可用通道: "mesh" | "public"
        self._consecutive_fail = 0
        self._backoff_until = 0.0

    # ---- 通道与鉴权 ----
    def _base(self, chan: str) -> str:
        return _cfg()["mesh"] if chan == "mesh" else _cfg()["public"]

    def _login(self, chan: str) -> str | None:
        c = _cfg()
        try:
            r = requests.post(self._base(chan) + "/api/auth/login",
                              json={"username": c["user"], "password": c["pass"]},
                              headers=_UA, timeout=15)
            data = r.json()
            if data.get("code") == 200:
                return data["data"]["token"]
            log.warning("【媒体备份】%s login 失败: %s", chan, str(data.get("message"))[:100])
        except Exception as exc:  # noqa: BLE001
            log.warning("【媒体备份】%s login 异常: %s", chan, str(exc)[:100])
        return None

    def _get_token(self) -> tuple[str | None, str]:
        # patch10b：双检锁——快路径锁外返回，避免命中缓存也排队；慢路径锁内登录
        tok = self._token
        if tok and time.monotonic() - self._token_at < _TOKEN_TTL:
            return tok, self._chan
        with self._lock:
            now = time.monotonic()
            if self._token and now - self._token_at < _TOKEN_TTL:
                return self._token, self._chan
            order = ([self._chan] if self._chan else []) + \
                    [x for x in ("mesh", "public") if x != self._chan]
            for chan in order:
                tok = self._login(chan)
                if tok:
                    self._token, self._token_at, self._chan = tok, now, chan
                    log.info("【媒体备份】通道就绪: %s", chan)
                    return tok, chan
            return None, None

    def _invalidate_token(self):
        """token 被服务端作废（401/403）时清缓存，下一轮 _get_token 强制重登。
        否则死 token 会一直用到 48h TTL 到期，备份最长停摆 48h。"""
        self._token = None
        self._token_at = 0.0

    # ---- OpenList REST 原语 ----
    def _mkdir(self, tok: str, chan: str, path: str) -> bool:
        try:
            r = requests.post(self._base(chan) + "/api/fs/mkdir", json={"path": path},
                              headers={**_UA, "Authorization": tok}, timeout=15)
            data = r.json()
            if data.get("code") in (401, 403):
                self._invalidate_token()
            return data.get("code") == 200
        except Exception:  # noqa: BLE001
            return False

    def _put(self, tok: str, chan: str, rel_path: str, data: bytes) -> bool:
        try:
            r = requests.put(self._base(chan) + "/api/fs/put", data=data,
                             headers={**_UA, "Authorization": tok,
                                      "File-Path": quote(rel_path),
                                      "Content-Type": "application/octet-stream",
                                      "Content-Length": str(len(data))},
                             timeout=max(60, len(data) // (512 * 1024)))
            resp = r.json()
            if resp.get("code") in (401, 403):
                self._invalidate_token()
                return False
            return resp.get("code") == 200
        except Exception as exc:  # noqa: BLE001
            log.warning("【媒体备份】PUT 异常 %s: %s", rel_path[:80], str(exc)[:100])
            return False

    def _remote_exists(self, tok: str, chan: str, rel_path: str) -> bool:
        """存在性探测：fs/list 列父目录查文件名（不触碰不存在对象）。

        为什么不用 fs/get 探测：对不存在文件 fs/get 会在 OpenList 服务端
        记一条 WARN "failed get ... object not found"（2026-11-16 深度
        review 实锤，8 笔 muse2api 伪警报全部来自这里）。fs/list 对不存在
        目录返回 HTTP 层 code 500 但服务端日志零 WARN（同日实测），且一次
        list 能拿到整目录清单，对同目录批量备份还省请求。

        注意：返回 False 覆盖「目录不存在」与「探测本身失败」两种情形——
        后者会导致冗余重传（结果仍安全：sha 校验兜底），仅记录日志供排障。
        """
        if "/" not in rel_path:
            log.warning("【媒体备份】探测路径无目录段，跳过: %s", rel_path[:80])
            return False
        dir_path, name = rel_path.rsplit("/", 1)
        try:
            page = 1
            while True:
                r = requests.post(self._base(chan) + "/api/fs/list",
                                  json={"path": dir_path, "page": page, "per_page": 100},
                                  headers={**_UA, "Authorization": tok}, timeout=20)
                data = r.json()
                if data.get("code") in (401, 403):
                    self._invalidate_token()
                    return False
                if data.get("code") != 200:
                    return False  # 目录不存在 → 文件必然不存在
                content = (data.get("data") or {}).get("content") or []
                if any((x or {}).get("name") == name for x in content):
                    return True
                if len(content) < 100:
                    return False
                page += 1
                if page > 1000:  # 服务端异常（分页参数被忽略等）防死循环；1 万件目录也够用
                    log.warning("【媒体备份】fs/list 分页超上界，放弃探测: %s", dir_path[:80])
                    return False
        except Exception as exc:  # noqa: BLE001
            log.warning("【媒体备份】fs/list 探测异常（按不存在处理）: %s", str(exc)[:100])
            return False

    def _remote_sha(self, tok: str, chan: str, rel_path: str) -> str | None:
        """回读远端文件计算 sha256（诚实校验）。

        注意：WebDav 驱动的 fs/get `sign` 是 URL 签名令牌（`hmac:expiry`）而非
        sha256，hash_info 亦为 null——只能走 raw_url 回读实测（2026-11-15 实证）。
        """
        try:
            r = requests.post(self._base(chan) + "/api/fs/get",
                              json={"path": rel_path},
                              headers={**_UA, "Authorization": tok}, timeout=20)
            data = r.json()
            if data.get("code") in (401, 403):
                self._invalidate_token()
                return None
            if data.get("code") != 200:
                return None
            raw_url = (data.get("data") or {}).get("raw_url") or ""
            if not raw_url:
                return None
            # patch10b：超时随远端体积伸缩（与 _put 公式对齐），大文件公网回读不假超时
            size = int((data.get("data") or {}).get("size") or 0)
            rr = requests.get(raw_url, headers=_UA, timeout=max(60, size // (512 * 1024)))
            if rr.status_code != 200:
                return None
            return hashlib.sha256(rr.content).hexdigest()
        except Exception:  # noqa: BLE001
            return None

    def _remove_remote(self, tok: str, chan: str, dir_path: str, names: list[str]) -> bool:
        try:
            r = requests.post(self._base(chan) + "/api/fs/remove",
                              json={"dir": dir_path, "names": names},
                              headers={**_UA, "Authorization": tok}, timeout=15)
            data = r.json()
            if data.get("code") in (401, 403):
                self._invalidate_token()
            return data.get("code") == 200
        except Exception:  # noqa: BLE001
            return False

    # ---- 单文件备份（幂等：远端已有 → 直接校验） ----
    def backup_file(self, fp: str) -> str:
        """备份单个媒体文件。返回 "removed" | "kept-verified" | "kept-unverified" | "kept-failed"。

        红线：只有「远端存在且 sha256 与本地一致」才允许删本地。
        """
        c = _cfg()
        tok, chan = self._get_token()
        if not tok:
            return "kept-failed"
        rel = c["base"] + "/" + time.strftime("%Y-%m-%d") + "/" + os.path.basename(fp)
        dir_path = rel.rsplit("/", 1)[0]

        try:
            with open(fp, "rb") as f:
                data = f.read()
        except OSError as exc:
            log.warning("【媒体备份】读取失败 %s: %s", fp, str(exc)[:80])
            return "kept-failed"
        local_sha = hashlib.sha256(data).hexdigest()

        if not self._remote_exists(tok, chan, rel):
            # 目录可能不存在；OpenList mkdir 递归建（base_path 由账号隔离兜底）
            if not self._mkdir(tok, chan, dir_path):
                self._note_fail()
                return "kept-failed"
            if not self._put(tok, chan, rel, data):
                self._note_fail()
                return "kept-failed"
            remote_sha = self._remote_sha(tok, chan, rel)
        else:
            # 远端已有（上轮传完未删等）→ 直接进入校验环节（幂等语义不变）
            remote_sha = self._remote_sha(tok, chan, rel)

        if remote_sha and remote_sha.lower() == local_sha:
            self._note_ok()
            if c["delete_local"]:
                try:
                    os.unlink(fp)
                    log.info("【媒体备份】已备份并删除本地 %s (%.1fMB → %s)",
                             os.path.basename(fp), len(data) / 1048576, chan)
                    return "removed"
                except OSError:
                    pass
            return "kept-verified"
        if remote_sha:
            # 远端存在但 hash 不一致 → 换名重传（冲突自愈）
            alt = rel.rsplit(".", 1)
            rel2 = (alt[0] + f"-{local_sha[:8]}." + alt[1]) if len(alt) == 2 else rel + f"-{local_sha[:8]}"
            if self._put(tok, chan, rel2, data):
                remote2 = self._remote_sha(tok, chan, rel2)
                if remote2 and remote2.lower() == local_sha:
                    self._note_ok()
                    if c["delete_local"]:
                        try:
                            os.unlink(fp)
                            log.info("【媒体备份】重名冲突已换名备份 %s", os.path.basename(rel2))
                            return "removed"
                        except OSError:
                            pass
                    return "kept-verified"
        self._note_fail()
        return "kept-unverified"

    def _note_ok(self):
        with self._lock:
            self._consecutive_fail = 0
            self._backoff_until = 0.0

    def _note_fail(self):
        with self._lock:
            self._consecutive_fail += 1
            if self._consecutive_fail >= _FAIL_BACKOFF_THRESHOLD:
                self._backoff_until = time.monotonic() + _FAIL_BACKOFF_SECS
                log.warning("【媒体备份】连续失败 %d 次，退避 %d 分钟（本地文件保留）",
                            self._consecutive_fail, _FAIL_BACKOFF_SECS // 60)

    def in_backoff(self) -> bool:
        with self._lock:
            return time.monotonic() < self._backoff_until

    def status(self) -> dict:
        # in_backoff() 内部会再取 _lock（不可重入），这里直接读字段避免自死锁
        with self._lock:
            return {"channel": self._chan, "consecutive_fail": self._consecutive_fail,
                    "backoff": time.monotonic() < self._backoff_until}

    # ---- 扫描一轮 ----
    def scan_once(self) -> dict:
        if not enabled():
            return {"skipped": "disabled"}
        if self.in_backoff():
            return {"skipped": "backoff"}
        c = _cfg()
        d = c["media_dir"]
        now = time.time()
        done = {"removed": 0, "kept-verified": 0, "kept-unverified": 0, "kept-failed": 0, "purged": 0}
        for name in sorted(os.listdir(d)):
            fp = os.path.join(d, name)
            if not os.path.isfile(fp):
                continue
            age_h = (now - os.path.getmtime(fp)) / 3600
            # 磁盘保护：超龄未备份直接删（max_age_hours>0 才启用）
            if c["max_age_hours"] and age_h > c["max_age_hours"]:
                try:
                    os.unlink(fp)
                    done["purged"] += 1
                    continue
                except OSError:
                    pass
            # 刚完成的文件给 120s 落盘缓冲
            if age_h * 3600 < 120:
                continue
            r = self.backup_file(fp)
            done[r] += 1
            if r == "kept-failed":
                # 通道疑似故障：连续 2 笔 kept-failed 才终止本轮（单笔瞬时抖动
                # 不拖死全队列——生产首轮实测 mesh 偶发抖动会撞出单笔失败）
                if done["kept-failed"] >= 2:
                    log.warning("【媒体备份】连续 2 笔上传失败，疑似通道故障，本轮提前结束")
                    break
        if any(done.values()):
            log.info("【媒体备份】本轮: %s", json.dumps(done))
        return done

    def run_forever(self):
        while True:
            try:
                self.scan_once()
            except Exception as exc:  # noqa: BLE001
                log.warning("【媒体备份】扫描异常: %s", str(exc)[:120])
            time.sleep(_cfg()["interval"])


BACKUPER = MediaBackuper()
