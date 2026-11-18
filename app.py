"""muse2api — 把 Muse(muse.ai) 网页免费账号的对话/生图/生视频额度反代成 API。

OpenAI 兼容:
  POST /v1/chat/completions          （文本 / 代码对话，支持 stream，可接 Codex）
  POST /v1/images/generations
  POST /v1/videos  +  GET /v1/videos/{task_id}
  GET  /v1/models
  GET  /v1/media/{name}

管理（前端账号池管理页面在 GET /）:
  GET    /admin/status
  GET    /admin/accounts
  POST   /admin/accounts            （单条 / 批量文本 / 批量数组）
  PATCH  /admin/accounts/{id}       （改标签、启用/禁用）
  DELETE /admin/accounts/{id}
  POST   /admin/accounts/{id}/test  （真实打开 muse.ai 验证会话是否有效）
  POST   /admin/accounts/{id}/relogin
  GET    /admin/tasks               （任务记录）
  DELETE /admin/tasks/{id}  |  POST /admin/tasks/clear
  GET    /admin/media  |  POST /admin/media/delete  |  DELETE /admin/media/{name}
  GET    /admin/extension           （浏览器扩展 zip，用来取 cookie）
  GET    /admin/cookie-helper       （命令行取 cookie 脚本，进阶）
"""
from __future__ import annotations

from typing import Any

import asyncio
import copy
from contextlib import contextmanager
import base64
import collections
import binascii
import io
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
import zipfile

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               Response, StreamingResponse)
from pydantic import model_validator, BaseModel, Field, SecretStr, field_validator

from config import CFG
from engine import (ESSENTIAL_COOKIES, MAX_REFERENCE_IMAGE_BYTES,
                    MAX_REFERENCE_IMAGES, MAX_REFERENCE_TOTAL_BYTES,
                    REFERENCE_MIME_EXT, MuseAuthError, MuseEngine, MuseGenerationError,
                    MuseModelRefusalError, MuseSessionUnavailable, RefImageSafetyError, reference_kind,
                    _stage_before)  # patch22d: worker 失败落库/sweep 均调用；缺失即 NameError→僵尸 processing 任务
from engine import MuseProxyError
from store import Store, account_expiry, min_expiry
from store import _read as _store_read, _write as _store_write
from proxy import normalize_server, request_proxy_context, split_proxy_url
import tetherpool
from tetherpool import MANAGED_SCHEME, TP
import mediabak
from mediabak import BACKUPER as MEDIA_BACKUPER
from generation_timing import QueuePolicy, QueueSettings, TaskTiming
from thumbnails import (VIDEO_FORMATS, ThumbnailUnavailable, clear_thumbnail,
                        source_version, video_thumbnail)

import sys
log = logging.getLogger("muse2api")
log.setLevel(logging.INFO)
if not log.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    log.addHandler(_h)

CFG.ensure_dirs()
app = FastAPI(title="muse2api", version="1.6.11")

# Cookie 助手脚本从 muse.ai 页面发起导入请求，需要放行该来源；
# 浏览器扩展从 chrome-extension:// 发起，也一并放行。
#
# 这里直接放行所有来源：本服务用 Bearer Key 鉴权、不依赖 Cookie，
# 放行来源不会带来越权风险；反之如果把来源限死，浏览器端的智能体
# （Open WebUI / LobeChat / 各种 Web 客户端）会被 CORS 拦住用不了。
_origins = [o.strip() for o in (CFG.cors_origins or "").split(",") if o.strip()]
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

@app.middleware("http")
async def _body_size_guard(request, call_next):
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > _BODY_LIMIT:
        return JSONResponse(status_code=413, content={
            "error": {"message": "Request body too large",
                      "type": "invalid_request_error"}})
    return await call_next(request)


app.add_middleware(CORSMiddleware,
                   allow_origins=["*"],
                   allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
                   allow_headers=["*"],
                   expose_headers=["*"],
                   max_age=600)

# patch13(F11)：服务级请求体上限——40MiB/100MiB 的素材校验发生在 Starlette
# 全量读入之后，无此防线单次超大 POST + 并发即可 OOM。
_BODY_LIMIT = 128 * 1024 * 1024

store = Store(CFG)
engine = MuseEngine(CFG)
engine.proxy_resolver = lambda aid: (store.get_account(aid) or {}).get("proxy")


def _fingerprint_resolver(aid: str):
    """patch17：账号指纹档案解析——缺档案的存量账号现场生成并回写。"""
    acc = store.get_account(aid)
    if not acc:
        return None
    fp = acc.get("fingerprint")
    if not fp:
        import secrets
        from store import derive_fingerprint
        fp = derive_fingerprint(secrets.token_hex(16))
        store.update_account(aid, fingerprint=fp)
    return fp


engine.fingerprint_resolver = _fingerprint_resolver


class ConcurrencyLock:
    """多账号并发调度锁。
    由 CFG.max_concurrency 限制最大并发槽位；
    同时提供与标准 Lock 兼容的 acquire / release / locked 接口，
    确保向下兼容历史单锁断言与管理端操作。"""
    def __init__(self, limit: int = 2):
        self.limit = max(1, limit)
        self._sem = threading.Semaphore(self.limit)
        self._count_lock = threading.Lock()
        self._active_count = 0

    def acquire(self, blocking: bool = True, timeout: float | None = -1) -> bool:
        if timeout is None:
            timeout = -1
        if timeout < 0:
            ok = self._sem.acquire(blocking=blocking)
        else:
            ok = self._sem.acquire(blocking=blocking, timeout=timeout)
        if ok:
            with self._count_lock:
                self._active_count += 1
        return ok

    def release(self):
        with self._count_lock:
            if self._active_count > 0:
                self._active_count -= 1
        self._sem.release()

    def locked(self) -> bool:
        with self._count_lock:
            return self._active_count > 0

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


class AccountPoolManager:
    """账号并发冲突管理器：确保同一账号不会同时被两个并发任务操作，多账号则并行执行。"""
    def __init__(self, store_instance):
        self.store = store_instance
        self.busy_accounts: set[str] = set()
        self.cond = threading.Condition()

    def acquire(self, account_id: str | None = None, preferred_id: str | None = None,
                rotate: bool = True, force_rotate: bool = False,
                timeout: float | None = 60.0, record_use: bool = True) -> dict:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self.cond:
            while True:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise MuseGenerationError("等待空闲账号超时，当前所有账号均处于繁忙中")

                if account_id:
                    acc = self.store.get_account(account_id)
                    if not acc or not acc.get("enabled", True):
                        raise MuseAuthError(f"指定账号 {account_id} 不存在或已禁用")
                    if account_id not in self.busy_accounts:
                        self.busy_accounts.add(account_id)
                        return acc
                else:
                    pref = preferred_id if preferred_id not in self.busy_accounts else None
                    acc = self.store.pick_account(
                        preferred_id=pref,
                        rotate=rotate,
                        force_rotate=force_rotate,
                        exclude_ids=self.busy_accounts,
                        record_use=record_use,
                    )
                    if acc:
                        self.busy_accounts.add(acc["id"])
                        return acc

                live = [a for a in self.store.accounts if a.get("enabled", True) and a.get("cookies")]
                if not live:
                    raise MuseAuthError("没有可用账号，请先在管理页面导入 cookie")

                self.cond.wait(timeout=0.5 if remaining is None else min(remaining, 0.5))

    def release(self, account_id: str | None):
        if not account_id:
            return
        with self.cond:
            self.busy_accounts.discard(account_id)
            self.cond.notify_all()


GEN_LOCK = ConcurrencyLock(limit=CFG.max_concurrency)
ACCOUNT_POOL = AccountPoolManager(store)
IMAGE_TASK_LOCK = threading.Lock()
# patch12：video 幂等并发保护（对齐 IMAGE_TASK_LOCK；无锁 check-then-create
# 在并发同 key 下互相 miss → 重复建 task 重复计费）
VIDEO_TASK_LOCK = threading.Lock()
# patch13(F4)：幂等索引与 task 生命周期解耦——task 会被 _prune_tasks_locked
# 裁剪（tasks_max），key 只寄生在 task 上时同 key 重放会 miss 重复计费。
# patch22o：索引落盘 idempotency_videos.json（store._write 原子写）——纯内存
# 索引在容器重启后即丢，重启后同 key 重放会重复建任务重复计费（OpenPet 409
# 取证实锤的另一半断链）。
IDEMPOTENCY_VIDEOS: dict[str, dict] = {}  # key_hash -> {task_id, request_hash, ts}
IDEMPOTENCY_TTL = 24 * 3600
IDEMPOTENCY_FILE = os.path.join(CFG.data_dir, "idempotency_videos.json")


def _idem_load():
    # 注意：本模块的 `store` 是 Store 实例（109 行），模块级 _read/_write 需显式导入
    IDEMPOTENCY_VIDEOS.update(
        {k: v for k, v in _store_read(IDEMPOTENCY_FILE, {}).items()
         if time.time() - (v.get("ts") or 0) <= IDEMPOTENCY_TTL})


def _idem_write():
    _store_write(IDEMPOTENCY_FILE, IDEMPOTENCY_VIDEOS)
QUEUE_POLICY = QueuePolicy(os.path.join(CFG.data_dir, "generation-queue.json"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _bind_account_proxy(account: dict):
    """Pin the selected account's route for the whole operation, including retries."""
    if hasattr(engine, "_ensure_local"):
        engine._ensure_local().proxy_snapshot = copy.deepcopy(account.get("proxy")) or None


def _clear_account_proxy():
    if hasattr(engine, "_ensure_local"):
        local = engine._ensure_local()
        if hasattr(local, "proxy_snapshot"):
            del local.proxy_snapshot


def _account_proxy(account: dict):
    if hasattr(engine, "_ensure_local"):
        local = engine._ensure_local()
        if hasattr(local, "proxy_snapshot"):
            return local.proxy_snapshot
    return account.get("proxy")


@contextmanager
def _account_operation(aid: str):
    """Maintenance and settings share the same account lease as generation."""
    with ACCOUNT_POOL.cond:
        account = store.get_account(aid)
        if not account:
            raise HTTPException(404, "账号不存在")
        if aid in ACCOUNT_POOL.busy_accounts:
            raise HTTPException(409, "账号正在执行任务或检测，请稍后再试")
        ACCOUNT_POOL.busy_accounts.add(aid)
        account = copy.deepcopy(account)
    try:
        _bind_account_proxy(account)
        yield account
    finally:
        try:
            engine.close_current_page()
        finally:
            _clear_account_proxy()
            ACCOUNT_POOL.release(aid)


# ------------------------- OpenAI 风格的错误响应 -------------------------
# 各类智能体基本都按 OpenAI 的 {"error": {"message": ...}} 取错误信息；
# FastAPI 默认返回的是 {"detail": ...}，客户端会读不到原因、只显示"未知错误"。
# 所以 /v1/* 统一转成 OpenAI 格式，管理接口保持原样（前端依赖 detail）。
def _err_type(status: int) -> str:
    if status == 404:
        return "not_found_error"
    if status == 429:
        return "rate_limit_error"
    if status >= 500:
        return "server_error"
    return "invalid_request_error"


@app.exception_handler(HTTPException)
async def _http_exc(request: Request, exc: HTTPException):
    if request.url.path.startswith("/v1/"):
        # patch22q：结构化 409 诊断——dict detail 直接透传为错误体字段
        # （idempotency_key_conflict 带 existing_task_id/双方摘要/时间），
        # OpenPet 机读；str detail 保持 OpenAI 兼容外壳不变。
        if isinstance(exc.detail, dict):
            return JSONResponse(status_code=exc.status_code,
                                content=exc.detail,
                                headers=getattr(exc, "headers", None))
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"message": str(exc.detail),
                               "type": _err_type(exc.status_code),
                               "param": None, "code": exc.status_code}},
            headers=getattr(exc, "headers", None))
    return JSONResponse(status_code=exc.status_code,
                        content={"detail": exc.detail},
                        headers=getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def _validation_exc(request: Request, exc: RequestValidationError):
    # Never echo user-supplied proxy credentials in validation errors.
    errors = [{k: v for k, v in error.items() if k not in ("input", "ctx")}
              for error in exc.errors()]
    if request.url.path.startswith("/v1/"):
        return JSONResponse(status_code=422, content={"error": {
            "message": "请求参数校验失败：" + str(errors)[:400],
            "type": "invalid_request_error", "param": None, "code": 422}})
    return JSONResponse(status_code=422, content={"detail": errors})

MODELS = [
    {"id": "muse-spark", "object": "model", "owned_by": "muse",
     "description": "Muse Spark —— 文本 / 代码对话（网页免费额度，支持流式）"},
    {"id": "muse-image", "object": "model", "owned_by": "muse",
     "description": "Muse Image —— 文生图 / 图像编辑（网页免费额度）"},
    {"id": "muse-video", "object": "model", "owned_by": "muse",
     "description": "Muse Video —— 文生视频 / 图生视频（网页免费额度）"},
]

# 下游（Codex / Cline / 各种客户端）习惯按 OpenAI、Anthropic 的名字传模型，
# 这里统一映射到 muse 的真实能力上。
#
# 说明：muse.ai 网页是自动路由的 agent，对外**没有可枚举的模型清单**，
# 能稳定调用的就是三条真实能力 —— Muse Spark（语言/代码）、
# Muse Image（生图）、Muse Video（生视频）。别名只是让下游不用改配置。
MODEL_ALIASES = {
    # ---- 文本 / 代码 → muse-spark ----
    "muse-text": "muse-spark", "muse-chat": "muse-spark", "muse-llm": "muse-spark",
    "koda": "muse-spark",
    "gpt-3.5-turbo": "muse-spark", "gpt-4": "muse-spark", "gpt-4-turbo": "muse-spark",
    "gpt-4o": "muse-spark", "gpt-4o-mini": "muse-spark", "gpt-4.1": "muse-spark",
    "gpt-4.1-mini": "muse-spark", "gpt-5": "muse-spark", "gpt-5-codex": "muse-spark",
    "o1": "muse-spark", "o1-mini": "muse-spark", "o3": "muse-spark",
    "o3-mini": "muse-spark", "o4-mini": "muse-spark",
    "codex": "muse-spark", "codex-mini-latest": "muse-spark",
    "claude-3-5-sonnet": "muse-spark", "claude-3-5-sonnet-latest": "muse-spark",
    "claude-3-7-sonnet": "muse-spark", "claude-sonnet-4": "muse-spark",
    "claude-opus-4": "muse-spark", "claude-3-opus": "muse-spark",
    "claude-3-haiku": "muse-spark",
    "deepseek-chat": "muse-spark", "deepseek-coder": "muse-spark",
    "deepseek-reasoner": "muse-spark", "qwen-coder": "muse-spark",
    "gemini-2.5-pro": "muse-spark", "gemini-2.5-flash": "muse-spark",
    # ---- 生图 → muse-image ----
    "muse-img": "muse-image", "dall-e": "muse-image", "dall-e-3": "muse-image",
    "gpt-image-1": "muse-image", "flux": "muse-image", "midjourney": "muse-image",
    # ---- 生视频 → muse-video ----
    "muse-vid": "muse-video", "muse-videos": "muse-video",
    "sora": "muse-video", "sora-2": "muse-video", "veo": "muse-video",
    "veo-3": "muse-video", "kling": "muse-video", "runway": "muse-video",
}


def resolve_model(name: str | None, default: str = "muse-image") -> str:
    n = (name or "").strip().lower()
    return MODEL_ALIASES.get(n, n or default)


# ------------------------- 鉴权 -------------------------
def auth(authorization: str | None = Header(default=None)):
    if not CFG.api_key:
        return True
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "缺少 Authorization: Bearer <key>")
    parts = authorization.split(None, 1)
    token = parts[1].strip() if len(parts) > 1 else ""
    import secrets
    if not token or not secrets.compare_digest(token, CFG.api_key):
        raise HTTPException(401, "API key 无效")
    return True


# ------------------------- 请求模型 -------------------------

def _persist_session_cookies(acc_id: str, cookies: dict, cookies_exp: dict | None) -> None:
    """Merge cookies rotated by muse.ai into storage without confirming the session."""
    acc = store.get_account(acc_id)
    if not acc or not cookies:
        return
    merged = {**(acc.get("cookies") or {}), **{k: v for k, v in cookies.items() if v}}
    exps = {**(acc.get("cookies_exp") or {}),
            **{k: v for k, v in (cookies_exp or {}).items() if v}}
    if merged == (acc.get("cookies") or {}) and exps == (acc.get("cookies_exp") or {}):
        return
    # synced_at / last_keepalive mean "session confirmed"; rotation alone does not.
    store.update_account(acc_id, cookies=merged, cookies_exp=exps)


engine.cookie_sink = _persist_session_cookies


def _renew_and_persist(acc_id: str, wake_vm: bool = True, force: bool = False) -> dict | None:
    """调用 /api/session 续签账号 cookie 并写回 store，返回最新账号 dict。
    近期（10分钟内）已续签且状态正常的账号直接复用，避免每次请求阻塞 2~3 秒 HTTP 往返。"""
    acc = store.get_account(acc_id)
    if not acc or not acc.get("cookies"):
        return acc
    now = time.time()
    last_sync = max(
        int(acc.get("synced_at") or 0),
        int(acc.get("last_keepalive") or 0),
        int(engine._last_http_renew.get(acc_id, 0)),
    )
    if not force and acc.get("ok") is True and (now - last_sync) < 600:
        engine._last_http_renew[acc_id] = last_sync
        return acc
    try:
        res = engine.renew_session_http(acc["cookies"], acc.get("cookies_exp"), wake_vm=wake_vm,
                                        **({"proxy": _account_proxy(acc)} if _account_proxy(acc) else {}))
        engine._last_http_renew[acc_id] = now
        if res.get("cookies"):
            store.update_account(acc_id, cookies=res["cookies"],
                                 cookies_exp=res.get("cookies_exp"),
                                 ok=True if res.get("ok") else acc.get("ok"),
                                 synced_at=int(now))
            store.touch_keepalive(acc_id, True, f"会话正常 (VM: {res.get('vm_state') or 'RUNNING'})")
    except MuseAuthError as exc:
        store.mark(acc_id, False, str(exc))
        raise
    except MuseProxyError:
        raise
    except MuseSessionUnavailable as exc:
        _persist_session_cookies(acc_id, exc.cookies, exc.cookies_exp)
        # The page load right after must not renew again with the pre-rotation cookie.
        engine._last_http_renew[acc_id] = now
        log.warning("HTTP 预续签账号 %s 未分配工作区%s: %s", acc_id,
                    "（已保存轮换后的 cookie）" if exc.rotated else "", exc)
    except Exception as exc:
        log.warning("HTTP 预续签账号 %s 异常: %s", acc_id, exc)
    return store.get_account(acc_id)


