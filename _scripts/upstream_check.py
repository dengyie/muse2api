def _check_update_sync(force: bool = False, _auto_upgrade: bool = False) -> dict:
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


