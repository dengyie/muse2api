def _upgrade_from_github_sync() -> dict:
    """Explicit upgrades pin one verified remote commit and preserve deployment data."""
    if MANAGED_DEPLOYMENT:
        raise HTTPException(409, "此实例通过镜像部署，请从配置的仓库分支构建并更新容器")
    if GEN_LOCK.locked() or any(t.get("status") in ("queued", "processing") for t in store.tasks.values()):
        raise HTTPException(409, "仍有任务排队或运行，请等待任务完成后更新")
    import requests
    import tarfile
    target = _check_update_sync(force=True, _auto_upgrade=False).get("remote_commit", "")
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
    status = _check_update_sync(force=True, _auto_upgrade=False)
    status["local_sha"] = target[:7]
    status["has_update"] = bool(status.get("remote_sha") and status["remote_sha"] != target[:7])
    status["up_to_date"] = not status["has_update"] if status.get("remote_sha") else None
    return {"ok": True, "via": upgraded_via, "installed_sha": target,
            "message": f"已安装版本 {_read_local_version().get('version')} ({target[:7]})", "status": status}