def safe_chat_stream(cookies: dict, prompt: str, expires: dict | None,
                     timeout: int, account_id: str | None):
    """Acquire a generation slot before the account lease; release both after cleanup."""
    import queue
    q = queue.Queue(maxsize=100)
    stop_event = threading.Event()

    def worker():
        cur_id = account_id
        cur_cookies = cookies
        cur_exp = expires
        last_exc = None
        leased_id = None
        slot_acquired = False
        try:
            slot_acquired = GEN_LOCK.acquire(timeout=timeout)
            if not slot_acquired:
                q.put(("error", MuseGenerationError("等待对话并发槽位超时")))
                return
            for attempt in range(2):
                if stop_event.is_set():
                    return
                if attempt > 0:
                    if leased_id:
                        ACCOUNT_POOL.release(leased_id)
                        leased_id = None
                    try:
                        alt = ACCOUNT_POOL.acquire(rotate=True, force_rotate=True, timeout=timeout)
                    except Exception:
                        break
                    if not alt or alt["id"] == cur_id:
                        if alt:
                            leased_id = alt["id"]
                        break
                    cur_id = alt["id"]
                    leased_id = cur_id
                    cur_cookies = alt["cookies"]
                    cur_exp = alt.get("cookies_exp")
                    log.info("【对话自动切号】切换到备用账号 %s (%s) 重试...", alt.get("label"), cur_id)
                elif cur_id:
                    ACCOUNT_POOL.acquire(account_id=cur_id, timeout=timeout)
                    leased_id = cur_id

                yielded = False
                try:
                    if cur_id:
                        _bind_account_proxy(store.get_account(cur_id) or {})
                        refreshed = _renew_and_persist(cur_id, wake_vm=True, force=(attempt > 0))
                        if refreshed:
                            cur_cookies = refreshed["cookies"]
                            cur_exp = refreshed.get("cookies_exp")
                    if stop_event.is_set():
                        return
                    engine.start()
                    for chunk in engine.chat_stream(
                        cur_cookies, prompt, cur_exp, timeout,
                        account_id=cur_id, stop_event=stop_event
                    ):
                        yielded = True
                        q.put(("data", chunk))
                        if stop_event.is_set():
                            return
                    if cur_id:
                        store.mark(cur_id, True, "")
                        _sync_cookies(cur_id)
                    return
                except MuseAuthError as exc:
                    last_exc = exc
                    if cur_id:
                        store.mark(cur_id, False, str(exc))
                    engine.close_current_page()
                    if yielded:
                        break
                except Exception as exc:
                    last_exc = exc
                    try:
                        engine.reset_thread()
                    except Exception:
                        pass
                    if yielded:
                        break
            if last_exc is not None:
                q.put(("error", last_exc))
        except Exception as exc:
            q.put(("error", exc))
        finally:
            try:
                if leased_id:
                    ACCOUNT_POOL.release(leased_id)
                engine.close_current_page()
            except Exception as exc:
                log.warning("对话清理失败: %s", type(exc).__name__)
            finally:
                try:
                    _clear_account_proxy()
                finally:
                    if slot_acquired:
                        GEN_LOCK.release()
                    q.put(("done", None))

    threading.Thread(target=worker, daemon=True).start()

    try:
        while True:
            kind, val = q.get()
            if kind == "data":
                yield val
            elif kind == "error":
                raise val
            else:
                break
    finally:
        stop_event.set()


class ImageRequest(BaseModel):
    prompt: str
    model: str = "muse-image"
    n: int = 1
    size: str | None = None
    aspect_ratio: str | None = None
    response_format: str = "url"      # url | b64_json
    timeout: int | None = Field(default=None, ge=1, le=600)
    extra: str | None = None
    image: Any = None
    images: list | None = None
    videos: list | None = None
    audios: list | None = None
    files: list | None = None
    reference_image: str | None = None
    async_: bool = Field(False, alias="async")


class VideoRequest(BaseModel):
    prompt: str
    model: str = "muse-video"
    # patch13(F9)：边界校验——无界 timeout 会让 worker 近乎永久占 GEN_LOCK
    # 槽位；无界 duration 直接拼进 prompt。
    duration: int | None = Field(default=None, ge=1, le=60)
    size: str | None = None
    aspect_ratio: str | None = None
    resolution: str | None = None
    timeout: int | None = Field(default=None, ge=1, le=1800)  # patch13(F9)
    extra: str | None = None
    image: Any = None
    images: list | None = None
    videos: list | None = None
    audios: list | None = None
    files: list | None = None
    image_url: Any = None
    reference_image: str | None = None


class ChatMessage(BaseModel):
    role: str
    content: str | list | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list | None = None


class ChatRequest(BaseModel):
    """OpenAI Chat Completions 请求。

    对第三方客户端要尽量宽松：不认识字段一律忽略（Pydantic 默认行为），
    只挑我们真正用得上的读 —— 否则各种智能体各传各的参数就会 422。
    """
    model: str = "muse-spark"
    messages: list[ChatMessage] = []
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    timeout: int | None = Field(default=None, ge=1, le=1800)  # patch13(F9)
    prompt: str | None = None       # 兼容把 prompt 直接放顶层的客户端
    # 下面这些声明出来只是为了「能读到」，muse.ai 端不做对应处理
    tools: list | None = None
    tool_choice: object | None = None
    response_format: object | None = None
    stream_options: object | None = None


class ResponsesRequest(BaseModel):
    """OpenAI Responses API（新版 Codex 默认走这个）。"""
    model: str = "muse-spark"
    input: str | list | None = None
    instructions: str | None = None
    stream: bool = False
    max_output_tokens: int | None = None
    timeout: int | None = None
    tools: list | None = None
    store: bool | None = None


class AccountProxy(BaseModel):
    server: str
    username: str = ""
    password: SecretStr | None = None

    @model_validator(mode="before")
    @classmethod
    def parse_proxy_url(cls, value):
        return split_proxy_url(value) if isinstance(value, dict) else value

    @field_validator("server")
    @classmethod
    def validate_server(cls, value):
        if value == MANAGED_SCHEME:
            return value  # TetherPool 托管占位，校验放行
        return normalize_server(value)

    @field_validator("username")
    @classmethod
    def validate_username(cls, value):
        if ":" in value or any(ord(c) < 32 for c in value):
            raise ValueError("Proxy username cannot contain a colon or control characters")
        try:
            value.encode("latin-1")
        except UnicodeError:
            raise ValueError("Proxy credentials must use Latin-1 characters") from None
        return value

    @field_validator("password")
    @classmethod
    def validate_password(cls, value):
        if value is not None:
            try:
                value.get_secret_value().encode("latin-1")
            except UnicodeError:
                raise ValueError("Proxy credentials must use Latin-1 characters") from None
        return value

    def configuration(self, previous=None):
        previous = previous or {}
        password = self.password.get_secret_value() if self.password is not None else None
        if password is None:
            if previous.get("server") == self.server and previous.get("username", "") == self.username:
                password = previous.get("password", "")
            elif self.username:
                raise HTTPException(400, "Enter the proxy password when changing its server or username")
            else:
                password = ""
        return {"server": self.server, "username": self.username, "password": password}


class AccountRequest(BaseModel):
    label: str = ""
    cookies: dict[str, str] = Field(default_factory=dict)
    cookie_header: str | None = None
    batch: str | None = None          # 多行文本，每行一个账号
    expires: dict[str, int] = Field(default_factory=dict)   # cookie 名 -> 过期时间戳
    proxy: AccountProxy | None = None
    # patch17：账号级出口约束（TetherPool 托管账号生效；acquire/重绑透传）
    region: str | None = None
    region_strict: bool | None = None
    egress: str | None = None


class AccountPatch(BaseModel):
    label: str | None = None
    enabled: bool | None = None
    proxy: AccountProxy | None = None
    region: str | None = None         # patch17：出口约束
    region_strict: bool | None = None
    egress: str | None = None


def _egress_constraints(acc: dict | None = None, req=None) -> dict:
    """账号档案 + 请求里的出口约束 → acquire constraints dict。"""
    out = {}
    src = {}
    if acc:
        src.update(region=acc.get("region"), region_strict=acc.get("region_strict"),
                   egress=acc.get("egress"))
    if req is not None:
        for k in ("region", "region_strict", "egress"):
            v = getattr(req, k, None)
            if v is not None:
                src[k] = v
    if src.get("region"):
        out["region"] = src["region"]
    if src.get("region_strict"):
        out["region_strict"] = "true"
    if src.get("egress"):
        out["egress_preference"] = src["egress"]
    return out


class ProxyTestRequest(BaseModel):
    proxy: AccountProxy


