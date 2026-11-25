# -*- coding: utf-8 -*-
import io

path = "app.py"
src = io.open(path, encoding="utf-8").read()

# ---------- 3. replace _check_update_sync with upstream read-only version ----------
start = src.index("def _check_update_sync(")
end_marker = "def _upgrade_from_github_sync("
end = src.index(end_marker, start)
upstream_check = '''def _check_update_sync(force: bool = False) -> dict:
    """检测只读（上游 1.6.11 语义）：本地安装态与远端状态分开如实上报，
    绝不自动升级、绝不把远端版本伪装成本地版本。"""
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
        with open(_installed_sha_file(), encoding="utf-8") as f:
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
            commits.append({"sha": commit["sha"][:7], "message": info.get("message", "").split("\\n")[0],
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


'''
src = src[:start] + upstream_check + src[end:]

# ---------- 4. replace _upgrade_from_github_sync with upstream pinned-commit version ----------
start = src.index("def _upgrade_from_github_sync(")
end_marker = '@app.get("/admin/update/check")'
end = src.index(end_marker, start)
upstream_upgrade = '''def _upgrade_from_github_sync() -> dict:
    """显式升级（上游 1.6.11 语义）：钉住单一已验证远端提交，保留部署数据。
    托管镜像部署与排队/运行中任务直接拒绝；绝不触碰 .env 与 data/。"""
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
                rel = parts[1].replace("\\\\", "/")
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
    with open(_installed_sha_file(), "w", encoding="utf-8") as f:
        f.write(target)
    _UPDATE_CACHE["ts"] = 0.0
    status = _check_update_sync(force=True)
    status["local_sha"] = target[:7]
    status["has_update"] = bool(status.get("remote_sha") and status["remote_sha"] != target[:7])
    status["up_to_date"] = not status["has_update"] if status.get("remote_sha") else None
    return {"ok": True, "via": upgraded_via, "installed_sha": target,
            "message": f"已安装版本 {_read_local_version().get('version')} ({target[:7]})", "status": status}


'''
src = src[:start] + upstream_upgrade + src[end:]

# ---------- 5. fix call sites ----------
old = 'await asyncio.to_thread(_check_update_sync, force, False)'
assert src.count(old) == 2, src.count(old)
src = src.replace(old, 'await asyncio.to_thread(_check_update_sync, force)')

old = '''        _UPDATE_CACHE["ts"] = 0.0
        status = _check_update_sync(force=True, _auto_upgrade=False)'''
assert src.count(old) == 1
src = src.replace(old, '''        _UPDATE_CACHE["ts"] = 0.0
        status = _check_update_sync(force=True)''')

# keepalive loop: no auto-upgrade path exists anymore; check stays read-only
old = '_check_update_sync, True, _KEEPALIVE_AUTO_UPGRADE)'
assert src.count(old) == 2, src.count(old)
src = src.replace(old, '_check_update_sync, True)')

io.open(path, "w", encoding="utf-8", newline="").write(src)
print("OK")
