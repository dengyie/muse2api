# -*- coding: utf-8 -*-
"""patch22r-review：仓库/版本/升级子系统——从 app.py 拆出（结构迭代第一步）。

职责单一：上游版本检查（只读）、管理员显式升级（钉住单一已验证提交）、
维护者推送（repo/push）。不依赖 fastapi——错误用 UpdaterError（带 http
状态码语义），app.py 路由壳负责转 HTTPException。可独立 import 测试。

事故契约（不可回退）：
  patch21：上游 1.6.11 已彻底移除「静默自升级」——检查只读、升级必须管理员
  显式 POST 且钉住单一已验证提交；不保留任何 Tarball 自动覆盖路径。2026-11-16
  01:44 事故（自更新把 /app 覆盖成无补丁的上游 main、7 个视频任务全部超时）
  即旧静默升级 worker 所致，本 fork 补丁永不在上游里。
  patch22r：镜像部署强制 MUSE2API_MANAGED_DEPLOYMENT=1（Dockerfile ENV），
  /admin/update/upgrade 在托管部署下直接 409。
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import tarfile
import time
from typing import Any, Callable

import requests

log = logging.getLogger("muse2api.updater")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class UpdaterError(Exception):
    """升级/推送失败（携带 HTTP 语义状态码，app 路由壳据此转 HTTPException）。"""

    def __init__(self, message: str, status: int = 500):
        super().__init__(message)
        self.status = status


REPO_SLUG = os.environ.get("MUSE2API_REPOSITORY", "dengyie/muse2api")
if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", REPO_SLUG):
    REPO_SLUG = "dengyie/muse2api"
REPO_REF = os.environ.get("MUSE2API_REPO_REF", "main")
REPO_URL = f"https://github.com/{REPO_SLUG}"
MANAGED_DEPLOYMENT = os.environ.get("MUSE2API_MANAGED_DEPLOYMENT", "0") == "1"

# 全量受管文件清单（dirty 检查 / push add 面）——与 Dockerfile COPY 清单同步维护
TRACKED_REPO_PATHS = [
    "app.py", "engine.py", "store.py", "cdp.py", "config.py", "proxy.py",
    "tetherpool.py", "mediabak.py", "idempotency.py", "updater.py",
    "generation_timing.py", "thumbnails.py",
    "GENERATION_QUEUE.md", "VERSION_REPORTING.md",
    "admin.html", "README.md", "version.json", "requirements.txt",
    "Dockerfile", "docker-compose.yml", ".env.example", ".gitignore",
    "LICENSE", "extension", "deploy", "tools",
]
_UPDATE_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}


def read_env_key(key: str) -> str:
    """环境变量 > data/runtime.env（卷内，rotate/push 持久化目标）> base/.env。"""
    val = os.environ.get(key, "").strip()
    if val:
        return val
    from config import CFG
    for path in (os.path.join(CFG.data_dir, "runtime.env"),
                 os.path.join(CFG.base_dir, ".env")):
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


def installed_sha_file() -> str:
    from config import CFG
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


def ensure_git_repo(token: str = ""):
    """确保 BASE_DIR 已初始化为绑定 REPO_SLUG 的 Git 仓库。"""
    git_dir = os.path.join(BASE_DIR, ".git")
    remote_url = f"https://x-access-token:{token}@github.com/{REPO_SLUG}.git" if token else f"{REPO_URL}.git"
    if not os.path.isdir(git_dir):
        _git(["init", "-b", "main"])
        _git(["remote", "add", "origin", remote_url])
        _git(["fetch", "origin", "main"], timeout=45)
        _git(["reset", "--mixed", "origin/main"])
    else:
        _git(["remote", "set-url", "origin", remote_url])
    _git(["config", "user.name", REPO_SLUG.split("/", 1)[0]])
    _git(["config", "user.email", f"{REPO_SLUG.split('/', 1)[0]}@users.noreply.github.com"])


def check(force: bool = False) -> dict:
    """上游版本检查（只读，90s 缓存）。报告本地安装面与远端状态，互不混写。"""
    now = time.time()
    if not force and _UPDATE_CACHE["data"] and now - _UPDATE_CACHE["ts"] < 90:
        return _UPDATE_CACHE["data"]
    from urllib.parse import quote
    local = _read_local_version()
    local_version = str(local.get("version") or "unknown")
    local_sha = os.environ.get("MUSE2API_BUILD_SHA", "").strip()
    has_git = os.path.exists(os.path.join(BASE_DIR, ".git"))
    if not local_sha and has_git:
        result = _git(["rev-parse", "HEAD"])
        if result.returncode == 0:
            local_sha = result.stdout.strip()
    if not local_sha and os.path.isfile(installed_sha_file()):
        with open(installed_sha_file()) as f:
            local_sha = f.read().strip()
    local_sha = local_sha[:7]
    local_dirty = False
    if has_git:
        changed = _git(["status", "--porcelain", "--", *TRACKED_REPO_PATHS])
        local_dirty = changed.returncode == 0 and bool(changed.stdout.strip())
    remote_version, remote_sha, remote_commit, remote_msg, remote_time = "", "", "", "", ""
    commits, highlights, highlights_en, errors = [], [], [], []
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "muse2api-updater"}
    token = read_env_key("GITHUB_TOKEN")
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
    # release notes 与版本钉在同一远端提交（防 notes/SHA 错位）
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


def upgrade(busy: Callable[[], bool] | None = None) -> dict:
    """管理员显式升级：钉住单一已验证远端提交，保留 .env 与 data/。

    busy：调用方注入的占用检查（有任务排队/运行时返回 True）——本模块
    不感知业务并发面（依赖方向：updater 不 import app）。
    """
    if MANAGED_DEPLOYMENT:
        raise UpdaterError("此实例通过镜像部署，请从配置的仓库分支构建并更新容器", status=409)
    if busy and busy():
        raise UpdaterError("仍有任务排队或运行，请等待任务完成后更新", status=409)
    target = check(force=True).get("remote_commit", "")
    if not re.fullmatch(r"[0-9a-f]{40}", target):
        raise UpdaterError("无法确认远端目标提交，未执行更新", status=502)
    upgraded_via = ""
    try:
        ensure_git_repo(read_env_key("GITHUB_TOKEN"))
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
            raise UpdaterError(f"下载更新包失败 (HTTP {response.status_code})", status=502)
        with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as archive:
            for member in archive.getmembers():
                parts = member.name.split("/", 1)
                if len(parts) < 2 or not parts[1]:
                    continue
                rel = parts[1].replace("\\", "/")
                # 路径穿越硬拒绝：.env/数据/git 元数据永不落地
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
    # 记录已安装 SHA（绝不写下载之后才观察到的新远端 HEAD）
    with open(installed_sha_file(), "w") as f:
        f.write(target)
    _UPDATE_CACHE["ts"] = 0.0
    status = check(force=True)
    status["local_sha"] = target[:7]
    status["has_update"] = bool(status.get("remote_sha") and status["remote_sha"] != target[:7])
    status["up_to_date"] = not status["has_update"] if status.get("remote_sha") else None
    return {"ok": True, "via": upgraded_via, "installed_sha": target,
            "message": f"已安装版本 {_read_local_version().get('version')} ({target[:7]})", "status": status}


def push(message: str = "", token: str = "") -> dict:
    """维护者推送当前节点受管文件到 REPO_SLUG（自动过滤 .env 与 data 目录）。"""
    msg = (message or "").strip() or f"chore: sync update ({time.strftime('%Y-%m-%d %H:%M:%S')})"
    auth_token = (token or "").strip() or read_env_key("GITHUB_TOKEN")
    ensure_git_repo(auth_token)
    existing_paths = [p for p in TRACKED_REPO_PATHS if os.path.exists(os.path.join(BASE_DIR, p))]
    _git(["add", "--", *existing_paths])
    st = _git(["status", "--porcelain", "--", *existing_paths])
    committed = False
    if st.stdout.strip():
        c_res = _git(["commit", "-m", msg])
        if c_res.returncode != 0:
            raise UpdaterError(f"Git commit 失败: {c_res.stderr or c_res.stdout}", status=500)
        committed = True
    p_res = _git(["push", "origin", "HEAD:main"], timeout=60)
    if p_res.returncode != 0:
        err = (p_res.stderr or p_res.stdout or "").strip()
        raise UpdaterError(f"Git push 失败: {err[:300]}", status=500)
    _UPDATE_CACHE["ts"] = 0.0
    status = check(force=True)
    return {"ok": True, "committed": committed,
            "message": "已成功提交并推送到 GitHub 仓库", "status": status}