# ------------------------- 提示词构造 -------------------------
def _image_value(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        candidate = (value.get("image_url") or value.get("video_url") or value.get("audio_url")
                     or value.get("file_url") or value.get("url") or value.get("b64_json")
                     or value.get("data"))
        if isinstance(candidate, dict):
            candidate = candidate.get("url")
        return candidate.strip() if isinstance(candidate, str) and candidate.strip() else None
    return None


def _reference_list(images: list | None, legacy: list[Any], field: str,
                    extra: dict[str, list | None] | None = None) -> list[str]:
    """Validate ordered references and make legacy precedence explicit.

    顺序：images（或旧版单图字段）在前，其后依次是 videos / audios / files。
    images 本身也可以混放视频、音频与文档，类型在预处理时按 MIME/文件头识别。
    """
    primary = next((ref for value in legacy if (ref := _image_value(value))), None)
    if primary and images:
        raise HTTPException(400, f"{field} 不能与旧版单图字段同时使用")
    groups: list[tuple[str, list]] = []
    if primary:
        groups.append((field, [primary]))
    elif images is not None:
        groups.append((field, images))
    for name, values in (extra or {}).items():
        if values is not None:
            if not isinstance(values, list):
                raise HTTPException(400, f"{name} 必须是数组")
            groups.append((name, values))
    total = sum(len(values) for _, values in groups)
    if total > MAX_REFERENCE_IMAGES:
        raise HTTPException(400, f"参考素材最多 {MAX_REFERENCE_IMAGES} 个（当前 {total} 个）")
    refs = []
    for name, values in groups:
        for index, value in enumerate(values):
            ref = _image_value(value)
            if not ref:
                raise HTTPException(400, f"{name}[{index}] 不是有效素材引用")
            refs.append(ref)
    return refs


def _extra_media(req) -> dict[str, list | None]:
    return {"videos": req.videos, "audios": req.audios, "files": req.files}


def _image_references(req: ImageRequest) -> list[str]:
    return _reference_list(req.images, [req.reference_image, req.image], "images",
                           _extra_media(req))


def _video_references(req: VideoRequest) -> list[str]:
    return _reference_list(
        req.images, [req.reference_image, req.image_url, req.image], "images",
        _extra_media(req))


def _reference_kinds(prepared: list[str]) -> list[str]:
    """预处理后的 data URL -> image/video/audio/document 列表（保持顺序）。"""
    return [reference_kind(ref[5:].split(";", 1)[0]) for ref in prepared]


_KIND_LABELS = {"image": ("张", "图片"), "video": ("段", "视频"),
                "audio": ("段", "音频"), "document": ("份", "文档")}


def _describe_kinds(kinds: list[str]) -> str:
    counts = collections.Counter(kinds)
    return "、".join(f"{counts[k]} {_KIND_LABELS[k][0]}{_KIND_LABELS[k][1]}"
                    for k in ("image", "video", "audio", "document") if counts[k])


# 工作台提示词用 [图N] / [视频N] / [音频N] 指代素材，按同类顺序编号（与上传顺序一致）。
# muse 不认识这些标记，所以把「标记 -> 第几个附件」写进提示词。
_KIND_TOKENS = {"image": "图", "video": "视频", "audio": "音频", "document": "文档"}
_REFERENCE_TOKEN = re.compile(r"\[(?:图|视频|音频|文档)\d+\]")


def _reference_legend(kinds: list[str]) -> str:
    seen: collections.Counter = collections.Counter()
    parts = []
    for index, kind in enumerate(kinds, 1):
        seen[kind] += 1
        parts.append(f"[{_KIND_TOKENS[kind]}{seen[kind]}] 是第 {index} 个附件（{_KIND_LABELS[kind][1]}）")
    return "附件对照：" + "，".join(parts)

def build_image_prompt(r: ImageRequest, kinds: list[str] | None = None) -> str:
    refs = _image_references(r)
    has_ref = bool(refs)
    kinds = kinds or ["image"] * len(refs)
    if has_ref and any(k != "image" for k in kinds):
        parts = [f"基于我本次上传附带的参考素材（{_describe_kinds(kinds)}；{_reference_legend(kinds)}；"
                 f"只使用本次上传的这些附件，各附件的用途以下面的描述为准）"
                 f"进行生图/编辑，输出一张图片：{r.prompt.strip()}"]
    elif len(refs) > 1 and _REFERENCE_TOKEN.search(r.prompt):
        parts = [f"基于我本次上传附带的参考图片（{_reference_legend(kinds)}）进行生图/编辑：{r.prompt.strip()}"]
    elif has_ref:
        parts = [f"基于我本次上传附带的参考图片进行生图/编辑：{r.prompt.strip()}"]
    else:
        parts = [f"全新文生图创作（当前未提供任何参考图，请勿查找历史相册或向用户索要原图，直接根据文字描述从零绘制生成一张全新图片）：{r.prompt.strip()}"]
    ar = (r.aspect_ratio or "").strip().lower()
    sz = (r.size or "").strip().lower()

    if any(k in ar or k in sz for k in ("9:16", "9/16", "portrait", "竖屏", "720x1280", "1080x1920")):
        parts.append("【画面构图与比例要求】：严格 9:16 竖屏满屏画幅（9:16 vertical portrait aspect ratio，高大于宽的手机全屏竖版画面），绝对不要生成横屏，保持垂直构图")
    elif any(k in ar or k in sz for k in ("16:9", "16/9", "landscape", "横屏", "1280x720", "1920x1080")):
        parts.append("【画面构图与比例要求】：16:9 宽屏横屏画幅（16:9 widescreen landscape aspect ratio）")
    elif any(k in ar or k in sz for k in ("1:1", "square", "正方形", "1024x1024")):
        parts.append("【画面构图与比例要求】：1:1 正方形画幅（1:1 square aspect ratio）")
    elif any(k in ar or k in sz for k in ("4:3", "4/3")):
        parts.append("【画面构图与比例要求】：4:3 比例画幅")
    elif any(k in ar or k in sz for k in ("3:4", "3/4")):
        parts.append("【画面构图与比例要求】：3:4 竖向画幅")
    elif r.aspect_ratio:
        parts.append(f"【画面构图与比例要求】：{r.aspect_ratio} 画面比例")
    elif r.size:
        parts.append(f"尺寸/比例：{r.size}")

    if has_ref and "image" in kinds:
        parts.append("【纯净画面要求】：彻底清除并去除参考图中的所有文字、水印、签名、角标及Logo标记（clean image without any watermark, text, or logo），输出绝对纯净无字画面")

    if r.extra:
        parts.append(r.extra)
    return "，".join(parts)


def _content_text(content, strip_images: bool = False) -> str:
    """把 OpenAI 的 content 归一成纯文本（兼容多模态 list 形式）。

    strip_images=True：带图生成路径下图片将作为真实参考附件上传，
    "[图片]" 占位文本只会污染 prompt（P3 根修），直接丢弃。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                t = item.get("type")
                if t in (None, "text", "input_text", "output_text"):
                    parts.append(str(item.get("text") or ""))
                elif t == "image_url" and not strip_images:
                    parts.append("[图片]")
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(p for p in parts if p)
    return str(content)


def _chat_images(messages: list[ChatMessage]) -> list[str]:
    """提取多模态 messages 里的全部图片引用（标准 OpenAI vision 格式）。

    支持 content list 中的 {"type":"image_url","image_url":{"url":...}} 及裸字符串；
    url 可为 http(s):// 公网地址、data URI、裸 base64 —— 交给 _image_value 归一。
    """
    out: list[str] = []
    for m in messages:
        if not isinstance(m.content, list):
            continue
        for item in m.content:
            if not isinstance(item, dict) or item.get("type") != "image_url":
                continue
            v = item.get("image_url")
            if isinstance(v, dict):
                v = v.get("url")
            if isinstance(v, str) and v.strip():
                out.append(v.strip())
    return out


def _mentions_image_or_video_model(model: str) -> str | None:
    """model 指向生图/生视频时返回 kind（image/video），否则 None。"""
    n = (model or "").strip().lower()
    if n in ("muse-image",) or MODEL_ALIASES.get(n) == "muse-image":
        return "image"
    if n in ("muse-video",) or MODEL_ALIASES.get(n) == "muse-video":
        return "video"
    return None


def build_chat_prompt(messages: list[ChatMessage], strip_images: bool = False) -> str:
    """把 messages 拼成发给 muse.ai 的一段提示词。

    muse.ai 网页本身是个带上下文的会话，但本 API 是无状态的（每次可能落到
    不同账号/页面），所以把历史拼进 prompt 最可控 —— 这正好匹配 Codex 这类
    「每轮都带全量历史」的客户端。
    """
    system, turns = [], []
    for m in messages:
        role = (m.role or "").strip().lower()
        text = _content_text(m.content, strip_images=strip_images).strip()
        if role == "tool":
            # 工具执行结果 → 当成"用户提供的信息"发过去
            if text:
                turns.append(("user", "【工具执行结果】\n" + text))
            continue
        if not text:
            # 部分客户端的 assistant 消息只带 tool_calls、没有正文
            if role == "assistant" and m.tool_calls:
                turns.append(("assistant", "【请求调用工具】" + json.dumps(
                    m.tool_calls, ensure_ascii=False)[:600]))
            continue
        if role in ("system", "developer"):
            system.append(text)
        else:
            turns.append((role, text))

    # 单轮且无系统指令 → 直接发原文，最贴近自然对话
    if len(turns) == 1 and not system and turns[0][0] == "user":
        return turns[0][1]

    parts = []
    if system:
        sys_text = "\n\n".join(system)
        sys_text = sys_text.replace("danger-full-access", "standard-workspace-access")
        parts.append(f"背景与任务设定：\n{sys_text}")
    for role, text in turns:
        label = "助手" if role == "assistant" else "用户"
        parts.append(f"{label}：\n{text}")
    return "\n\n".join(parts)


# ------------------------- 工具调用（function calling）适配 -------------------------
# muse.ai 的网页模型**不会**返回结构化的 tool_calls，所以这里做一层协议适配：
#   1) 请求带 tools 时，把工具定义翻译成提示词里的【工具调用协议】；
#   2) 模型按协议输出 ```json {"tool": "...", "arguments": {...}} ```；
#   3) 我们把这段解析回 OpenAI 的 tool_calls 交给下游 agent。
#
# 注意：这是"尽力适配"而非保证 —— 目标模型是通用对话模型，没有针对
# function calling 做微调，遵守协议的程度需要实测观察。
_TOOL_PROTOCOL_HEAD = """你可以根据需要调用以下工具来协助用户完成任务。
若需调用工具，请直接输出如下格式的 JSON 代码块（不要包含其他多余解释）：
```json
{"name": "<工具名>", "arguments": {<参数>}}
```
如果需要调用多个工具，请输出包含多个对象的 JSON 数组。
如果无需调用工具，请直接用自然语言回答。

可用工具列表：
"""


def _describe_params(params) -> str:
    """把 JSON Schema 的参数描述成易读的多行文本。"""
    if not isinstance(params, dict):
        return "      （无参数）"
    props = params.get("properties") or {}
    required = set(params.get("required") or [])
    if not props:
        return "      （无参数）"
    lines = []
    for name, spec in props.items():
        spec = spec if isinstance(spec, dict) else {}
        lines.append("      - %s (%s, %s)%s" % (
            name, spec.get("type") or "any",
            "必填" if name in required else "可选",
            (" " + spec["description"]) if spec.get("description") else ""))
    return "\n".join(lines)


def build_tools_prompt(tools: list | None) -> str:
    """把 tools 定义翻译成提示词片段（没有工具时返回空串）。

    同时兼容 Chat Completions 的 `{"type":"function","function":{...}}`
    和 Responses API 的 `{"type":"function","name":...,"parameters":...}`。
    """
    if not tools:
        return ""
    items = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        if not fn.get("name"):
            continue
        desc = fn.get("description") or ""
        items.append("%d. %s%s\n   参数：\n%s" % (
            len(items) + 1, fn["name"], (" — " + desc) if desc else "",
            _describe_params(fn.get("parameters"))))
    if not items:
        return ""
    return _TOOL_PROTOCOL_HEAD + "\n".join(items) + "\n"


_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*([\s\S]*?)```")


def _as_tool_calls(obj) -> list[dict] | None:
    """把解析出的 JSON 转成 OpenAI tool_calls；不像工具调用就返回 None。"""
    raw = obj if isinstance(obj, list) else [obj]
    if not raw:
        return None
    out = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        name = item.get("tool") or item.get("name") or item.get("function")
        if not isinstance(name, str) or not name:
            return None
        args = item.get("arguments")
        if args is None:
            args = item.get("parameters") or item.get("args") or {}
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False)
        out.append({"id": "call_" + uuid.uuid4().hex[:20], "type": "function",
                    "function": {"name": name, "arguments": args}})
    return out or None


def parse_tool_calls(text: str) -> tuple[list[dict] | None, str]:
    """从模型输出里抽出工具调用。

    返回 (tool_calls, 剩余文本)；抽不到就返回 (None, 原文)。
    支持多种形态：
    1) ```json ... ``` 代码块；
    2) 裸 JSON（或带有前导 json/JSON 关键字）；
    3) 文本中内嵌的完整 JSON 对象或数组。
    """
    if not text:
        return None, text
    for m in reversed(list(_FENCE_RE.finditer(text))):
        try:
            calls = _as_tool_calls(json.loads(m.group(1).strip()))
        except Exception:
            continue
        if calls:
            return calls, (text[:m.start()] + text[m.end():]).strip()

    stripped = text.strip()
    clean_stripped = re.sub(r"^(?:```)?(?:json|JSON)?\s*", "", stripped).rstrip("`").strip()
    if clean_stripped.startswith(("{", "[")):
        try:
            calls = _as_tool_calls(json.loads(clean_stripped))
            if calls:
                return calls, ""
        except Exception:
            pass

    m_json = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", text)
    if m_json:
        try:
            calls = _as_tool_calls(json.loads(m_json.group(1).strip()))
            if calls:
                rest = (text[:m_json.start()] + text[m_json.end():]).strip()
                if re.sub(r"^(?:```)?(?:json|JSON)?\s*", "", rest).strip("`").strip() == "":
                    rest = ""
                return calls, rest
        except Exception:
            pass

    return None, text


def build_video_prompt(r: VideoRequest, kinds: list[str] | None = None) -> str:
    user_prompt = r.prompt.strip()
    ar = (r.aspect_ratio or "").strip().lower()
    sz = (r.size or "").strip().lower()
    dur = r.duration or 6
    refs = _video_references(r)
    has_ref = bool(refs)
    kinds = kinds or ["image"] * len(refs)
    if any(k != "image" for k in kinds):
        # 不再默认「第一张图作起始画面」：实测会把模型拉回图片场景、忽略参考视频
        reference_instruction = (
            f"基于我本次上传的参考素材（{_describe_kinds(kinds)}）生成视频"
            f"（{_reference_legend(kinds)}；只使用本次上传的这些附件，严禁使用历史素材；"
            "各附件的用途以下面的描述为准）")
    elif len(refs) > 1 and _REFERENCE_TOKEN.search(user_prompt):
        reference_instruction = (
            f"基于我本次上传的 {len(refs)} 张参考图片生成视频（{_reference_legend(kinds)}；"
            "各图片的用途以下面的描述为准，未说明时以第一张作为主要起始画面；严禁使用历史图片或其他图像）")
    elif len(refs) > 1:
        reference_instruction = (
            f"基于我本次上传的 {len(refs)} 张参考图片生成视频（以第一张作为主要起始画面，"
            "其余图片作为主体、场景与风格参考，严禁使用历史图片或其他图像）")
    else:
        reference_instruction = (
            "基于我本次上传的参考图片附件作为第一帧参考图（严禁使用历史图片或任何其他图像，"
            "必须严格以我当前刚刚上传并附带在此处的这张图片为起始帧）")

    gen_word = "图生视频" if all(k == "image" for k in kinds) else "视频"
    is_vertical = any(k in ar or k in sz for k in ("9:16", "9/16", "portrait", "竖屏", "720x1280", "1080x1920"))

    parts = []
    if is_vertical:
        if has_ref:
            parts.append(f"{reference_instruction}：生成一个严格9:16竖屏手机满屏的动态{gen_word}（9:16 vertical portrait video，720x1280，高大于宽的手机全屏竖版画面，严禁生成横屏或黑边，时长严格为 {dur} 秒）：{user_prompt}")
        else:
            parts.append(f"全新文生视频创作（纯文本全新生成，严禁参考任何历史图片或上下文）：生成一个严格9:16竖屏手机满屏视频（9:16 vertical portrait video，720x1280，高大于宽的手机全屏竖版画面，严禁生成横屏或带有左右黑边，保持垂直构图，时长严格为 {dur} 秒）：{user_prompt}")
    elif any(k in ar or k in sz for k in ("16:9", "16/9", "landscape", "横屏", "1280x720", "1920x1080")):
        if has_ref:
            parts.append(f"{reference_instruction}：生成一个16:9宽屏横屏{gen_word}（16:9 widescreen landscape video，时长严格为 {dur} 秒）：{user_prompt}")
        else:
            parts.append(f"全新文生视频创作（纯文本全新生成，严禁参考任何历史图片或上下文）：生成一个16:9横屏宽屏视频（16:9 widescreen landscape video，时长严格为 {dur} 秒）：{user_prompt}")
    else:
        if has_ref:
            parts.append(f"{reference_instruction}：生成动态{gen_word}（时长严格为 {dur} 秒）：{user_prompt}")
        else:
            parts.append(f"全新文生视频创作（纯文本全新生成，严禁参考任何历史图片或上下文）：生成一个视频（时长严格为 {dur} 秒）：{user_prompt}")

    if r.resolution:
        parts.append(f"画质规格：{r.resolution}")
    if r.extra:
        parts.append(r.extra)
    return "，".join(parts)


def media_url(name: str) -> str:
    """媒体地址。

    配了 public_base 就返回**绝对 URL** —— OpenAI 兼容客户端（以及各类智能体
    平台）拿到 data[].url 后一般会直接渲染或下载，相对路径会被解析到客户端
    自己的域名上，导致 404。public_base 为空时退回相对路径。
    """
    base = _public_base()
    return f"{base}/v1/media/{name}" if base else f"/v1/media/{name}"


# ------------------------- cookie 解析 -------------------------
def parse_cookie_text(text: str) -> dict[str, str]:
    """把 `a=1; b=2` / JSON / Set-Cookie 行解析成 dict。"""
    text = (text or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        import json
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return {str(k): str(v) for k, v in obj.items()}
        except Exception:  # noqa: BLE001
            pass
    out: dict[str, str] = {}
    for part in re.split(r"[;\n]+", text):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip()
        if k:
            out[k] = v.strip()
    return out


def parse_batch(text: str) -> list[tuple[str, dict]]:
    """批量导入：每行 `标签 | cookie串`，标签可省略。"""
    out: list[tuple[str, dict]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        label = ""
        body = line
        if "|" in line:
            head, tail = line.split("|", 1)
            if "=" not in head:          # `|` 出现在 cookie 里时不算分隔符
                label, body = head.strip(), tail.strip()
        cookies = parse_cookie_text(body)
        if cookies:
            out.append((label, cookies))
    return out


# ------------------------- 核心生成 -------------------------
def _sync_cookies(acc_id: str) -> dict:
    """生成完从浏览器读回 cookie 并写回账号池。

    实测结论：**核心 cookie 的 expires 不会被使用行为续期**（hatch_vml 固定
    约 2 天就到期），这里同步回来的主要是那些每次访问都会重新下发的非核心
    cookie（_fbp / wd / dpr 等）以及可能新增的条目，避免账号信息比实际更旧。

    注意：这个函数**绝不能把生成结果搞失败** —— 生成已经成功了，
    同步只是锦上添花，出任何问题都只记一条日志。
    """
    try:
        live = engine.read_cookies()
        if not live:
            return {"synced": 0}
        acc = store.get_account(acc_id)
        if not acc:
            return {"synced": 0}
        cur = dict(acc.get("cookies") or {})
        new_vals = {k: v["value"] for k, v in live.items() if v.get("value")}
        # 有效期要「合并」而不是「覆盖」：会话 cookie 读回的 expires 是 -1，
        # 若整体覆盖会把原有有效期记录清空（实测踩过这个坑）。
        exps = dict(acc.get("cookies_exp") or {})
        exps.update({k: v["expires"] for k, v in live.items()
                     if _pos(v.get("expires"))})
        changed = sum(1 for k, v in new_vals.items() if cur.get(k) != v)
        store.update_account(acc_id, cookies={**cur, **new_vals},
                             cookies_exp=exps, synced_at=int(time.time()))
        updated = store.get_account(acc_id) or {}
        return {"synced": len(new_vals), "changed": changed,
                "expires_at": updated.get("expires_at")}
    except Exception as exc:  # noqa: BLE001
        log.warning("cookie 同步失败（不影响本次生成）: %s", exc)
        return {"synced": 0, "error": str(exc)[:200]}


def _pos(v) -> bool:
    try:
        return int(v) > 0
    except (TypeError, ValueError):
        return False


_REFUSAL_REWRITE = ("【重要】上一次回复只有文字说明，没有生成任何媒体附件。"
                    "请立即直接生成并输出{kind_zh}文件本身，"
                    "不要用文字汇报、不要解释、不要询问，直接出片")


def _err_detail(exc: Exception) -> str:
    """patch9：错误详情字符串化，带机器可读 code 前缀（OpenPet Agent 契约）。"""
    code = getattr(exc, "code", "") or "generation_failed"
    return f"[{code}] {exc}"


def _rewrite_prompt_on_refusal(prompt: str, kind: str, attempt_no: int) -> str:
    """只回文本失败后的 prompt 改写：注入强制出片指令（第 2 次更强硬）。

    二开 P3 根修：去掉"原始需求："悬挂标签（原实现标签后无内容）；指令基于
    传进来的 prompt（重试链中始终是原始需求 + 本次指令），不再叠加多份指令文本。
    """
    kind_zh = "视频" if kind == "video" else "图片"
    return (f"{prompt}\n\n{_REFUSAL_REWRITE.format(kind_zh=kind_zh)}"
            + ("；这是第二次尝试，上一轮已失败一次，务必直接输出媒体附件，禁止任何纯文字回复"
               if attempt_no >= 2 else ""))


def _generation_callback(callback, *args):
    if callback:
        try:
            callback(*args)
        except Exception as exc:
            log.warning("Task metadata callback failed: %s", type(exc).__name__)


def _run_generation(prompt: str, kind: str, timeout: int,
                    account_id: str | None = None, on_progress=None,
                    reference_image: str | None = None,
                    reference_images: list[str] | None = None,
                    on_acquired=None, on_account=None, on_queue=None, on_error=None,
                    on_stage=None) -> tuple[dict, str | None]:
    if not any(a.get("enabled", True) and a.get("cookies") for a in store.accounts):
        raise MuseGenerationError("没有可用的生成账号，请检查账号池")
    # patch13(F1)：排队时限独立于生成时限——上游 1.6.11 引入 QueuePolicy，
    # 队列等待不再吃生成预算（原二开版 timeout-polled put 语义对齐）。
    queue_timeout = QUEUE_POLICY.settings()["queue_timeout"]
    t0 = time.monotonic()
    acquired = GEN_LOCK.acquire(timeout=queue_timeout or None)
    queued = time.monotonic() - t0
    _generation_callback(on_queue, queued)
    if not acquired:
        raise MuseGenerationError("排队时限已到，尚未开始生成；请稍后重试或调整独立排队时限")
    try:
        # patch15b：外层不再入账——语义统一为「发起上游生成即计」（锁内
        # generate 调用前唯一入账点），否则首次生成被记 2 次（现场实证）。
        return _run_generation_locked(prompt, kind, timeout, account_id,
                                      on_progress, reference_image, reference_images,
                                      on_acquired=on_acquired, on_account=on_account,
                                      on_queue=on_queue, on_error=on_error,
                                      queued=queued, queue_timeout=queue_timeout,
                                      on_stage=on_stage)
    finally:
        GEN_LOCK.release()


def _run_generation_locked(prompt: str, kind: str, timeout: int,
                    account_id: str | None = None, on_progress=None,
                    reference_image: str | None = None,
                    reference_images: list[str] | None = None,
                    deadline=None, on_acquired=None, on_account=None,
                    on_queue=None, on_error=None, queued=0.0,
                    queue_timeout=None, on_stage=None) -> tuple[dict, str | None]:
    if queue_timeout is None:
        queue_timeout = QUEUE_POLICY.settings()["queue_timeout"]

    def lease(**kwargs):
        # 队列预算覆盖一切等待（含切号重试排队）；生成预算只归生成。
        nonlocal queued, deadline
        remaining = max(0.001, queue_timeout - queued) if queue_timeout else None
        t0 = time.monotonic()
        try:
            return ACCOUNT_POOL.acquire(timeout=remaining, **kwargs)
        finally:
            waited = time.monotonic() - t0
            queued += waited
            if deadline is not None:
                deadline += waited
            _generation_callback(on_queue, queued)

    if deadline is None:
        deadline = time.monotonic() + max(1, timeout)
    cur_acc = lease(account_id=account_id, preferred_id=None, rotate=True)
    last_exc = None
    rebound = False  # 本轮是否完成过 TetherPool 重绑（单号池原地重试依据）
    base_prompt = prompt  # refusal 重写始终基于原始需求（不叠加多份指令文本）
    try:
        if on_acquired:
            _generation_callback(on_acquired)
        for attempt in range(3):  # 二开：2→3，refusal 自动重试
            if isinstance(last_exc, MuseModelRefusalError) or time.monotonic() >= deadline:
                if last_exc is not None:
                    raise last_exc
                raise MuseGenerationError("生成时限已到（不含排队时间），已停止等待")
            if attempt > 0:
                if isinstance(last_exc, MuseModelRefusalError):
                    # patch9：上游附件失效（带图零流量）改 prompt 修不了，立即终止重试链，
                    # 让 code=upstream_attachment_broken 原样上抛供 Agent 归因。
                    if getattr(last_exc, "code", "") == "upstream_attachment_broken":
                        log.warning("【快速失败】上游附件失效，跳过 refusal 重试链: %s", str(last_exc)[:120])
                        break
                    # 二开：只回文本≠拒绝终态，是上游随机行为（issue #52）。
                    # 不 break 不换号：清掉 last_exc，走下方正常生成路径重发（改写 prompt）。
                    # deadline 兜底：剩余时间不足以再出一轮就放弃。
                    if time.monotonic() >= deadline - 30:
                        break
                    prompt = _rewrite_prompt_on_refusal(base_prompt, kind, attempt)
                    log.info("【自动重试】只回文本，改写 prompt 后第 %d 次重发", attempt)
                    last_exc = None  # refusal 不换号不释放账号，直接落到下方重新生成
                else:
                    # 非 refusal（auth/proxy/其它）→ 沿用原切号重试逻辑（等待计入队列预算）
                    old_id = cur_acc["id"] if cur_acc else None
                    if old_id:
                        ACCOUNT_POOL.release(old_id)
                    cur_acc = None
                    try:
                        alt = lease(rotate=True, force_rotate=True)
                    except (MuseAuthError, MuseGenerationError):
                        raise last_exc
                    cur_acc = alt
                    if not alt or (old_id and alt["id"] == old_id):
                        # 池内无其他账号（如单号池）：若本轮刚完成 TetherPool 重绑，
                        # 新出口已生效，原地重试一次而不是直接放弃（P2 根修）。
                        if isinstance(last_exc, MuseProxyError) and rebound and attempt + 1 < 3:
                            log.info("【TetherPool】重绑已完成，单号池原地重试 %s (%s)", cur_acc.get("label"), cur_acc["id"])
                            continue
                        break
                    log.info("【生图/视频自动切号】切换到备用账号 %s (%s) 重试...", cur_acc.get("label"), cur_acc["id"])
            # 上游 1.6.11 语义：每次 attempt（含首次）都上报账号归属（TaskTiming.account
            # 记录 task.account/attempt，失败任务也保留账号溯源）。
            _generation_callback(on_account, cur_acc["id"], attempt + 1)
            try:
                _bind_account_proxy(cur_acc)
                refreshed = _renew_and_persist(cur_acc["id"], wake_vm=True, force=(attempt > 0))
                if refreshed:
                    cur_acc = refreshed
                engine.start()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MuseGenerationError("生成准备超时（不含排队时间），尚未发送本次提示词")
                refs = list(reference_images or [])
                engine_kwargs = {}
                if len(refs) == 1 and not reference_image:
                    reference_image = refs[0]
                elif len(refs) > 1:
                    engine_kwargs["reference_images"] = refs
                # patch15(F5)：发起即入账——refusal 改写/切号重试都是真实上游
                # 付费生成，失败/超时/取消同样要计入 attempt（原「成功后才入账」
                # 漏记重发那次，现场实证 task_bb4cb 2 次付费只记 1 次）。
                _generation_callback(on_acquired)
                # patch22：stage 回调透传——video worker 据此落库失败环节
                if on_stage:
                    engine_kwargs["on_stage"] = on_stage
                res = engine.generate(cur_acc["cookies"], prompt, expect=kind,
                                      timeout=remaining, expires=cur_acc.get("cookies_exp"),
                                      account_id=cur_acc["id"], on_progress=on_progress,
                                      reference_image=reference_image, **engine_kwargs)
                store.mark(cur_acc["id"], True, "")
                _sync_cookies(cur_acc["id"])
                return res, cur_acc["id"]
            except MuseAuthError as exc:
                last_exc = exc
                store.mark(cur_acc["id"], False, str(exc))
                engine.close_current_page()
            except MuseProxyError as exc:
                last_exc = exc
                store.touch_keepalive(cur_acc["id"], None, f"代理异常: {str(exc)[:160]}")
                engine.close_current_page()
                # 二开：TetherPool 托管账号代理异常 → 上报事件 + 强制重绑（下次循环生效）
                if tetherpool.enabled() and tetherpool.is_managed(cur_acc.get("proxy")):
                    # patch17e（P2）：异常重绑透传账号约束（报告事件接口无
                    # constraints 形参，重绑一步带上）。
                    new_proxy = TP.acquire(
                        cur_acc["id"], force=True,
                        constraints=_egress_constraints(cur_acc))
                    TP.report_proxy_error(cur_acc["id"],
                                          event_type="proxy_error")
                    if new_proxy:
                        # patch17：重绑成功同步更新 binding 档案
                        cur_acc = store.update_account(cur_acc["id"], proxy=new_proxy,
                                                       binding=new_proxy.get("_binding"))
                        engine._last_http_renew.pop(cur_acc["id"], None)
                        rebound = True
                        log.info("【TetherPool】账号 %s 代理异常已重绑", cur_acc["id"])
            except MuseModelRefusalError as exc:
                # 二开：refusal 单独捕获——清页面后 continue 重发（上方分支已改写 prompt）
                last_exc = exc
                store.mark(cur_acc["id"], True, f"模型只回文本: {str(exc)[:60]}")
                engine.close_current_page()
            except MuseGenerationError as exc:
                last_exc = exc
                store.mark(cur_acc["id"], True, f"任务异常: {str(exc)[:60]}")
                try:
                    engine.reset_thread()
                except Exception:
                    pass
            except Exception as exc:  # noqa: BLE001
                engine.close_current_page()
                last_exc = MuseGenerationError(f"生成失败: {exc}")
            _generation_callback(on_error, str(last_exc) if last_exc else "")
        if last_exc is None:  # 二开兜底：重试耗尽且无异常记录（不应发生，防 raise None）
            raise MuseGenerationError("生成失败：重试次数已用尽")
        raise last_exc
    finally:
        if cur_acc:
            ACCOUNT_POOL.release(cur_acc.get("id"))
        engine.close_current_page()
        _clear_account_proxy()



# ------------------------- 基础接口 -------------------------
@app.get("/healthz")
def healthz():
    return {"status": "ok", "time": int(time.time())}


@app.get("/readyz")
def readyz():
    accs = [a for a in store.list_accounts() if a.get("enabled", True)]
    return {"status": "ready" if accs else "no_account",
            "accounts": len(accs),
            "browser_running": bool(engine.proc and engine.proc.poll() is None)}


@app.get("/v1/models")
def models(_=Depends(auth)):
    return {"object": "list", "data": MODELS}


# ------------------------- 生图 -------------------------
def _image_response(req: ImageRequest, res: dict) -> dict:
    log.info("【生图完成】%.0fKB %s", res["size"] / 1024, res["filename"])
    item = {"revised_prompt": req.prompt, "url": media_url(res["filename"]),
            "size": req.size or "auto", "kind": res["kind"], "bytes": res["size"]}
    if req.response_format == "b64_json":
        fpath = res.get("path") or os.path.join(CFG.media_dir, res["filename"])
        try:
            with open(fpath, "rb") as f:
                item["b64_json"] = base64.b64encode(f.read()).decode()
        except FileNotFoundError as exc:
            # patch13(F13)：媒体被清理/备份后删除 → 410 而非裸 500
            raise HTTPException(410, "媒体文件已过期") from exc
        item.pop("url", None)
    return {"created": int(time.time()), "data": [item]}


def _generic_upload_mime(mime: str) -> bool:
    """multipart 上传常见的笼统类型（交给文件头识别）以及别名写法。"""
    return mime in ("application/octet-stream", "binary/octet-stream") or mime in (
        "image/jpg", "audio/mp3", "audio/x-wav", "audio/wave", "audio/x-m4a",
        "audio/m4a", "audio/x-flac", "audio/x-aac", "video/x-m4v")


def _prepare_reference_images(images: list[str]) -> list[str]:
    """Validate and snapshot all sources before a task is accepted."""
    prepared = []
    total_bytes = 0
    for index, image in enumerate(images):
        try:
            b64, mime = engine._normalize_image(image)
            data = base64.b64decode(b64, validate=True)
        except (RefImageSafetyError, MuseGenerationError, binascii.Error, ValueError) as exc:
            raise HTTPException(400, f"images[{index}]: {exc}") from exc
        total_bytes += len(data)
        if total_bytes > MAX_REFERENCE_TOTAL_BYTES:
            raise HTTPException(
                400, f"参考素材总大小超过 {MAX_REFERENCE_TOTAL_BYTES // (1024 * 1024)} MiB 限制")
        prepared.append(f"data:{mime};base64,{b64}")
    return prepared


def _queue_image(req: ImageRequest, prompt: str, reference_images: list[str],
                 idempotency_key: str | None = None):
    """Opt-in polling avoids reverse-proxy timeouts; no generation is repeated by polling."""
    if idempotency_key and len(idempotency_key) > 256:
        raise HTTPException(400, "Idempotency-Key too long")
    key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest() if idempotency_key else None
    request_hash = hashlib.sha256(json.dumps(
        {"request": req.model_dump(by_alias=True), "references": reference_images},
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with IMAGE_TASK_LOCK:
        tasks = list(store.tasks.values())
        if key_hash:
            for existing in tasks:
                if existing.get("image_request_key") == key_hash:
                    if existing.get("image_request_hash") != request_hash:
                        # patch18：409 detail 带冲突 task_id + patch22q 结构化诊断头（对齐 video 侧）
                        raise HTTPException(409, detail={
                            "error": "idempotency_key_conflict",
                            "message": "Idempotency-Key reused with different request",
                            "existing_task_id": existing["id"],
                            "existing_status": existing.get("status"),
                            "existing_created_at": existing.get("created_at"),
                            "existing_request_hash": (existing.get("image_request_hash") or "")[:16],
                            "incoming_request_hash": request_hash[:16],
                            "conflict_at": int(time.time()),
                        }, headers={"X-Idempotency-Conflict-Task": existing["id"]})
                    # patch20(F1)：failed 任务不算幂等命中（对齐 patch14 video
                    # 侧双保险）——同 key 重放允许重建。
                    if existing.get("status") == "failed":
                        continue
                    return JSONResponse(status_code=202, content={
                        "id": existing["id"], "task_id": existing["id"],
                        "object": "image.task", "status": existing["status"],
                        "progress": existing.get("progress", 0),
                        "created_at": existing["created_at"]})
        # ponytail: one Chromium worker; cap admission instead of adding a broker.
        if sum(t.get("kind") == "image" and t.get("status") in ("queued", "processing")
               for t in tasks) >= 8:
            raise HTTPException(429, "Image queue is full; retry later")
        task = store.create_task("image", req.prompt)
        tid = task["id"]
        store.update_task(tid, api_prompt=prompt, size=req.size,
                          response_format=req.response_format, progress=0, queue_elapsed=0.0,
                          image_request_key=key_hash, image_request_hash=request_hash)

    def worker():
        timing = TaskTiming(store, tid)
        def _on_acquired():
            timing.acquired()
            store.update_task(tid, progress=5)
        try:
            res, acc_id = _run_generation(
                prompt, "image", req.timeout or CFG.image_timeout,
                reference_images=reference_images,
                on_progress=lambda p: store.update_task(tid, progress=p),
                on_acquired=_on_acquired, on_account=timing.account,
                on_queue=timing.queued, on_error=timing.error)
            # Store only media metadata, not large base64 payloads or reference credentials.
            store.update_task(tid, status="completed", progress=100, account=acc_id,
                              **timing.finish(),
                              url=media_url(res["filename"]),
                              result={**{k: res[k] for k in ("filename", "size", "kind")},
                                      "url": media_url(res["filename"])})
            log.info("【生图完成】task=%s 账号=%s %.0fKB %.1fs", tid, acc_id,
                     res["size"] / 1024, timing.total_elapsed)
        except Exception as exc:  # noqa: BLE001
            store.update_task(tid, status="failed", error=_err_detail(exc),
                              error_code=getattr(exc, "code", "") or "generation_failed",
                              **timing.finish())
            # patch20(F1)：失败即清 key（对齐 patch14 video 侧）——失败任务
            # 不再挡住同 key 同体重放重建。
            if task.get("image_request_key"):
                store.update_task(tid, image_request_key=None, image_request_hash=None)
            log.warning("【生图失败】task=%s %.1fs %s", tid,
                        timing.total_elapsed, str(exc)[:120])

    threading.Thread(target=worker, daemon=True).start()
    return JSONResponse(status_code=202, content={
        "id": tid, "task_id": tid, "object": "image.task", "status": "queued",
        "progress": 0, "created_at": task["created_at"]})


@app.post("/v1/images/tasks")
def create_image_task(req: ImageRequest,
                      idempotency_key: str | None = Header(default=None), _=Depends(auth)):
    refs = _prepare_reference_images(_image_references(req))
    return _queue_image(req, build_image_prompt(req, _reference_kinds(refs)), refs, idempotency_key)


@app.get("/v1/images/tasks/{task_id}")
def get_image_task(task_id: str, _=Depends(auth)):
    task = store.get_task(task_id)
    if not task or task.get("kind") != "image":
        raise HTTPException(404, "image task 不存在")
    out = dict(task)
    if out.get("status") == "completed":
        req = ImageRequest(prompt=out["prompt"], size=out.get("size"),
                           response_format=out.get("response_format") or "url")
        out.update(_image_response(req, out["result"]))
    return out


@app.post("/v1/images/generations")
async def images_generations(req: ImageRequest, _=Depends(auth)):
    ref_images = await asyncio.to_thread(
        _prepare_reference_images, _image_references(req))

    prompt = build_image_prompt(req, _reference_kinds(ref_images))
    timeout = req.timeout or CFG.image_timeout
    if req.async_:
        return _queue_image(req, prompt, ref_images)
    try:
        res, _acc = await asyncio.to_thread(
            _run_generation, prompt, "image", timeout, reference_images=ref_images)
    except MuseAuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    except MuseGenerationError as exc:
        raise HTTPException(502, _err_detail(exc)) from exc
    return _image_response(req, res)


@app.post("/v1/images/edits")
async def images_edits(request: Request, _=Depends(auth)):
    """OpenAI 兼容的图生图/图像编辑接口，兼容 multipart/form-data 与 application/json。"""
    content_type = request.headers.get("content-type", "").lower()
    prompt = ""
    model = "muse-image"
    size = None
    aspect_ratio = None
    response_format = "url"
    timeout = None
    ref_image_data: list[str] = []
    async_mode = False

    if "multipart/form-data" in content_type:
        form = await request.form(
            max_files=MAX_REFERENCE_IMAGES, max_fields=20)
        async_mode = form.get("async", False)
        prompt = form.get("prompt") or ""
        model = form.get("model") or "muse-image"
        size = form.get("size")
        aspect_ratio = form.get("aspect_ratio")
        response_format = form.get("response_format") or "url"
        timeout_val = form.get("timeout")
        if timeout_val:
            try:
                timeout = int(timeout_val)
            except ValueError:
                pass
        # image 可重复提交；video / audio / file 同样可重复，按字段顺序排在 image 之后
        img_fields = [(name, value) for name in ("image", "image[]", "video", "audio", "file")
                      for value in form.getlist(name)]
        if len(img_fields) > MAX_REFERENCE_IMAGES:
            raise HTTPException(400, f"参考素材最多 {MAX_REFERENCE_IMAGES} 个（当前 {len(img_fields)} 个）")
        total_upload_bytes = 0
        mib = 1024 * 1024
        for index, (name, img_field) in enumerate(img_fields):
            if img_field and hasattr(img_field, "read"):
                ref_mime = (getattr(img_field, "content_type", "") or "").split(";", 1)[0].strip().lower()
                if ref_mime and ref_mime not in REFERENCE_MIME_EXT and not _generic_upload_mime(ref_mime):
                    raise HTTPException(400, f"{name}[{index}] 文件类型不受支持（{ref_mime}）")
                content = await img_field.read(MAX_REFERENCE_IMAGE_BYTES + 1)
                if len(content) > MAX_REFERENCE_IMAGE_BYTES:
                    raise HTTPException(400, f"{name}[{index}] 超过 {MAX_REFERENCE_IMAGE_BYTES // mib} MiB 限制")
                total_upload_bytes += len(content)
                if total_upload_bytes > MAX_REFERENCE_TOTAL_BYTES:
                    raise HTTPException(400, f"参考素材总大小超过 {MAX_REFERENCE_TOTAL_BYTES // mib} MiB 限制")
                ref_mime = ref_mime or "application/octet-stream"
                ref_image_data.append(
                    f"data:{ref_mime};base64,{base64.b64encode(content).decode('ascii')}")
            elif isinstance(img_field, str) and img_field.strip():
                ref_image_data.append(img_field.strip())
            else:
                raise HTTPException(400, f"{name}[{index}] 不是有效素材引用")
    else:
        body = await request.json()
        async_mode = body.get("async", False)
        prompt = body.get("prompt") or ""
        model = body.get("model") or "muse-image"
        size = body.get("size")
        aspect_ratio = body.get("aspect_ratio")
        response_format = body.get("response_format") or "url"
        timeout = body.get("timeout")
        raw_images = body.get("images")
        if raw_images is not None and not isinstance(raw_images, list):
            raise HTTPException(400, "images 必须是数组")
        legacy = [body.get("reference_image"), body.get("image_url"), body.get("image")]
        ref_image_data = _reference_list(raw_images, legacy, "images", {
            name: body.get(name) for name in ("videos", "audios", "files")})

    if not prompt:
        prompt = "参考此图片并进行生图创作"

    req_obj = ImageRequest(
        prompt=prompt,
        model=model,
        size=size,
        aspect_ratio=aspect_ratio,
        response_format=response_format,
        timeout=timeout,
        images=ref_image_data,
        **{"async": async_mode}
    )
    ref_image_data = await asyncio.to_thread(
        _prepare_reference_images, ref_image_data)
    full_prompt = build_image_prompt(req_obj, _reference_kinds(ref_image_data))
    gen_timeout = timeout or CFG.image_timeout
    if req_obj.async_:
        return _queue_image(req_obj, full_prompt, ref_image_data)
    try:
        res, _acc = await asyncio.to_thread(
            _run_generation, full_prompt, "image", gen_timeout,
            reference_images=ref_image_data)
    except MuseAuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    except MuseGenerationError as exc:
        raise HTTPException(502, _err_detail(exc)) from exc

    return _image_response(req_obj, res)


# ------------------------- 生视频（异步任务） -------------------------
@app.post("/v1/videos")
@app.post("/v1/videos/generations")
async def create_video(req: VideoRequest, request: Request, _=Depends(auth),
                       idempotency_key: str | None = Header(default=None)):
    ref_images = await asyncio.to_thread(
        _prepare_reference_images, _video_references(req))

    prompt = build_video_prompt(req, _reference_kinds(ref_images))
    timeout = req.timeout or CFG.video_timeout
    # patch11：Idempotency-Key 幂等（对齐 image 侧 _queue_image 先例）——
    # 同 key 同请求体 → 复用既有 task（轮询/重试不重复计费）；同 key 不同体 → 409。
    if idempotency_key and len(idempotency_key) > 256:
        raise HTTPException(400, "Idempotency-Key too long")
    # patch13(F18)：hash 计算在锁外（image 侧先例）——大参考素材的 sha256
    # 不串行化其他建任务请求。
    key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest() if idempotency_key else None
    request_hash = hashlib.sha256(json.dumps(
        {"request": req.model_dump(), "references": ref_images},
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with VIDEO_TASK_LOCK:  # patch12：check+create 原子化，堵并发同 key 双建
        # patch13(F4)：幂等查独立索引（不被 task prune 蒸发），miss 再查 task
        # 记录兜底；同 key 不同体 409。
        if key_hash:
            hit = IDEMPOTENCY_VIDEOS.get(key_hash)
            # patch14(F4)：TTL 生效——超 24h 的索引项失效（防内存慢漏 +
            # 防陈旧 hash 误命中）；IDEMPOTENCY_TTL 定义于模块级。
            if hit and time.time() - hit.get("ts", 0) > IDEMPOTENCY_TTL:
                IDEMPOTENCY_VIDEOS.pop(key_hash, None)
                _idem_write()
                hit = None
            if hit and hit.get("request_hash") == request_hash:
                existing = store.get_task(hit["task_id"])
                if existing:
                    return {"id": existing["id"], "task_id": existing["id"],
                            "object": "video.task", "status": existing["status"],
                            "progress": existing.get("progress", 0),
                            "created_at": existing["created_at"]}
                del IDEMPOTENCY_VIDEOS[key_hash]  # 指向的任务已不在，索引项失效
                _idem_write()
        if key_hash:
            # patch21b：task 兜底扫描只在带 Idempotency-Key 时进行——key_hash=None
            # 时会误匹配一切无 key 任务（含 image task）并错报 409
            # （test_async_images 首个 /v1/videos POST 即触发，合并时引入）。
            for existing in list(store.tasks.values()):
                if existing.get("video_request_key") == key_hash:
                    if existing.get("video_request_hash") != request_hash:
                        # patch18：409 detail 带冲突 task_id，排障不必进容器翻库。
                        # patch22q：结构化诊断头——OpenPet 不必解析 detail 文本，
                        # 双方摘要/时间/已有任务终态全部可机读（不覆盖旧记录）。
                        raise HTTPException(409, detail={
                            "error": "idempotency_key_conflict",
                            "message": "Idempotency-Key reused with different request",
                            "existing_task_id": existing["id"],
                            "existing_status": existing.get("status"),
                            "existing_created_at": existing.get("created_at"),
                            "existing_request_hash": (existing.get("video_request_hash") or "")[:16],
                            "incoming_request_hash": request_hash[:16],
                            "conflict_at": int(time.time()),
                            "recovery_hint": "GET /admin/idempotency?scope=videos&key_hash=%s 查询原任务终态（只读）" % key_hash[:16],
                        }, headers={"X-Idempotency-Conflict-Task": existing["id"],
                                    "X-Idempotency-Existing-Hash": (existing.get("video_request_hash") or "")[:16],
                                    "X-Idempotency-Incoming-Hash": request_hash[:16]})
                    # patch14(F4)：failed 任务不算幂等命中——同 key 重放允许重建
                    # （失败后清索引可能因竞态漏掉，这里双保险）。
                    # patch22：仅限 safe_to_resubmit=True（提交前失败，可证上游
                    # 无任务）——提交后失败/结果未知一律返回原任务，禁止自动重放。
                    if existing.get("status") == "failed":
                        if existing.get("safe_to_resubmit") is False:
                            return {"id": existing["id"], "task_id": existing["id"],
                                    "object": "video.task", "status": existing["status"],
                                    "progress": existing.get("progress", 0),
                                    "created_at": existing["created_at"]}
                        continue
                    return {"id": existing["id"], "task_id": existing["id"],
                            "object": "video.task", "status": existing["status"],
                            "progress": existing.get("progress", 0),
                            "created_at": existing["created_at"]}
        # patch13(F3)：video 是最贵上游资源，与 image 侧同款准入上限。
        if sum(t.get("kind") == "video" and t.get("status") in ("queued", "processing")
               for t in store.tasks.values()) >= 8:
            raise HTTPException(429, "Video queue is full; retry later")
        task = store.create_task("video", req.prompt)
        store.update_task(task["id"], api_prompt=prompt, queue_elapsed=0.0,
                          video_request_key=key_hash, video_request_hash=request_hash)
        if key_hash:
            IDEMPOTENCY_VIDEOS[key_hash] = {
                "task_id": task["id"], "request_hash": request_hash,
                "ts": time.time()}
            _idem_write()

    store.update_task(task["id"], progress=10)

    def worker():
        timing = TaskTiming(store, task["id"])
        # patch11：attempt 审计——每个 generation attempt 对应一次上游付费生成；
        # 轮询/查询不产生 attempt。refusal 重试（换 prompt 重发）与快速失败都入账。
        attempts = {"n": 0, "events": []}
        acc_id = None  # patch17c：失败路径快照引用 acc_id——取得账号前抛异常
        # （锁超时/排队失败）时 except 块不能 UnboundLocalError 掩盖真实异常

        def _on_acquired():
            timing.acquired()
            attempts["n"] += 1
            attempts["events"].append(
                {"attempt": attempts["n"], "event": "engine_call",
                 "at": round(timing.total_elapsed, 1)})
            store.update_task(task["id"], progress=15,
                              generation_attempts=attempts["n"],
                              attempt_events=list(attempts["events"]))
        try:
            def prog_cb(p):
                store.update_task(task["id"], progress=p)
            res, acc_id = _run_generation(prompt, "video", timeout, on_progress=prog_cb,
                                          reference_images=ref_images, on_acquired=_on_acquired,
                                          on_account=timing.account,
                                          on_queue=timing.queued, on_error=timing.error,
                                          on_stage=lambda s: store.update_task(
                                              task["id"], stage=s))
            vurl = media_url(res["filename"])
            # patch16：token 关联账——终态记录该账号当时最近一次 quota 快照
            # （weekly_used_pct / extra_left），跨任务差分即得消耗趋势。
            _acc = store.get_account(acc_id) if acc_id else None
            _q = (_acc or {}).get("quota") or {}
            store.update_task(task["id"], status="completed", progress=100, account=acc_id,
                              **timing.finish(),
                              stage="completed",
                              url=vurl,
                              video={"url": vurl},
                              quota_snapshot={k: _q.get(k) for k in
                                              ("plan", "weekly_used_pct", "extra_left",
                                               "extra_used_pct", "checked_at")} if _q else None,
                              result={"url": vurl,
                                      "filename": res["filename"],
                                      "bytes": res["size"], "kind": res["kind"]})
            log.info("【生视频完成】task=%s 账号=%s %.1fMB %.0fs", task["id"], acc_id,
                     res["size"] / 1048576, timing.total_elapsed)
        except Exception as exc:  # noqa: BLE001
            attempts["events"].append(
                {"attempt": attempts["n"], "event": "failed",
                 "code": getattr(exc, "code", "") or "generation_failed",
                 "at": round(timing.total_elapsed, 1)})
            # retryable：发送层失败（换号重试有意义）；附件失效/上游不可用不安全重试
            retryable = getattr(exc, "code", "") not in (
                "upstream_attachment_broken", "native_video_unavailable")
            # patch22：失败环节与提交安全性——stage < submit 证明上游未收到
            # 任何生成请求（重发零计费风险）；stage >= submit 的失败上游结果
            # 未知，generation_accepted 必须记 unknown，禁止调用方自动重放。
            fail_stage = getattr(exc, "stage", "") or (task.get("stage") or "queued")
            exc_retryable = getattr(exc, "retryable", None)
            if exc_retryable is not None:
                retryable = bool(exc_retryable) and retryable
            safe_to_resubmit = _stage_before(fail_stage, "submit")
            _acc = store.get_account(acc_id) if acc_id else None
            _q = (_acc or {}).get("quota") or {}
            store.update_task(task["id"], status="failed",
                              **timing.finish(),
                              error=_err_detail(exc),
                              error_code=getattr(exc, "code", "") or "generation_failed",
                              stage=fail_stage,
                              generation_accepted=("unknown" if not safe_to_resubmit
                                                   else "no"),
                              safe_to_resubmit=safe_to_resubmit,
                              generation_attempts=attempts["n"],
                              attempt_events=list(attempts["events"]),
                              retryable=retryable,
                              quota_snapshot={k: _q.get(k) for k in
                                              ("plan", "weekly_used_pct", "extra_left",
                                               "extra_used_pct", "checked_at")} if _q else None)  # patch16
            # patch14(F4)+patch22：失败清幂等索引只对「提交前失败」生效（
            # safe_to_resubmit=True）——提交后失败的上游结果未知，同 key 重放
            # 不得静默重建；调用方拿到带 stage/safe_to_resubmit 的失败任务自行决策。
            vh = task.get("video_request_key")
            if vh and safe_to_resubmit:
                with VIDEO_TASK_LOCK:
                    idx = IDEMPOTENCY_VIDEOS.get(vh)
                    if idx and idx.get("task_id") == task["id"]:
                        IDEMPOTENCY_VIDEOS.pop(vh, None)
                        _idem_write()
            log.warning("【生视频失败】task=%s %.1fs %s", task["id"],
                        timing.total_elapsed, str(exc)[:120])

    threading.Thread(target=worker, daemon=True).start()
    return {"id": task["id"], "task_id": task["id"], "object": "video.task", "status": "queued",
            "progress": 10, "created_at": task["created_at"]}


@app.get("/v1/videos/{task_id}")
@app.get("/v1/videos/generations/{task_id}")
def get_video(task_id: str, _=Depends(auth)):
    t = store.get_task(task_id)
    if not t or t.get("kind") != "video":  # patch13(F10)：kind 过滤对齐 image 侧
        raise HTTPException(404, "task 不存在")
    out = dict(t)
    status = out.get("status")
    if status in ("succeeded", "success", "done"):
        out["status"] = "completed"
    if out.get("status") == "completed":
        out["progress"] = 100
    elif out.get("status") == "processing":
        # Progress reports observed milestones only; Muse exposes no percentage.
        out["progress"] = max(out.get("progress", 0) or 0, 15)
    vurl = out.get("url")
    if not vurl and isinstance(out.get("result"), dict):
        vurl = out["result"].get("url")
    if vurl:
        out["url"] = vurl
        if "video" not in out:
            out["video"] = {"url": vurl}
    return out


# ------------------------- patch22q：幂等只读核验 -------------------------
def _safe_task_view(t: dict) -> dict:
    """任务的安全对外视图：绝不回显 prompt/api_prompt（内容侧敏感），
    只给幂等恢复需要的结构字段。"""
    r = t.get("result") or {}
    return {
        "task_id": t["id"], "object": "video.task", "kind": t.get("kind"),
        "status": t.get("status"), "progress": t.get("progress", 0),
        "created_at": t.get("created_at"), "updated_at": t.get("updated_at"),
        "key_hash": (t.get("video_request_key") or "")[:16] or None,
        "request_hash": (t.get("video_request_hash") or "")[:16] or None,
        "safe_to_resubmit": t.get("safe_to_resubmit"),
        "error_code": t.get("error_code"),
        "result": ({"filename": r.get("filename"), "bytes": r.get("bytes"),
                    "kind": r.get("kind"), "url": r.get("url")} if r else None),
    }


@app.get("/admin/idempotency")
def lookup_idempotency(key_hash: str = "", request_hash: str = "", media: str = "",
                       _=Depends(auth)):
    """patch22q：幂等记录只读核验（OpenPet 恢复路径，不触发任何生成）。

    三种查询维度（可单用可组合，AND 语义）：
      key_hash=<sha256 前 16 位以上前缀>  — 幂等键查询（前缀匹配防日志泄漏全键）
      request_hash=<sha256 前 16 位以上>  — 规范化请求体摘要查询
      media=<成品文件名>                  — 按归档索引反查（含已被 prune 的任务）
    返回 idem 索引命中 + 任务库命中 + 归档索引命中三段，各段可为空——
    「空」即「现存记录中没有」，不是「从未创建」。
    """
    if not any([key_hash, request_hash, media]):
        raise HTTPException(400, "需要 key_hash / request_hash / media 至少一个查询维度")
    out = {"scope": "videos", "idem_index": [], "tasks": [], "archive": []}
    if key_hash:
        if len(key_hash) < 16:
            raise HTTPException(400, "key_hash 前缀至少 16 位（防全键泄漏，只收前缀查询）")
        for kh, v in IDEMPOTENCY_VIDEOS.items():
            if kh.startswith(key_hash):
                out["idem_index"].append({"key_hash": kh[:16], "task_id": v["task_id"],
                                          "request_hash": (v.get("request_hash") or "")[:16],
                                          "ts": int(v.get("ts") or 0)})
        out["tasks"].extend(_safe_task_view(t) for t in store.tasks.values()
                            if (t.get("video_request_key") or "").startswith(key_hash))
    if request_hash:
        if len(request_hash) < 16:
            raise HTTPException(400, "request_hash 前缀至少 16 位")
        out["tasks"].extend(_safe_task_view(t) for t in store.tasks.values()
                            if (t.get("video_request_hash") or "").startswith(request_hash))
    if media:
        out["archive"].extend(store.lookup_archive_by_media(media))
        out["tasks"].extend(_safe_task_view(t) for t in store.tasks.values()
                            if ((t.get("result") or {}).get("filename") == media
                                or ((t.get("result") or {}).get("url") or "").endswith(media)))
    # 去重（多维度可能命中同一任务）
    seen, uniq = set(), []
    for t in out["tasks"]:
        if t["task_id"] not in seen:
            seen.add(t["task_id"])
            uniq.append(t)
    out["tasks"] = uniq
    return out


@app.get("/v1/videos/{task_id}/content")
@app.get("/v1/videos/generations/{task_id}/content")
def get_video_content(task_id: str, _=Depends(auth)):
    """patch22j：OpenPet 契约的成品字节直取端点。等价于先 GET /v1/videos/{id}
    再 GET url，但一步到位且只认已完成的视频任务——未完成/失败/无成品一律
    native_video_unavailable，绝不 substitute。"""
    t = store.get_task(task_id)
    if not t or t.get("kind") != "video":
        raise HTTPException(404, "task 不存在")
    if t.get("status") not in ("completed", "succeeded", "success", "done"):
        raise HTTPException(409, "任务尚未完成，暂无成品可下载")
    vurl = t.get("url") or (t.get("result") or {}).get("url") \
        or (t.get("video") or {}).get("url") or ""
    name = vurl.rstrip("/").rsplit("/", 1)[-1]
    if not name or "/" in name or "\\" in name or ".." in name:
        raise HTTPException(503, "native_video_unavailable")
    p = os.path.join(CFG.media_dir, name)
    if not os.path.isfile(p):
        # patch22m：同 get_media——备份删除本地副本后回源远端，不落盘直接返回
        data = MEDIA_BACKUPER.fetch_remote(name) if mediabak.enabled() else None
        if not data:
            raise HTTPException(503, "native_video_unavailable")
        return Response(content=data, media_type="video/mp4")
    return FileResponse(p, media_type="video/mp4")


# ------------------------- 对话（OpenAI 兼容） -------------------------
def _sse(obj: dict) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def _chat_chunk(cid: str, created: int, model: str, delta: dict,
                finish: str | None = None) -> dict:
    return {"id": cid, "object": "chat.completion.chunk", "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def _want_usage(stream_options) -> bool:
    """下游（LangChain、部分 SDK / 智能体框架）会带
    `stream_options.include_usage=true`，要求在 [DONE] 之前补一个
    `choices: []` + `usage` 的分片。不发的话少数框架会一直等 usage 而卡住。"""
    if isinstance(stream_options, dict):
        return bool(stream_options.get("include_usage"))
    return False


def _pace_text(text: str, chunk_size: int = 2, delay: float = 0.012):
    """把大块文本平滑切分为仿原生 LLM 打字机的微流式 Token。
    若传入文本本身微小（<= 3 字），直接秒级放行，零延迟；
    若传入文本是大块（如 DOM 批量刷新），按每 chunk_size 字间隔 delay 平滑输出。
    """
    if not text:
        return
    if len(text) <= 3 or delay <= 0:
        yield text
        return
    for i in range(0, len(text), chunk_size):
        yield text[i:i + chunk_size]
        if i + chunk_size < len(text):
            time.sleep(delay)


_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest, _=Depends(auth)):
    """OpenAI 兼容的对话接口 —— 各类智能体客户端都能接。

    说明：muse.ai 网页是**自动路由**的 agent，界面上没有模型选择器，所有请求
    都会落到同一个网页助手（自称 Koda，底层是 Muse 系列语言模型）。所以
    `model` 字段只用于兼容下游，不影响路由结果。

    请求带 `tools` 时，会注入【工具调用协议】并把模型输出的 JSON 解析回
    `tool_calls`（muse.ai 没有原生 function calling，这层属于协议适配）。

    对话与生图/生视频共用同一个浏览器实例，靠 `GEN_LOCK` 串行。
    """
    # ---- 二开：标准多模态带图提示词（vision）+ 显式生图/生视频模型路由 ----
    # P3 根修：stream 拒绝提前到图片预取之前（避免白做公网图下载）；
    # 带图时 [图片] 占位剥离（真实附件已在路上，占位文本只产生语义噪声）。
    chat_images = _chat_images(req.messages)
    model_probe = resolve_model(req.model, default="muse-spark")
    _forced_kind = _mentions_image_or_video_model(model_probe)
    if (chat_images or _forced_kind) and req.stream:
        raise HTTPException(400, "生图/生视频不支持流式，请 stream=false 或改用 /v1/images、/v1/videos")
    if chat_images:
        prompt = build_chat_prompt(req.messages, strip_images=True) \
            or (req.prompt or "").strip()
        try:
            chat_ref_images = await asyncio.to_thread(_prepare_reference_images, chat_images)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"chat 图片解析失败: {exc}") from exc

    prompt = build_chat_prompt(req.messages, strip_images=bool(chat_images)) \
        or (req.prompt or "").strip()
    if not prompt:
        raise HTTPException(400, "messages 为空")

    # 工具调用协议默认不注入 —— 实测 muse.ai 的助手会明确拒绝输出"伪工具调用"，
    # 注入反而污染正常回答；详见 config.py 的 tool_protocol 注释。
    tool_note = build_tools_prompt(req.tools) if CFG.tool_protocol else ""
    if tool_note:
        # 放在末尾：开头是 agent 自己的 system 提示，夹在中间容易被忽略；
        # 紧贴用户消息之前，模型对末尾指令的遵守度明显更高。
        prompt = prompt + "\n\n" + tool_note

    model = model_probe  # 顶部已解析（分流判定复用，避免二次解析）
    timeout = int(req.timeout or CFG.chat_timeout)

    # ---- 二开：带图提示词 或 显式生图/生视频模型 → 走生成路径 ----
    if chat_images or _forced_kind:
        kind = _forced_kind or "image"          # 有图未指定模型 → agent 自动路由，按 image 提交
        gen_timeout = int(req.timeout or (CFG.video_timeout if kind == "video" else CFG.image_timeout))
        try:
            res, _acc_id = await asyncio.to_thread(
                _run_generation, prompt, kind, gen_timeout,
                reference_images=chat_ref_images if chat_images else None)
        except MuseAuthError as exc:
            raise HTTPException(401, str(exc)) from exc
        except MuseGenerationError as exc:
            raise HTTPException(502, _err_detail(exc)) from exc
        media_link = media_url(res["filename"])
        text_out = (f"{media_link}\n\n（{kind}已生成：{res['filename']}，"
                    f"{res['size']} bytes；如客户端不能内联预览，请直接访问上方链接下载）")
        return {"id": "chatcmpl-" + uuid.uuid4().hex[:24], "object": "chat.completion",
                "created": int(time.time()), "model": model,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": text_out}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}

    acc = store.pick_account(rotate=True)
    if not acc:
        raise HTTPException(400, "没有可用账号，请先在管理页面导入 cookie")

    cid = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())
    acc_id = acc["id"]
    cookies = acc["cookies"]
    expires = acc.get("cookies_exp")

    def tool_call_deltas(calls: list[dict]) -> list[list[dict]]:
        """按 OpenAI 习惯分两片发：先 id/name，再 arguments 全文。"""
        head = [{"index": i, "id": c["id"], "type": "function",
                 "function": {"name": c["function"]["name"], "arguments": ""}}
                for i, c in enumerate(calls)]
        body = [{"index": i, "function": {"arguments": c["function"]["arguments"]}}
                for i, c in enumerate(calls)]
        return [head, body]

    if req.stream:
        def sync_stream():
            try:
                # 握手建立瞬间立即发送 role: assistant 首包，让下游客户端秒级捕获光标
                yield _sse(_chat_chunk(cid, created, model, {"role": "assistant"}))

                stream_gen = safe_chat_stream(cookies, prompt, expires, timeout, account_id=acc_id)
                if not tool_note:
                    for chunk in stream_gen:
                        for piece in _pace_text(chunk):
                            yield _sse(_chat_chunk(cid, created, model, {"content": piece}))
                    yield _sse(_chat_chunk(cid, created, model, {}, finish="stop"))
                else:
                    buf, mode = "", None
                    for chunk in stream_gen:
                        if mode == "text":
                            for piece in _pace_text(chunk):
                                yield _sse(_chat_chunk(cid, created, model, {"content": piece}))
                            continue
                        buf += chunk
                        if mode is None:
                            head = buf.lstrip()
                            if len(head) >= 2:
                                if head[0] in "{[" or head.startswith("```"):
                                    mode = "maybe_tool"
                                else:
                                    mode = "text"
                                    for piece in _pace_text(buf):
                                        yield _sse(_chat_chunk(cid, created, model, {"content": piece}))
                                    buf = ""
                    if mode == "text":
                        yield _sse(_chat_chunk(cid, created, model, {}, finish="stop"))
                    else:
                        calls, rest = parse_tool_calls(buf)
                        if calls:
                            if rest:
                                for piece in _pace_text(rest):
                                    yield _sse(_chat_chunk(cid, created, model, {"content": piece}))
                            for piece in tool_call_deltas(calls):
                                yield _sse(_chat_chunk(cid, created, model, {"tool_calls": piece}))
                            yield _sse(_chat_chunk(cid, created, model, {}, finish="tool_calls"))
                        else:
                            if buf:
                                for piece in _pace_text(buf):
                                    yield _sse(_chat_chunk(cid, created, model, {"content": piece}))
                            yield _sse(_chat_chunk(cid, created, model, {}, finish="stop"))
                if _want_usage(req.stream_options):
                    yield _sse({"id": cid, "object": "chat.completion.chunk",
                                "created": created, "model": model,
                                "choices": [],
                                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})
                yield "data: [DONE]\n\n"
                store.mark(acc_id, True, "")
            except MuseAuthError as exc:
                store.mark(acc_id, False, str(exc))
                yield _sse({"error": {"message": str(exc), "type": "auth_error", "code": 401}})
            except MuseGenerationError as exc:
                store.mark(acc_id, True, f"助手超时: {str(exc)[:60]}")
                try:
                    engine.reset_thread()
                except Exception:
                    pass
                yield _sse({"error": {"message": str(exc), "type": "server_error", "code": 502}})
            except Exception as exc:
                yield _sse({"error": {"message": f"内部错误: {exc}", "type": "server_error", "code": 500}})
        return StreamingResponse(sync_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)

    def run() -> str:
        return "".join(safe_chat_stream(cookies, prompt, expires, timeout, account_id=acc_id))

    try:
        text = await asyncio.to_thread(run)
    except MuseAuthError as exc:
        raise HTTPException(401, str(exc))
    except MuseGenerationError as exc:
        raise HTTPException(502, _err_detail(exc))

    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    if tool_note:
        calls, rest = parse_tool_calls(text)
        if calls:
            return {"id": cid, "object": "chat.completion", "created": created,
                    "model": model,
                    "choices": [{"index": 0, "finish_reason": "tool_calls",
                                 "message": {"role": "assistant",
                                             "content": rest or None,
                                             "tool_calls": calls}}],
                    "usage": usage}
    return {"id": cid, "object": "chat.completion", "created": created,
            "model": model,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": usage}


def _responses_messages(req: ResponsesRequest) -> list[ChatMessage]:
    """把 Responses API 的 instructions / input 归一成 messages。"""
    msgs: list[ChatMessage] = []
    if req.instructions:
        msgs.append(ChatMessage(role="system", content=req.instructions))
    inp = req.input
    if isinstance(inp, str):
        msgs.append(ChatMessage(role="user", content=inp))
        return msgs
    if isinstance(inp, list):
        for item in inp:
            if isinstance(item, str):
                msgs.append(ChatMessage(role="user", content=item))
                continue
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if item.get("role"):                      # message 形态
                msgs.append(ChatMessage(role=item["role"],
                                        content=item.get("content")))
            elif itype == "input_text":
                msgs.append(ChatMessage(role="user", content=item.get("text")))
            elif itype == "function_call_output":
                msgs.append(ChatMessage(role="user", content="【工具执行结果】\n"
                                        + str(item.get("output") or "")))
            elif itype == "function_call":
                msgs.append(ChatMessage(role="assistant", content="【请求调用工具】"
                                        + str(item.get("name") or "")))
    return msgs


def _sse_event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/v1/responses")
async def responses_api(req: ResponsesRequest, _=Depends(auth)):
    """OpenAI Responses API —— 新版 Codex 默认走这个端点。

    只实现 Codex 实际用到的子集：文本进 → 文本出（含流式事件）。
    muse.ai 不会返回结构化的 tool_calls，关于工具调用的边界见 README。
    """
    prompt = build_chat_prompt(_responses_messages(req))
    if not prompt:
        raise HTTPException(400, "input 为空")

    model = resolve_model(req.model, default="muse-spark")
    timeout = int(req.timeout or CFG.chat_timeout)
    acc = store.pick_account(rotate=True)
    if not acc:
        raise HTTPException(400, "没有可用账号，请先在管理页面导入 cookie")

    rid = "resp_" + uuid.uuid4().hex[:24]
    mid = "msg_" + uuid.uuid4().hex[:24]
    created = int(time.time())
    acc_id, cookies, expires = acc["id"], acc["cookies"], acc.get("cookies_exp")

    def envelope(status: str, text: str = "") -> dict:
        done = status == "completed"
        return {"id": rid, "object": "response", "created_at": created,
                "status": status, "model": model,
                "output": [{"type": "message", "id": mid, "role": "assistant",
                            "status": "completed" if done else "in_progress",
                            "content": ([{"type": "output_text", "text": text,
                                          "annotations": []}] if text else [])}],
                "usage": {"input_tokens": 0, "output_tokens": 0,
                          "total_tokens": 0}}

    if req.stream:
        def sync_stream():
            try:
                yield _sse_event("response.created", {
                    "type": "response.created",
                    "response": envelope("in_progress")})
                yield _sse_event("response.output_item.added", {
                    "type": "response.output_item.added", "output_index": 0,
                    "item": {"id": mid, "type": "message", "role": "assistant",
                             "status": "in_progress", "content": []}})
                yield _sse_event("response.content_part.added", {
                    "type": "response.content_part.added", "item_id": mid,
                    "output_index": 0, "content_index": 0,
                    "part": {"type": "output_text", "text": "",
                             "annotations": []}})
                full = ""
                for chunk in safe_chat_stream(cookies, prompt, expires, timeout, account_id=acc_id):
                    full += chunk
                    yield _sse_event("response.output_text.delta", {
                        "type": "response.output_text.delta", "item_id": mid,
                        "output_index": 0, "content_index": 0, "delta": chunk})
                yield _sse_event("response.output_text.done", {
                    "type": "response.output_text.done", "item_id": mid,
                    "output_index": 0, "content_index": 0, "text": full})
                yield _sse_event("response.output_item.done", {
                    "type": "response.output_item.done", "output_index": 0,
                    "item": {"id": mid, "type": "message", "role": "assistant",
                             "status": "completed",
                             "content": [{"type": "output_text", "text": full,
                                          "annotations": []}]}})
                yield _sse_event("response.completed", {
                    "type": "response.completed",
                    "response": envelope("completed", full)})
            except Exception as exc:  # noqa: BLE001
                log.warning("responses 流式失败: %s", exc)
                yield _sse_event("response.failed", {
                    "type": "response.failed",
                    "response": envelope("failed")})
        return StreamingResponse(sync_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)

    def run() -> str:
        return "".join(safe_chat_stream(cookies, prompt, expires, timeout, account_id=acc_id))

    try:
        text = await asyncio.to_thread(run)
    except MuseAuthError as exc:
        raise HTTPException(401, str(exc))
    except MuseGenerationError as exc:
        raise HTTPException(502, _err_detail(exc))

    return envelope("completed", text)


_MEDIA_MIME_EXT = {
    ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
    ".webp": "image/webp", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".gif": "image/gif",
}


def _media_mime(name: str) -> str:
    """patch22m：按扩展名给媒体 Content-Type（回源内存返回时使用）。
    显式表优先——容器内 mimetypes 注册不全（webp 实测落 octet-stream）。"""
    ext = os.path.splitext(name)[1].lower()
    if ext in _MEDIA_MIME_EXT:
        return _MEDIA_MIME_EXT[ext]
    import mimetypes
    mime, _ = mimetypes.guess_type(name)
    return mime or "application/octet-stream"


@app.get("/v1/media/{name}")
def get_media(name: str):
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "非法文件名")
    p = os.path.join(CFG.media_dir, name)
    if not os.path.isfile(p):
        # patch22m：本地副本已被 mediabak 备份删除（delete_local）时回源远端。
        # 不落盘直接流式返回——落盘会让回源副本重新进入「备份→校验→删除」
        # 循环（重复回源 + 误导日志）；回源是低频兜底，内存返回即可。
        data = MEDIA_BACKUPER.fetch_remote(name) if mediabak.enabled() else None
        if not data:
            raise HTTPException(404, "文件不存在")
        mime = _media_mime(name)
        return Response(content=data, media_type=mime)
    return FileResponse(p)


@app.get("/admin/generation-queue")
def get_generation_queue(_=Depends(auth)):
    return QUEUE_POLICY.settings()

@app.put("/admin/generation-queue")
def save_generation_queue(req: QueueSettings, _=Depends(auth)):
    QUEUE_POLICY.save(req)
    return QUEUE_POLICY.settings()

@app.get("/v1/media/{name}/thumbnail")
def get_media_thumbnail(name: str):
    try:
        path = video_thumbnail(CFG.media_dir, os.path.join(CFG.data_dir, "thumbnails"), name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except FileNotFoundError:
        raise HTTPException(404, "文件不存在")
    except ThumbnailUnavailable:
        raise HTTPException(503, "视频缩略图暂不可用")
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


def _clear_media_thumbnail(name: str):
    try:
        clear_thumbnail(os.path.join(CFG.data_dir, "thumbnails"), name)
    except OSError:
        log.warning("Could not remove cached thumbnail for %s", name)


# ------------------------- 管理：总览 -------------------------
@app.get("/admin/status")
def admin_status(_=Depends(auth)):
    base = _public_base()
    return {
        "accounts": store.list_accounts(),
        "stats": store.stats(),
        "generation_queue": QUEUE_POLICY.settings(),
        "tasks": store.list_tasks(20),
        "media_count": len(os.listdir(CFG.media_dir))
        if os.path.isdir(CFG.media_dir) else 0,
        "media_backup": MEDIA_BACKUPER.status() if mediabak.enabled() else None,
        "browser_running": bool(engine.proc and engine.proc.poll() is None),
        "essential_cookies": list(ESSENTIAL_COOKIES),
        "base_url": f"{base}/v1" if base else "",
        "config": {"site": CFG.site_url, "cdp_port": CFG.cdp_port,
                   "image_timeout": CFG.image_timeout,
                   "video_timeout": CFG.video_timeout,
                   "host": CFG.host, "port": CFG.port,
                   "public_base": base},
    }


# ------------------------- 管理：账号池 -------------------------
@app.get("/admin/accounts")
def list_accounts(_=Depends(auth)):
    return {"accounts": store.list_accounts(), "stats": store.stats()}


@app.post("/admin/accounts")
def add_account(req: AccountRequest, _=Depends(auth)):
    proxy = req.proxy.configuration() if req.proxy else None
    added: list[dict] = []
    seen: list[dict] = []          # 本次导入的所有 cookie，用来检查核心项是否齐全

    if req.batch:
        for label, cookies in parse_batch(req.batch):
            acc = store.add_account(cookies, label)
            seen.append(cookies)
            added.append({"id": acc["id"], "label": acc["label"],
                          "cookie_count": len(cookies),
                          "expires_at": acc.get("expires_at")})

    cookies = dict(req.cookies)
    if req.cookie_header:
        cookies.update(parse_cookie_text(req.cookie_header))
    if cookies:
        acc = store.add_account(cookies, req.label, cookies_exp=req.expires)
        seen.append(cookies)
        added.append({"id": acc["id"], "label": acc["label"],
                      "cookie_count": len(cookies),
                      "expires_at": acc.get("expires_at")})

    if not added:
        raise HTTPException(400, "未解析到任何 cookie，请检查格式")

    if proxy:
        for account in added:
            store.update_account(account["id"], proxy=proxy)

    # patch17：加号即绑——无显式代理的账号立即向 TetherPool 申请独立出口，
    # binding 写回账号档案；失败不阻塞加号（心跳 300s 自愈补绑）。
    bind_results = {}
    if not proxy and tetherpool.enabled():
        for account in added:
            aid = account["id"]
            try:
                got = TP.acquire(aid, constraints=_egress_constraints(None, req))
            except Exception:  # noqa: BLE001
                got = None
            if got:
                store.update_account(aid, proxy=got,
                                     binding=got.get("_binding"))
                bind_results[aid] = {"bound": True,
                                     "node": (got.get("_binding") or {}).get("node_id")}
            else:
                bind_results[aid] = {"bound": False,
                                     "note": "控制面暂不可用，心跳将自动补绑"}
    elif not proxy:
        bind_results = {a["id"]: {"bound": False, "note": "TetherPool 未启用"} for a in added}

    # 只要有一个账号把核心 cookie 凑齐就算通过（批量时按整体判断）
    missing = [n for n in ESSENTIAL_COOKIES
               if not any(n in c for c in seen)]
    # patch17e（P3）：bind_results 此前计算后丢弃——调用方看不到每个账号
    # 的绑定结果。随响应返回。
    return {"added": added, "count": len(added),
            "binding": bind_results,
            "essential_missing": missing,
            "warning": (f"缺少核心 cookie：{', '.join(missing)}，该账号可能无法生成"
                        if missing else "")}


# One short-lived receipt per account; credentials are represented only by a hash.
_PROXY_TEST_RECEIPTS: dict[str, dict] = {}
_PROXY_TEST_RECEIPT_TTL = 300


def _proxy_test_identity(proxy: dict, cookies: dict | None) -> str:
    value = json.dumps({"proxy": proxy, "cookies": cookies or {}},
                       sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()


@app.patch("/admin/accounts/{aid}")
def patch_account(aid: str, req: AccountPatch, _=Depends(auth)):
    # 二开 P2 根修：托管分支需要控制面网络调用（最长 20s），照抄 rebind 端点的
    # 「锁内查 busy → 锁外 acquire」结构；持 ACCOUNT_POOL.cond 做网络调用会
    # 阻塞所有 acquire/release/cond.wait 排队任务。
    if "proxy" in req.model_fields_set and req.proxy is not None \
            and req.proxy.server == MANAGED_SCHEME and tetherpool.enabled():
        with ACCOUNT_POOL.cond:
            existing = store.get_account(aid)
            if not existing:
                raise HTTPException(404, "账号不存在")
            if aid in ACCOUNT_POOL.busy_accounts:
                raise HTTPException(409, "账号正在执行任务或检测，请稍后再修改代理")
        got = TP.acquire(aid, constraints=_egress_constraints(existing, req))  # 锁外：控制面调用不占账号池锁
        if not got:
            raise HTTPException(502, "TetherPool 控制面 acquire 失败，请稍后重试")
        changes = {"label": req.label, "enabled": req.enabled,
                   "proxy": got, "binding": got.get("_binding"),
                   "synced_at": 0, "last_keepalive": 0,
                   "checked_at": 0, "proxy_status": {}, "ok": None,
                   "note": "TetherPool 已绑定出口，请重新测试"}
        engine._last_http_renew.pop(aid, None)
        acc = store.update_account(aid, **changes)
        return store.public_account(acc)
    with ACCOUNT_POOL.cond:
        existing = store.get_account(aid)
        if not existing:
            raise HTTPException(404, "账号不存在")
        changes = {"label": req.label, "enabled": req.enabled}
        # patch17：出口约束字段（托管账号重绑时透传）
        for k in ("region", "egress"):
            if getattr(req, k, None) is not None:
                changes[k] = getattr(req, k)
        if req.region_strict is not None:
            changes["region_strict"] = req.region_strict
        if "proxy" in req.model_fields_set:
            if aid in ACCOUNT_POOL.busy_accounts:
                raise HTTPException(409, "账号正在执行任务或检测，请稍后再修改代理")
            proxy = req.proxy.configuration(existing.get("proxy")) if req.proxy else {}
            if proxy != (existing.get("proxy") or {}):
                # patch17e（P3）：托管切自建时当场释放 ip-pool 绑定，否则
                # allocation 一直占用出口容量直到删号（而删号 release 只
                # 认 is_managed，同样漏）。
                if tetherpool.enabled() and tetherpool.is_managed(existing.get("proxy"))                         and not tetherpool.is_managed(proxy):
                    TP.release(aid)
                changes.update(proxy=proxy, synced_at=0, last_keepalive=0,
                               checked_at=0, proxy_status={}, ok=None,
                               note="代理已修改，请重新测试")
                receipt = _PROXY_TEST_RECEIPTS.get(aid)
                if (proxy and receipt and 0 <= time.monotonic() - receipt["at"] <= _PROXY_TEST_RECEIPT_TTL
                        and receipt["identity"] == _proxy_test_identity(proxy, existing.get("cookies"))):
                    changes["proxy_status"] = dict(receipt["result"])
                    changes["note"] = "代理配置已保存，账号浏览器状态待测试"
                engine._last_http_renew.pop(aid, None)
            _PROXY_TEST_RECEIPTS.pop(aid, None)
        acc = store.update_account(aid, **changes)
        return store.public_account(acc)


@app.post("/admin/accounts/{aid}/proxy/test")
def test_account_proxy(aid: str, req: ProxyTestRequest | None = None, _=Depends(auth)):
    import ipaddress
    import requests
    with _account_operation(aid) as acc:
        proxy = req.proxy.configuration(acc.get("proxy")) if req is not None else _account_proxy(acc)
        if not proxy:
            raise HTTPException(400, "请输入代理链接或先保存该账号的代理")
        identity = _proxy_test_identity(proxy, acc.get("cookies"))
        result = {"ok": False, "proxy_configured": True, "proxy_reachable": False,
                  "exit_ip": None, "session_ok": None, "workspace_status": None}
        try:
            # Fixed HTTPS endpoint receives no Muse cookies or proxy credentials.
            with request_proxy_context(proxy) as proxy_options, requests.Session() as session:
                session.trust_env = False
                response = session.get("https://api.ipify.org?format=json",
                                       timeout=(5, 10), allow_redirects=False,
                                       **proxy_options)
            if response.status_code != 200:
                raise MuseProxyError("代理出口检测未成功，请检查代理连接或认证")
            result["exit_ip"] = str(ipaddress.ip_address(response.json()["ip"]))
            result["proxy_reachable"] = True
            checked = engine.renew_session_http(acc.get("cookies") or {}, acc.get("cookies_exp"),
                                                wake_vm=False, proxy=proxy)
            result.update(ok=True, session_ok=True, workspace_status=checked.get("status"),
                          message="代理出口与 Muse 会话检查通过；生成能力请使用账号测试和实际任务验证")
        except MuseAuthError:
            result.update(session_ok=False, message="代理可连接，但 Muse 会话认证失败，请重新导入 Cookie")
        except MuseProxyError as exc:
            result["message"] = str(exc)
        except MuseSessionUnavailable as exc:
            _persist_session_cookies(aid, exc.cookies, exc.cookies_exp)
            result.update(session_ok=False, message=str(exc))
        except MuseGenerationError as exc:
            result.update(session_ok=False, message=str(exc))
        except (requests.RequestException, ValueError, KeyError, TypeError):
            result["message"] = "代理出口检测失败，请检查代理地址、认证和网络"
        recorded = {**result, "checked_at": int(time.time())}
        tested_saved = False
        with ACCOUNT_POOL.cond:
            current = store.get_account(aid)
            if current and identity == _proxy_test_identity(proxy, current.get("cookies")):
                # Unsaved routes stay only in memory; saving the exact configuration may adopt this proof.
                _PROXY_TEST_RECEIPTS[aid] = {"identity": identity, "at": time.monotonic(),
                                           "result": dict(recorded)}
                tested_saved = proxy == _account_proxy(current)
                if tested_saved:
                    store.update_account(aid, proxy_status=dict(recorded))
        result["tested_saved_proxy"] = tested_saved
        return result


@app.delete("/admin/accounts/{aid}")
def del_account(aid: str, _=Depends(auth)):
    acc = store.get_account(aid)
    released = False
    # patch17：删号即释——防止 allocation 泄漏。
    # patch17e（P3 根修）：release 改为 enabled 即调用（幂等，无绑定安全）。
    # 旧条件 is_managed(proxy) 漏掉「先托管后改自建代理」的账号——allocation
    # 按 (service, account_id) 存续，proxy 改自建后删除即泄漏（线上已发现
    # muse_tp_probe 残留同类实例）。
    if acc and tetherpool.enabled():
        released = TP.release(aid)
    ok = store.delete_account(aid)
    if not ok:
        raise HTTPException(404, "账号不存在")
    with ACCOUNT_POOL.cond:
        _PROXY_TEST_RECEIPTS.pop(aid, None)
    return {"deleted": True, "id": aid, "binding_released": released}


@app.post("/admin/accounts/{aid}/rebind")
def rebind_account(aid: str, _=Depends(auth)):
    """patch17：手动强制换绑出口（新 IP），binding 档案同步更新。"""
    with ACCOUNT_POOL.cond:
        acc = store.get_account(aid)
        if not acc:
            raise HTTPException(404, "账号不存在")
        if aid in ACCOUNT_POOL.busy_accounts:
            raise HTTPException(409, "账号正在执行任务或检测，请稍后再换绑")
    got = TP.acquire(aid, force=True,
                     constraints=_egress_constraints(acc))  # 锁外：控制面调用
    if not got:
        raise HTTPException(502, "TetherPool 控制面 acquire 失败，请稍后重试")
    acc = store.update_account(aid, proxy=got, binding=got.get("_binding"))
    engine._last_http_renew.pop(aid, None)
    return {"rebound": True, "id": aid, "binding": acc.get("binding")}


@app.post("/admin/accounts/{aid}/refingerprint")
def refingerprint_account(aid: str, _=Depends(auth)):
    """patch17：为账号换新设备身份（新 seed → 新指纹档案）。"""
    import secrets as _secrets
    from store import derive_fingerprint
    with ACCOUNT_POOL.cond:
        acc = store.get_account(aid)
        if not acc:
            raise HTTPException(404, "账号不存在")
        if aid in ACCOUNT_POOL.busy_accounts:
            raise HTTPException(409, "账号正在执行任务或检测，请稍后再换指纹")
    fp = derive_fingerprint(_secrets.token_hex(16))
    acc = store.update_account(aid, fingerprint=fp)
    engine._last_http_renew.pop(aid, None)
    return {"refingerprinted": True, "id": aid,
            "fingerprint": store.public_account(acc).get("fingerprint")}


@app.post("/admin/accounts/{aid}/test")
async def test_account(aid: str, _=Depends(auth)):
    """真实打开 muse.ai 验证该账号 cookie 是否仍可登录。"""
    acc = store.get_account(aid)
    if not acc:
        raise HTTPException(404, "账号不存在")
    if not acc.get("cookies"):
        raise HTTPException(400, "该账号没有 cookie")

    def _probe():
        with GEN_LOCK, _account_operation(aid) as acc:
            try:
                engine.start()
                engine.refresh(acc["cookies"], acc.get("cookies_exp"), account_id=aid)
                synced = _sync_cookies(aid)
                quota = None
                quota_error = None
                try:  # Session validity and quota readability are independent results.
                    quota = engine.quota(acc["cookies"],
                                         acc.get("cookies_exp"), account_id=aid)
                    if quota.get("found"):
                        quota["checked_at"] = int(time.time())
                        store.update_account(aid, quota=quota)
                    else:
                        quota_error = "额度面板未读到有效数据；旧缓存未覆盖"
                        quota = None
                except Exception as exc:  # noqa: BLE001
                    quota_error = f"{type(exc).__name__}；额度缓存未更新"
                store.mark(aid, True, "会话有效" + ("；额度读取失败" if quota_error else ""))
                return {"ok": True, "message": "会话有效，可正常生成",
                        "synced": synced, "quota": quota,
                        "quota_error": quota_error}
            except MuseAuthError as exc:
                store.mark(aid, False, str(exc)[:200])
                return {"ok": False, "message": str(exc)[:200]}
            except Exception as exc:  # noqa: BLE001
                store.touch_keepalive(aid, None, f"测试未确认（保留账号状态）: {str(exc)[:200]}")
                return {"ok": False, "message": str(exc)[:200]}

    res = await asyncio.to_thread(_probe)
    if not res["ok"]:
        return JSONResponse(res, status_code=200)
    return res


@app.post("/admin/accounts/{aid}/attach-probe")
async def attach_probe(aid: str, _=Depends(auth)):
    """patch22 诊断：在 API 进程内对运行引擎执行真实 attach 测试（1KB 测试 PNG），
    返回 composer 现场——用于定位 API 路径 attach 失败而外部 probe 成功的差异。"""
    acc = store.get_account(aid)
    if not acc:
        raise HTTPException(404, "账号不存在")

    def _probe():
        with GEN_LOCK, _account_operation(aid) as acc:
            try:
                engine.start()
                _mode = "reuse"
                page = engine.ensure_page(acc["cookies"], acc.get("cookies_exp"),
                                          account_id=aid)
                # patch22h 验证：先试重用页，attach 失败自动换全新页再试一次——
                # 定位「API 进程重用页失败 / 外部进程新开页成功」的状态差异。
                _attempts = []
                info1 = _composer_probe_js(page)  # round 0 成功路径也要有 before 现场
                for _round in range(2):
                    try:
                        res = engine._attach_images([_tiny_png()])
                        _attempts.append({"round": _round, "mode": _mode, "ok": True})
                        info2 = page.js("""(function(){
                            return JSON.stringify({remove_btns:Array.from(document.querySelectorAll('button[aria-label]'))
                                .map(function(b){return b.getAttribute('aria-label');})
                                .filter(function(a){return /remove|attachment/i.test(a||'');})});
                        })()""")
                        return {"ok": True, "before": info1, "after": info2,
                                "attempts": _attempts, "attach": True}
                    except Exception as _ae:
                        _attempts.append({"round": _round, "mode": _mode,
                                          "ok": False, "err": str(_ae)[:120],
                                          # patch22m：失败现场留档——否则 round0 的
                                          # DOM 状态只存在于日志，排障要翻日志拼时间线
                                          "scene": _composer_probe_js(page)})
                        # 换全新页再试
                        page = engine.refresh(acc["cookies"], acc.get("cookies_exp"),
                                              account_id=aid)
                        _mode = "fresh"
                        info1 = _composer_probe_js(page)
                return {"ok": False, "before": info1, "attempts": _attempts}
            except Exception as exc:  # noqa: BLE001
                # patch22g4d：失败时也带 before 现场（诊断信息不能被异常吞掉）
                _before = ""
                try:
                    _before = info1  # noqa: F821
                except Exception:
                    pass
                return {"ok": False, "before": _before,
                        "error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}

    return await asyncio.to_thread(_probe)


def _tiny_png():
    import base64 as _b64
    return "data:image/png;base64," + _b64.b64encode(_b64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNg"
        "YGBgAAAABQABh6FO1AAAAABJRU5ErkJggg==")).decode()


def _composer_probe_js(page):
    return page.js("""(function(){
        var ta=document.querySelector('textarea');
        var form=ta?ta.closest('form'):null;
        var h=document.querySelector('[data-hatch-shell-hydration-state]');
        return JSON.stringify({url:location.href, ready:document.readyState,
            ta:!!ta, form_len:form?form.innerHTML.length:-1,
            n_file_inputs:document.querySelectorAll('input[type=file]').length,
            ta_parent_html_len: ta.parentElement?ta.parentElement.innerHTML.length:-1,
            ta_parent_has_input: ta.parentElement? !!ta.parentElement.querySelector('input[type=file]') : null,
            hydration: h?h.getAttribute('data-hatch-shell-hydration-state'):'MISSING'});
    })()""")


@app.post("/admin/accounts/{aid}/relogin")
async def relogin_account(aid: str, _=Depends(auth)):
    return await test_account(aid, _)


@app.post("/admin/accounts/{aid}/cookies")
def update_cookies(aid: str, payload: dict = Body(...), _=Depends(auth)):
    """更新某个账号的 cookie（用于会话过期后补新 cookie）。"""
    cookies = dict(payload.get("cookies") or {})
    if payload.get("cookie_header"):
        cookies.update(parse_cookie_text(payload["cookie_header"]))
    if not cookies:
        raise HTTPException(400, "未解析到 cookie")
    exp = {k: int(v) for k, v in (payload.get("expires") or {}).items()
           if _pos(v)}
    # 补入的是「全新会话」的 cookie，有效期估算锚点必须重置到当下，
    # 否则会沿用旧会话的锚点，把剩余天数算少。
    acc = store.update_account(aid, cookies=cookies, ok=None,
                               note="已更新 cookie",
                               expiry_anchor=int(time.time()),
                               cookies_exp=exp or None)
    if not acc:
        raise HTTPException(404, "账号不存在")
    return {"ok": True, "id": aid, "cookie_count": len(cookies),
            "expires_at": acc.get("expires_at")}


@app.post("/admin/relogin")
def relogin(_=Depends(auth)):
    acc = store.pick_account(rotate=True)
    if not acc:
        raise HTTPException(400, "没有可用账号")
    try:
        with GEN_LOCK, _account_operation(acc["id"]) as selected:
            engine.start()
            engine.refresh(selected["cookies"], selected.get("cookies_exp"), account_id=selected["id"])
            return {"ok": True, "account": selected["id"]}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)


# ------------------------- 管理：额度 -------------------------
@app.post("/admin/accounts/{aid}/quota")
async def query_quota(aid: str, _=Depends(auth)):
    """打开 muse.ai 的 Settings 面板读该账号的额度（实时），并缓存到账号记录。

    返回例：{"plan":"Free plan","weekly_reset":"Sep 30",
             "weekly_used_pct":1,"extra_left":"2B tokens left",
             "extra_used_pct":0,"extra_expires":"never"}
    """
    acc = store.get_account(aid)
    if not acc:
        raise HTTPException(404, "账号不存在")
    if not acc.get("cookies"):
        raise HTTPException(400, "该账号没有 cookie")

    def _probe():
        with GEN_LOCK, _account_operation(aid) as acc:
            engine.start()
            q = engine.quota(acc["cookies"], acc.get("cookies_exp"), account_id=aid)
            if not q.get("found"):
                raise MuseGenerationError("额度面板未读到有效数据；旧缓存未覆盖")
            q["checked_at"] = int(time.time())
            store.update_account(aid, quota=q)
            return q

    try:
        return await asyncio.to_thread(_probe)
    except HTTPException:
        raise
    except MuseAuthError as exc:
        store.mark(aid, False, str(exc)[:200])
        raise HTTPException(401, str(exc)) from exc
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"额度查询失败: {exc}") from exc


@app.post("/admin/quota")
async def query_any_quota(_=Depends(auth)):
    """用当前最久未用的可用账号查一次额度（同池账号共享同一 muse.ai 计划的
    通常只有一人使用时够用；多账号时建议按账号查）。"""
    acc = store.pick_account(rotate=True)
    if not acc:
        raise HTTPException(400, "没有可用账号")
    return await query_quota(acc["id"], _)


# ------------------------- 管理：接入信息 / API Key -------------------------
def _public_base() -> str:
    return (CFG.public_base or "").rstrip("/")


@app.get("/admin/apikey")
def get_apikey(_=Depends(auth)):
    base = _public_base()
    base_url = f"{base}/v1" if base else ""
    return {"api_key": CFG.api_key, "base_url": base_url,
            "models_url": f"{base}/v1/models" if base else "",
            "media_url": f"{base}/v1/media/{{name}}" if base else "/v1/media/{name}"}


@app.post("/admin/apikey/rotate")
def rotate_apikey(_=Depends(auth)):
    """生成新的 API Key，写入 .env 并立即生效（不用重启）。"""
    import secrets
    new_key = "m2a_" + secrets.token_hex(24)
    old = CFG.api_key
    CFG.api_key = new_key
    _persist_env("MUSE2API_KEY", new_key)
    return {"ok": True, "api_key": new_key, "previous": old,
            "message": "已生成新 Key 并立即生效；旧 Key 已失效，请更新下游项目"}


def _persist_env(key: str, value: str):
    """把配置写回 .env（保留其它行，原子替换）。"""
    path = os.path.join(CFG.base_dir, ".env")
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
    os.replace(tmp, path)


# ------------------------- 管理：Cookie 获取 -------------------------
@app.get("/admin/cookie-helper")
def cookie_helper(download: int = 0):
    """返回本机取 cookie 的助手脚本（进阶方式，需要装 Python）。"""
    p = os.path.join(BASE_DIR, "tools", "get_muse_cookie.py")
    if not os.path.isfile(p):
        raise HTTPException(404, "助手脚本缺失")
    headers = {}
    if download:
        headers["Content-Disposition"] = 'attachment; filename="get_muse_cookie.py"'
    return FileResponse(p, media_type="text/x-python", headers=headers)


@app.get("/admin/extension")
def extension_zip():
    """把浏览器扩展打包成 zip 返回（推荐方式，零命令行）。

    用户下载后解压 → chrome://extensions 开发者模式加载 → 点一下就导入 cookie。
    """
    src = os.path.join(BASE_DIR, "extension")
    if not os.path.isdir(src):
        raise HTTPException(404, "扩展目录缺失")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, names in os.walk(src):
            dirs.sort()
            for name in sorted(names):
                p = os.path.join(root, name)
                relative_path = os.path.relpath(p, src).replace(os.sep, "/")
                z.write(p, f"muse2api-extension/{relative_path}")
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition":
                 'attachment; filename="muse2api-extension.zip"',
                 "Cache-Control": "no-store"})


@app.get("/admin/extension/files")
def extension_files():
    """列出扩展目录内容（前端展示用）。"""
    src = os.path.join(BASE_DIR, "extension")
    if not os.path.isdir(src):
        raise HTTPException(404, "扩展目录缺失")
    files = []
    for root, dirs, names in os.walk(src):
        dirs.sort()
        for name in sorted(names):
            p = os.path.join(root, name)
            files.append(os.path.relpath(p, src).replace(os.sep, "/"))
    return {"files": sorted(files)}


# ------------------------- 管理：任务 / 媒体 -------------------------
@app.get("/admin/tasks")
def admin_tasks(limit: int = 50, _=Depends(auth)):
    return {"tasks": store.list_tasks(limit)}


@app.delete("/admin/tasks/{tid}")
def del_task(tid: str, _=Depends(auth)):
    # patch13(F12)：进行中任务拒删——删除会丢结果、造孤儿媒体文件、
    # 蒸发幂等键（同 key 重放重复计费）。
    t = store.get_task(tid)
    if t and t.get("status") in ("queued", "processing"):
        raise HTTPException(409, "task 进行中，禁止删除")
    if not store.delete_task(tid):
        raise HTTPException(404, "任务不存在")
    return {"deleted": True}


@app.post("/admin/tasks/clear")
def clear_tasks(payload: dict = Body(default={}), _=Depends(auth)):
    # patch20(F2)：对齐 patch13(F12)——进行中任务拒删（结果丢失/孤儿媒体/
    # 幂等键蒸发），跳过并计数；带 video_request_key 的任务删除时同步清
    # 幂等索引（校验归属防误删，对齐 patch14/19 失败即清语义）。
    keep = int(payload.get("keep") or 0)
    inflight = [t["id"] for t in store.tasks.values()
                if t.get("status") in ("queued", "processing")]
    removed, skipped = 0, len(inflight)
    ids = [tid for tid in store.tasks
           if tid not in inflight]
    keep_ids = {t["id"] for t in sorted(
        (t for t in store.tasks.values() if t["id"] not in inflight),
        key=lambda t: t.get("created_at") or 0, reverse=True)[:keep]}
    for tid in ids:
        if tid in keep_ids:
            continue
        kh = (store.get_task(tid) or {}).get("video_request_key")
        if kh:
            with VIDEO_TASK_LOCK:
                idx = IDEMPOTENCY_VIDEOS.get(kh)
                if idx and idx.get("task_id") == tid:
                    IDEMPOTENCY_VIDEOS.pop(kh, None)
                    _idem_write()
        if store.delete_task(tid):
            removed += 1
    return {"removed": removed, "skipped_inflight": skipped}


def _cleanup_expired_media() -> int:
    """MUSE2API_MEDIA_RETENTION_HOURS>0 时删除超龄媒体文件；0=禁用（现状默认）。"""
    hours = CFG.media_retention_hours
    if hours <= 0 or not os.path.isdir(CFG.media_dir):
        return 0
    # patch10c：所有权收敛——备份启用时删除权归 mediabak（sha256 校验红线 +
    # 自带 max_age 磁盘保护），retention 让位，避免退避期误删未备份文件
    if mediabak.enabled():
        return 0
    cutoff = time.time() - hours * 3600
    removed = 0
    for name in os.listdir(CFG.media_dir):
        fp = os.path.join(CFG.media_dir, name)
        try:
            if os.path.isfile(fp) and os.path.getmtime(fp) < cutoff:
                os.unlink(fp)
                removed += 1
        except OSError:
            pass
    if removed:
        log.info("【媒体清理】保留期 %dh，删除 %d 个过期文件", hours, removed)
    return removed


@app.get("/admin/media")
def admin_media(_=Depends(auth)):
    d = CFG.media_dir
    items = []
    if os.path.isdir(d):
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if not os.path.isfile(p):
                continue
            ext = os.path.splitext(name)[1].lower()
            stat = os.stat(p)
            is_video = ext in VIDEO_FORMATS
            item = {"name": name, "url": media_url(name), "bytes": stat.st_size,
                    "mtime": int(stat.st_mtime), "kind": "video" if is_video else "image"}
            if is_video:
                from urllib.parse import quote
                item["thumbnail_url"] = f"/v1/media/{quote(name, safe='')}/thumbnail?v={source_version(stat)}"
            items.append(item)
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {"media": items, "count": len(items)}


@app.post("/admin/media/delete")
def delete_media(payload: dict = Body(default={}), _=Depends(auth)):
    names = payload.get("names", [])
    if isinstance(names, str):
        names = [names]
    if not isinstance(names, list):
        raise HTTPException(400, "names 必须是文件名数组")
    d = CFG.media_dir
    removed = 0
    errors = []
    for name in names:
        if not isinstance(name, str) or "/" in name or "\\" in name or ".." in name:
            errors.append(f"非法文件名: {name}")
            continue
        p = os.path.join(d, name)
        if os.path.isfile(p):
            try:
                os.remove(p)
                _clear_media_thumbnail(name)
                store.remove_archive_by_media(name)  # patch22o：媒体删了索引同步删
                removed += 1
            except OSError as e:
                errors.append(f"{name}: {str(e)}")
    return {"removed": removed, "errors": errors}


@app.delete("/admin/media/{name}")
def delete_single_media(name: str, _=Depends(auth)):
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "非法文件名")
    p = os.path.join(CFG.media_dir, name)
    if not os.path.isfile(p):
        raise HTTPException(404, "文件不存在")
    try:
        os.remove(p)
        _clear_media_thumbnail(name)
        store.remove_archive_by_media(name)  # patch22o：媒体删了索引同步删
        return {"status": "ok", "deleted": name}
    except OSError as e:
        raise HTTPException(500, f"删除失败: {e}")


# ------------------------- 前端页面 -------------------------
def _admin_html() -> str:
    p = os.path.join(BASE_DIR, "admin.html")
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ("<!doctype html><meta charset=utf-8><body style='background:#0b0f19;"
                "color:#e6e8ee;font-family:system-ui;padding:40px'>"
                "<h2>muse2api</h2><p>管理页面文件 admin.html 缺失。</p>"
                "<p>接口可用：<code>/v1/images/generations</code>、"
                "<code>/v1/videos</code></p></body>")


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(_admin_html(), headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"})


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    return HTMLResponse(_admin_html(), headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"})



# ------------------------- 账号自动保活与静默续期 -------------------------
KEEPALIVE_LOCK = asyncio.Lock()
KEEPALIVE_STATE = {
    "running": False,
    "last_run": None,
    "last_result": None,
    "next_run": None,
}


def _probe_account_sync(aid: str, check_quota: bool = False) -> dict:
    """Verify the upstream session through this account's saved route."""
    try:
        with _account_operation(aid) as acc:
            if not acc.get("cookies"):
                return {"ok": False, "id": aid, "label": acc.get("label", aid), "error": "账号无有效 cookie"}
            res = engine.renew_session_http(acc["cookies"], acc.get("cookies_exp"), wake_vm=True,
                                            **({"proxy": _account_proxy(acc)} if _account_proxy(acc) else {}))
            store.update_account(
                aid,
                cookies=res["cookies"],
                cookies_exp=res["cookies_exp"],
                ok=True if res.get("ok") else False,
                synced_at=int(time.time()),
            )
            vm_state = res.get("vm_state") or "RUNNING"
            store.touch_keepalive(aid, True, f"会话有效 · 自动保活 (VM: {vm_state})")
            quota = acc.get("quota")
            if check_quota and GEN_LOCK.acquire(blocking=False):
                try:
                    engine.start()
                    quota = engine.quota(res["cookies"], res["cookies_exp"],
                                         account_id=aid)
                    if not quota.get("found"):
                        raise MuseGenerationError("额度面板未读到有效数据；旧缓存未覆盖")
                    quota["checked_at"] = int(time.time())
                    store.update_account(aid, quota=quota)
                except Exception as qe:  # noqa: BLE001
                    log.warning("读取账号 %s 额度失败: %s", aid, qe)
                finally:
                    GEN_LOCK.release()
            updated = store.get_account(aid) or {}
            return {
                "ok": True,
                "id": aid,
                "label": acc.get("label", aid),
                "vm_id": res.get("vm_id"),
                "vm_state": vm_state,
                "wake_ok": res.get("wake_ok"),
                "expires_at": updated.get("expires_at"),
                "quota": quota,
            }
    except HTTPException as exc:
        return {"ok": False, "id": aid, "busy": exc.status_code == 409, "error": str(exc.detail)}
    except MuseAuthError as exc:
        store.touch_keepalive(aid, False, f"保活认证失败: {str(exc)[:200]}")
        return {"ok": False, "id": aid, "error": str(exc)}
    except MuseSessionUnavailable as exc:
        # 上游 1.6.11：未分配工作区响应可能轮换 cookie，落库但不算成功。
        _persist_session_cookies(aid, exc.cookies, exc.cookies_exp)
        store.touch_keepalive(aid, None, f"保活未确认（保留账号状态）: {str(exc)[:200]}")
        return {"ok": False, "id": aid, "error": str(exc), "cookies_rotated": exc.rotated}
    except Exception as exc:
        store.touch_keepalive(aid, None, f"保活未确认（保留账号状态）: {str(exc)[:200]}")
        return {"ok": False, "id": aid, "error": str(exc)}


