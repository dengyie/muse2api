# -*- coding: utf-8 -*-
"""patch22r：幂等索引子系统——视频/图片双域统一实现。

演进史：
  patch12   VIDEO_TASK_LOCK 防并发同 key 双建
  patch13F4 索引与 task 生命周期解耦（task 会被 prune，key 寄生 task 会 miss）
  patch14F4 TTL 生效
  patch22o  落盘 idempotency_videos.json（重启后同 key 重放仍命中，杜绝重复计费）
  patch22r  抽取为独立模块 + 图片域对齐（image 侧此前只寄生 task 扫描，任务被裁
            后同 key 重放会重复建任务重复计费——video 侧同型问题的未修面）+
            写前 purge TTL 僵尸条目（此前只有同 key 重放才清理，长寿命进程慢泄漏）

语义（调用方需持对应域的锁保证 check+create+register 原子性）：
  同 key 同 request_hash → 命中原任务（调用方返回原任务）
  同 key 异 request_hash → 冲突（调用方抛结构化 409，本索引绝不覆盖旧记录）
  不同 key → 互不冲突
"""
import os
import threading
import time

from store import _read, _write


class IdempotencyIndex:
    """单域幂等索引（key_hash -> {task_id, request_hash, ts}），落盘 + TTL。"""

    def __init__(self, path: str, ttl: int = 24 * 3600):
        self.path = path
        self.ttl = ttl
        self.data: dict[str, dict] = {}
        # 读写锁保护：注册/清理/回读在请求线程与后台 worker 都会发生；
        # 调用方的外层业务锁（VIDEO/IMAGE_TASK_LOCK）保证 check+create 原子性，
        # 这里的锁只保护本索引自身结构与落盘。
        self._lock = threading.Lock()

    def load(self):
        """启动回读：TTL 外条目丢弃（重启即清僵尸）。"""
        with self._lock:
            now = time.time()
            self.data = {k: v for k, v in _read(self.path, {}).items()
                         if now - (v.get("ts") or 0) <= self.ttl}

    @staticmethod
    def _purged(d: dict, ttl: int) -> dict:
        now = time.time()
        return {k: v for k, v in d.items() if now - (v.get("ts") or 0) <= ttl}

    def _flush_locked(self):
        """落盘（写前 purge——patch22r：不再依赖同 key 重放才清僵尸）。"""
        _write(self.path, self._purged(self.data, self.ttl))

    def register(self, key_hash: str, task_id: str, request_hash: str):
        if not key_hash:
            return
        with self._lock:
            self.data[key_hash] = {"task_id": task_id,
                                   "request_hash": request_hash,
                                   "ts": time.time()}
            self._flush_locked()

    def lookup(self, key_hash: str) -> dict | None:
        """命中返回 {task_id, request_hash, ts}；TTL 过期惰性清理后返回 None。
        不校验任务是否存活——调用方 miss 后自行 pop（与原语义一致）。"""
        if not key_hash:
            return None
        with self._lock:
            hit = self.data.get(key_hash)
            if not hit:
                return None
            if time.time() - hit.get("ts", 0) > self.ttl:
                self.data.pop(key_hash, None)
                self._flush_locked()
                return None
            return dict(hit)

    def pop(self, key_hash: str, task_id: str | None = None) -> bool:
        """清索引项。task_id 给定时仅当指向一致才清（防误删他任务的记录）。"""
        if not key_hash:
            return False
        with self._lock:
            hit = self.data.get(key_hash)
            if not hit:
                return False
            if task_id is not None and hit.get("task_id") != task_id:
                return False
            self.data.pop(key_hash, None)
            self._flush_locked()
            return True

    def snapshot(self) -> dict:
        """只读快照（admin/idempotency 端点用）——快照内条目均已过 TTL 校验。"""
        with self._lock:
            return {k: dict(v) for k, v in self._purged(self.data, self.ttl).items()}