async def run_keepalive_all(force: bool = False) -> dict:
    """执行账号保活：force=True 时强制刷新所有启用账号，否则刷新临期/未检账号并保持 VM 热备。"""
    async with KEEPALIVE_LOCK:
        now = int(time.time())
        KEEPALIVE_STATE["running"] = True
        KEEPALIVE_STATE["last_run"] = now
        results = []
        skipped = []

        try:
            include_disabled = getattr(CFG, "keepalive_disabled_accounts", False)
            accounts = [a for a in store.list_accounts() if include_disabled or a.get("enabled", True)]
            for a in accounts:
                aid = a["id"]
                label = a.get("label", aid)
                exp_at = a.get("expires_at")
                last_ka = a.get("last_keepalive") or 0

                needs_run = (
                    force
                    or not last_ka
                    or ((now - last_ka) > 6 * 3600 and
                        (not exp_at or exp_at - now < 36 * 3600 or
                         a.get("ok") is not True))
                )
                if not needs_run:
                    skipped.append({"id": aid, "label": label, "reason": "会话充足且近期已保活"})
                    continue

                log.info("【自动保活】正在为账号 %s (%s) 执行静默续期与 VM 唤醒...", label, aid)
                res = await asyncio.to_thread(_probe_account_sync, aid, False)
                results.append(res)
                log.info("【自动保活】账号 %s 执行结果: ok=%s expires_at=%s", label, res.get("ok"), res.get("expires_at"))
                await asyncio.sleep(0.5)

            summary = {
                "checked_at": now,
                "refreshed_count": len(results),
                "refreshed": results,
                "skipped_count": len(skipped),
                "skipped": skipped,
            }
            KEEPALIVE_STATE["last_result"] = summary
            KEEPALIVE_STATE["next_run"] = now + 900
            return summary
        finally:
            KEEPALIVE_STATE["running"] = False


def _warmup_browser_sync():
    """后台静默预热浏览器与首个可用账号的 WebSocket 隧道，使重启后首条请求也秒回。"""
    if getattr(engine, "current_acc_id", None) and engine.page is not None:
        return
    if not GEN_LOCK.acquire(blocking=False):
        return
    acc = None
    try:
        acc = ACCOUNT_POOL.acquire(rotate=False, timeout=0.1, record_use=False)
        log.info("【浏览器预热】正在后台预热账号 %s (%s) 的热备标签页...", acc.get("label"), acc["id"])
        _bind_account_proxy(acc)
        refreshed = _renew_and_persist(acc["id"], wake_vm=True, force=False) or acc
        engine.start()
        engine.ensure_page(refreshed["cookies"], refreshed.get("cookies_exp"), account_id=acc["id"])
        log.info("【浏览器预热】账号 %s (%s) 热备标签页与 WebSocket 已就绪", acc.get("label"), acc["id"])
    except Exception as exc:  # noqa: BLE001
        log.warning("【浏览器预热】预热异常: %s", exc)
    finally:
        # Warmup runs in a worker thread; do not retain its account reservation or page.
        engine.close_current_page()
        _clear_account_proxy()
        if acc:
            ACCOUNT_POOL.release(acc["id"])
        GEN_LOCK.release()


async def _keepalive_loop():
    """后台常驻守护任务：每 15 分钟轮询一次账号健康状态并保持 VM 热备，同时静默同步仓库更新。"""
    log.info("【自动保活守护进程】已启动，检测周期: 15 分钟")
    await asyncio.sleep(1)
    try:
        await asyncio.to_thread(_check_update_sync, True)
    except Exception:
        pass
    try:
        await asyncio.to_thread(_warmup_browser_sync)
    except Exception as e:  # noqa: BLE001
        log.warning("【浏览器预热】异常: %s", e)
    while True:
        try:
            await asyncio.to_thread(_check_update_sync, True)
        except Exception:
            pass
        try:
            await run_keepalive_all(force=False)
        except Exception as e:  # noqa: BLE001
            log.error("【自动保活守护进程】轮询异常: %s", e)
        try:
            await asyncio.to_thread(_cleanup_expired_media)
        except Exception as e:  # noqa: BLE001
            log.warning("【媒体清理】异常: %s", str(e)[:80])
        await asyncio.sleep(900)


@app.post("/admin/accounts/keepalive")
async def trigger_keepalive_all(force: bool = True, _=Depends(auth)):
    """管理员手动触发一次全账号保活续期。"""
    if KEEPALIVE_STATE["running"]:
        return {"status": "busy", "message": "保活任务正在执行中，请稍候"}
    return await run_keepalive_all(force=force)


@app.post("/admin/accounts/{aid}/keepalive")
async def trigger_keepalive_single(aid: str, _=Depends(auth)):
    """手动针对单个账号执行保活续期。"""
    return await asyncio.to_thread(_probe_account_sync, aid, False)


@app.get("/admin/keepalive/status")
def get_keepalive_status(_=Depends(auth)):
    """获取保活守护协程状态。"""
    return KEEPALIVE_STATE


# ------------------------- 仓库实时更新检测、通知与一键在线升级 -------------------------
# 上游 1.6.11：仓库/分支/托管部署可配；检查只读、升级钉住单一已验证提交。
REPO_SLUG = os.environ.get("MUSE2API_REPOSITORY", "czg86389-hub/muse2api")
if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", REPO_SLUG):
    REPO_SLUG = "czg86389-hub/muse2api"
REPO_REF = os.environ.get("MUSE2API_REPO_REF", "main")
REPO_URL = f"https://github.com/{REPO_SLUG}"
MANAGED_DEPLOYMENT = os.environ.get("MUSE2API_MANAGED_DEPLOYMENT", "0") == "1"
TRACKED_REPO_PATHS = [
    "app.py", "engine.py", "store.py", "cdp.py", "config.py", "proxy.py",
    "tetherpool.py", "mediabak.py", "generation_timing.py", "thumbnails.py",
    "GENERATION_QUEUE.md", "VERSION_REPORTING.md",
    "admin.html", "README.md", "version.json", "requirements.txt",
    "Dockerfile", "docker-compose.yml", ".env.example", ".gitignore",
    "LICENSE", "extension", "deploy", "tools",
]
_UPDATE_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}


def _read_env_key(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if val:
        return val
    path = os.path.join(CFG.base_dir, ".env")
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f.read().splitlines():
                s = ln.strip()
                if s.startswith(key + "="):
                    return s.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def _read_local_version() -> dict:
    p = os.path.join(BASE_DIR, "version.json")
    try:
        with open(p, encoding="utf-8") as f:
            obj = json.load(f)
            if isinstance(obj, dict):
                return obj
    except Exception:
        pass
    return {"version": "1.5.0", "highlights": []}


def _installed_sha_file() -> str:
    return os.path.join(CFG.data_dir, ".installed_sha")


def _git(args: list[str], timeout: int = 30):
    import subprocess
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(
        ["git", "-c", f"safe.directory={BASE_DIR}", *args],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _ensure_git_repo(token: str = ""):
    """确保 BASE_DIR 已初始化为绑定 czg86389-hub/muse2api 的 Git 仓库。"""
    git_dir = os.path.join(BASE_DIR, ".git")
    remote_url = f"https://x-access-token:{token}@github.com/czg86389-hub/muse2api.git" if token else f"{REPO_URL}.git"
    if not os.path.isdir(git_dir):
        _git(["init", "-b", "main"])
        _git(["remote", "add", "origin", remote_url])
        _git(["fetch", "origin", "main"], timeout=45)
        _git(["reset", "--mixed", "origin/main"])
    else:
        _git(["remote", "set-url", "origin", remote_url])
    _git(["config", "user.name", "czg86389-hub"])
    _git(["config", "user.email", "czg86389-hub@users.noreply.github.com"])


# 二开（patch21）：上游 1.6.11 已彻底移除「静默自升级」——检查只读、升级必须
# 管理员显式 POST 且钉住单一已验证提交。这里不再保留任何 Tarball 自动覆盖路径：
# 2026-11-16 01:44 事故（自更新把 /app 覆盖成无补丁的上游 main、7 个视频任务
# 全部超时）即旧的静默升级 worker 所致，本 fork 补丁永不在上游里。
def _check_update_sync(force: bool = False) -> dict:
    """A check is read-only. Report installed files and remote state separately."""
    now = time.time()
    if not force and _UPDATE_CACHE["data"] and now - _UPDATE_CACHE["ts"] < 90:
        return _UPDATE_CACHE["data"]
    import requests
    from urllib.parse import quote
    local = _read_local_version()
    local_version = str(local.get("version") or "unknown")
    local_sha = os.environ.get("MUSE2API_BUILD_SHA", "").strip()
    has_git = os.path.exists(os.path.join(BASE_DIR, ".git"))
    if not local_sha and has_git:
        result = _git(["rev-parse", "HEAD"])
        if result.returncode == 0:
            local_sha = result.stdout.strip()
    if not local_sha and os.path.isfile(_installed_sha_file()):
        with open(_installed_sha_file()) as f:
            local_sha = f.read().strip()
    local_sha = local_sha[:7]
    local_dirty = False
    if has_git:
        changed = _git(["status", "--porcelain", "--", *TRACKED_REPO_PATHS])
        local_dirty = changed.returncode == 0 and bool(changed.stdout.strip())
    remote_version, remote_sha, remote_commit, remote_msg, remote_time = "", "", "", "", ""
    commits, highlights, highlights_en, errors = [], [], [], []
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "muse2api-updater"}
    token = _read_env_key("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = requests.get(
            f"https://api.github.com/repos/{REPO_SLUG}/commits",
            params={"sha": REPO_REF, "per_page": 5}, headers=headers, timeout=6)
        response.raise_for_status()
        raw_commits = response.json()
        remote_commit = raw_commits[0]["sha"] if raw_commits else ""
        for commit in raw_commits:
            info = commit.get("commit") or {}
            commits.append({"sha": commit["sha"][:7], "message": info.get("message", "").split("\n")[0],
                            "date": (info.get("committer") or {}).get("date", ""),
                            "url": commit.get("html_url") or REPO_URL})
        if commits:
            remote_sha, remote_msg, remote_time = (commits[0][k] for k in ("sha", "message", "date"))
    except Exception:
        errors.append("远端提交读取失败")
    # Pin release notes and version to the same commit as the reported remote SHA.
    try:
        response = requests.get(
            f"https://raw.githubusercontent.com/{REPO_SLUG}/{quote(remote_commit or REPO_REF, safe='')}/version.json",
            timeout=6)
        response.raise_for_status()
        remote = response.json()
        remote_version = str(remote.get("version") or "")
        highlights = list(remote.get("highlights") or [])
        highlights_en = list(remote.get("highlights_en") or [])
    except Exception:
        errors.append("远端版本文件读取失败")
    verified = bool(local_sha and remote_sha and remote_version)
    has_update = bool((local_sha and remote_sha and local_sha != remote_sha) or
                      (remote_version and local_version != remote_version))
    data = {"repo_url": REPO_URL, "repo_ref": REPO_REF, "has_git": has_git,
            "local_version": local_version, "local_sha": local_sha,
            "remote_version": remote_version, "remote_sha": remote_sha, "remote_commit": remote_commit,
            "remote_msg": remote_msg, "remote_time": remote_time,
            "has_update": has_update, "up_to_date": (not has_update and not local_dirty) if verified else None,
            "local_dirty": local_dirty,
            "check_error": "; ".join(errors), "managed_deployment": MANAGED_DEPLOYMENT,
            "highlights": highlights, "highlights_en": highlights_en,
            "recent_commits": commits, "checked_at": int(now)}
    _UPDATE_CACHE.update(ts=now, data=data)
    return data


def _upgrade_from_github_sync() -> dict:
    """Explicit upgrades pin one verified remote commit and preserve deployment data."""
    if MANAGED_DEPLOYMENT:
        raise HTTPException(409, "此实例通过镜像部署，请从配置的仓库分支构建并更新容器")
    if GEN_LOCK.locked() or any(t.get("status") in ("queued", "processing") for t in store.tasks.values()):
        raise HTTPException(409, "仍有任务排队或运行，请等待任务完成后更新")
    import requests
    import tarfile
    target = _check_update_sync(force=True).get("remote_commit", "")
    if not re.fullmatch(r"[0-9a-f]{40}", target):
        raise HTTPException(502, "无法确认远端目标提交，未执行更新")
    upgraded_via = ""
    try:
        _ensure_git_repo(_read_env_key("GITHUB_TOKEN"))
        for command in (["fetch", "origin", REPO_REF],
                        ["checkout", "-f", target, "--", "."],
                        ["reset", "--mixed", target]):
            result = _git(command, timeout=45)
            result.check_returncode()
        upgraded_via = "git"
    except Exception as exc:
        log.warning("Git 更新未完成，尝试固定提交归档 (%s)", type(exc).__name__)
    if not upgraded_via:
        response = requests.get(f"https://codeload.github.com/{REPO_SLUG}/tar.gz/{target}", timeout=60)
        if response.status_code != 200:
            raise HTTPException(502, f"下载更新包失败 (HTTP {response.status_code})")
        with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as archive:
            for member in archive.getmembers():
                parts = member.name.split("/", 1)
                if len(parts) < 2 or not parts[1]:
                    continue
                rel = parts[1].replace("\\", "/")
                if rel.startswith("/") or ".." in rel.split("/") or rel == ".env" or rel.startswith("data/") or rel.startswith(".git/"):
                    continue
                path = os.path.join(BASE_DIR, rel)
                if member.isdir():
                    os.makedirs(path, exist_ok=True)
                elif member.isfile():
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with archive.extractfile(member) as source, open(path, "wb") as out:
                        out.write(source.read())
        upgraded_via = "tarball"
    # Record what was installed, never a newer remote HEAD observed after the download.
    with open(_installed_sha_file(), "w") as f:
        f.write(target)
    _UPDATE_CACHE["ts"] = 0.0
    status = _check_update_sync(force=True)
    status["local_sha"] = target[:7]
    status["has_update"] = bool(status.get("remote_sha") and status["remote_sha"] != target[:7])
    status["up_to_date"] = not status["has_update"] if status.get("remote_sha") else None
    return {"ok": True, "via": upgraded_via, "installed_sha": target,
            "message": f"已安装版本 {_read_local_version().get('version')} ({target[:7]})", "status": status}


@app.get("/admin/update/check")
@app.get("/admin/repo/status")
async def admin_check_update(force: bool = False, _=Depends(auth)):
    """检测上游仓库是否有新版本。patch13(F2)：加鉴权且 HTTP 路径不触发静默
    自升级（未鉴权 GET 曾可后台拉起 os._exit(0) 重启，杀掉进行中的付费生成）。"""
    return await asyncio.to_thread(_check_update_sync, force)


@app.post("/admin/update/upgrade")
@app.post("/admin/repo/pull")
async def admin_upgrade_now(payload: dict = Body(default={}), _=Depends(auth)):
    """一键从 GitHub 官方仓库拉取最新更新并自动平滑重启服务。"""
    res = await asyncio.to_thread(_upgrade_from_github_sync)
    restart = payload.get("restart", True) if isinstance(payload, dict) else True
    if restart:
        def _delayed_restart():
            time.sleep(0.5)
            try:
                engine.stop()
            except Exception:
                pass
            os._exit(0)
        threading.Thread(target=_delayed_restart, daemon=True).start()
    return res


@app.post("/admin/repo/push")
async def admin_repo_push(payload: dict = Body(default={}), _=Depends(auth)):
    """维护者专用：将当前节点核心代码推送到 GitHub 仓库（自动过滤 .env 与 data 目录）。"""
    msg = (payload.get("message") or "").strip() or f"chore: sync update ({time.strftime('%Y-%m-%d %H:%M:%S')})"
    new_token = (payload.get("github_token") or "").strip()
    if new_token:
        _persist_env("GITHUB_TOKEN", new_token)
        os.environ["GITHUB_TOKEN"] = new_token
    token = new_token or _read_env_key("GITHUB_TOKEN")

    def _do_push():
        _ensure_git_repo(token)
        existing_paths = [p for p in TRACKED_REPO_PATHS if os.path.exists(os.path.join(BASE_DIR, p))]
        _git(["add", "--", *existing_paths])
        st = _git(["status", "--porcelain", "--", *existing_paths])
        committed = False
        if st.stdout.strip():
            c_res = _git(["commit", "-m", msg])
            if c_res.returncode != 0:
                raise HTTPException(500, f"Git commit 失败: {c_res.stderr or c_res.stdout}")
            committed = True
        p_res = _git(["push", "origin", "HEAD:main"], timeout=60)
        if p_res.returncode != 0:
            err = (p_res.stderr or p_res.stdout or "").strip()
            raise HTTPException(500, f"Git push 失败: {err[:300]}")
        _UPDATE_CACHE["ts"] = 0.0
        status = _check_update_sync(force=True)
        return {
            "ok": True,
            "committed": committed,
            "message": "已成功提交并推送到 GitHub 仓库",
            "status": status,
        }

    return await asyncio.to_thread(_do_push)


@app.on_event("startup")
async def _startup():
    _idem_load()  # patch22o：幂等索引从磁盘恢复（TTL 外项丢弃）
    for task in list(store.tasks.values()):
        if task.get("kind") in ("image", "video") and task.get("status") in ("queued", "processing"):
            sweep_stage = task.get("stage") or "queued"
            interrupted_safe = _stage_before(sweep_stage, "submit")
            store.update_task(task["id"], status="failed",
                              error="服务重启中断了任务，请重新提交",
                              # patch22：提交后中断的上游结果未知——不许自动重放
                              generation_accepted=("no" if interrupted_safe else "unknown"),
                              safe_to_resubmit=interrupted_safe)
            # patch19+patch22：sweep 清幂等索引只对「提交前中断」生效；
            # 提交后中断保留索引（同 key 重放拿到带 safe_to_resubmit=False
            # 的失败任务，由调用方决策），24h TTL 兜底防陈旧项。
            with VIDEO_TASK_LOCK:
                kh = task.get("video_request_key")
                if (kh and interrupted_safe
                        and IDEMPOTENCY_VIDEOS.get(kh, {}).get("task_id") == task["id"]):
                    IDEMPOTENCY_VIDEOS.pop(kh, None)
                    _idem_write()
    if not CFG.api_key:
        import secrets
        new_key = "m2a_" + secrets.token_hex(24)
        CFG.api_key = new_key
        _persist_env("MUSE2API_KEY", new_key)
        log.info("🔑 未检测到 MUSE2API_KEY，已自动生成初始密钥: %s", new_key)
    asyncio.create_task(_keepalive_loop())
    threading.Thread(target=MEDIA_BACKUPER.run_forever, daemon=True,
                     name="media-backup").start()
    log.info("【媒体备份】守护线程启动（interval=%ss delete_local=%s）",
             CFG.backup_interval, CFG.backup_delete_local)
    if tetherpool.enabled():
        asyncio.create_task(_tetherpool_heartbeat())


async def _tetherpool_heartbeat():
    """TetherPool 心跳：周期核对托管账号绑定；控制面恢复后自动补绑（自愈）。

    二开 P1/P2 根修：①网络调用 to_thread（同步 requests 阻塞 event loop 20s 的
    故障已被 review 实锤）；②只对"进程内无绑定缓存"的账号 acquire —— 已绑定
    账号保持节点粘性，不再每 300s churn 换出口（旧实现每次心跳换一个节点）。
    """
    log.info("【TetherPool】心跳线程启动（间隔 300s，只补缺失绑定）")
    while True:
        try:
            for acc in store.list_accounts():
                _px = acc.get("proxy")
                # patch17b（P2 根修）：跳过条件收紧——「非托管且有显式自建
                # 代理」才不介入；无代理（加号即绑失败）或占位值账号允许
                # 心跳补绑，否则"心跳自动补绑"承诺对这类账号落空。
                if not tetherpool.is_managed(_px) and _px:
                    continue
                aid = acc["id"]
                if TP.cached_binding(aid):
                    continue  # 已绑定：保持粘性，不 churn
                # patch17e（P2）：补绑透传账号出口约束——否则设了 region/
                # egress 的账号在心跳补绑后约束静默失效。
                got = await asyncio.to_thread(
                    TP.acquire, aid, False, False, _egress_constraints(acc))
                if got:
                    # patch17：心跳补绑也回写 binding 档案（与 acquire 响应同步）
                    store.update_account(aid, proxy=got,
                                         binding=got.get("_binding"))
        except Exception as exc:  # noqa: BLE001
            log.warning("【TetherPool】心跳异常: %s", str(exc)[:120])
        await asyncio.sleep(300)


@app.get("/admin/tetherpool/status")
def tetherpool_status(_=Depends(auth)):
    return TP.status()


# ---------------- patch22：历史提交按幂等键/request hash 查询 ----------------
def _submission_view(t: dict) -> dict:
    """公开任务↔原生任务映射 + 重放安全性的单一视图（排障/Agent 契约）。"""
    return {
        "task_id": t.get("id"),
        "status": t.get("status"),
        "stage": t.get("stage", ""),
        "error_code": t.get("error_code", ""),
        "error": t.get("error", ""),
        "retryable": t.get("retryable"),
        "generation_accepted": t.get("generation_accepted", ""),
        "safe_to_resubmit": t.get("safe_to_resubmit"),
        "created_at": t.get("created_at"),
        "video_request_key": t.get("video_request_key", ""),
        "video_request_hash": t.get("video_request_hash", ""),
    }


@app.get("/admin/videos/idempotency/{key_hash}")
def admin_video_by_key(key_hash: str, _=Depends(auth)):
    """按 Idempotency-Key 的 sha256（key_hash）查询原提交。

    返回索引命中 + task 兜底扫描的合并结果；绝不清理冲突记录。
    key_hash = sha256(Idempotency-Key 原文)（与写入路径同算法）。"""
    with VIDEO_TASK_LOCK:
        idx = dict(IDEMPOTENCY_VIDEOS.get(key_hash) or {})
    hits = []
    seen = set()
    if idx.get("task_id"):
        t = store.get_task(idx["task_id"])
        if t:
            hits.append(_submission_view(t))
            seen.add(t["id"])
    for t in store.tasks.values():
        if t.get("kind") == "video" and t.get("video_request_key") == key_hash \
                and t["id"] not in seen:
            hits.append(_submission_view(t))
            seen.add(t["id"])
    return {"key_hash": key_hash, "in_index": bool(idx),
            "request_hash": idx.get("request_hash", ""),
            "matches": hits}


@app.get("/admin/videos/by-request-hash/{request_hash}")
def admin_video_by_hash(request_hash: str, _=Depends(auth)):
    """按请求体 hash（sha256(JSON(req.model_dump()+归一化参考图))）查询原提交。"""
    hits = [_submission_view(t) for t in store.tasks.values()
            if t.get("kind") == "video" and t.get("video_request_hash") == request_hash]
    return {"request_hash": request_hash, "matches": hits}


@app.get("/admin/videos/by-public/{public_task_id}")
def admin_video_by_public(public_task_id: str, _=Depends(auth)):
    """按调用方（网关）公开任务 ID 直查本地任务——网关侧存有
    upstream_task_id 映射时，用本端点还原完整链路（OpenPet 排障契约）。"""
    t = store.get_task(public_task_id)
    if not t or t.get("kind") != "video":
        raise HTTPException(404, "task 不存在")
    return _submission_view(t)


@app.post("/admin/accounts/{aid}/tetherpool/rebind")
def tetherpool_rebind(aid: str, _=Depends(auth)):
    """旧重绑路由（兼容既有调用方）——patch17b 起委托统一实现，补齐
    账号约束透传与 binding 档案回写；保留本端点独有的检测状态重置语义。"""
    result = rebind_account(aid, _)
    acc = store.update_account(aid, synced_at=0, last_keepalive=0,
                               checked_at=0, proxy_status={}, ok=None,
                               note="TetherPool 已重绑出口，请重新测试")
    return store.public_account(acc) if acc else result


@app.on_event("shutdown")
def _shutdown():
    engine.stop()
