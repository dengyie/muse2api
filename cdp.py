"""极简 Chrome DevTools Protocol 客户端（仅依赖 websocket-client / requests）。"""
from __future__ import annotations

import json
import time

import threading

import requests
import websocket


class CDPError(RuntimeError):
    pass


class CDP:
    def __init__(self, ws_url: str, timeout: float = 90.0, max_size: int = 256 << 20):
        self.ws = websocket.create_connection(ws_url, timeout=timeout, max_size=max_size)
        self.timeout = timeout
        self._id = 0
        self._lock = threading.Lock()
        # patch27：CDP 事件回调（screencastFrame 等），pump_ws 里分发。
        self._event_cb = None
        # patch27：事件泵并发支持——pump_ws 消费事件帧时，把 send() 的响应帧
        # 回填此队列；send() 等待前先捞队列，两线程共享同一条 ws 不互吞消息。
        import queue
        self._pending = queue.Queue()

    def on_event(self, cb):
        """注册 CDP 事件回调 cb(ev: dict)；传 None 取消。"""
        self._event_cb = cb

    def send(self, method: str, params: dict | None = None, timeout: float | None = None):
        with self._lock:
            self._id += 1
            mid = self._id
            self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            deadline = time.time() + (timeout or self.timeout)
            while time.time() < deadline:
                # patch27：事件泵回填的响应帧优先出队（泵在跑时消息不在这里丢）
                try:
                    msg = self._pending.get_nowait()
                except Exception:  # noqa: BLE001 — 队列空
                    msg = None
                if msg is not None:
                    if msg.get("id") != mid:
                        continue                       # 迟到的旧响应，丢弃
                    if "error" in msg:
                        raise CDPError(f"{method}: {msg['error']}")
                    return msg
                try:
                    msg = json.loads(self.ws.recv())
                except websocket.WebSocketTimeoutException:
                    time.sleep(0.05)  # patch13(F6)：防忙等空转
                    continue
                except Exception as exc:  # noqa: BLE001
                    # patch13(F6)：断连（1006/帧超限）必须立即失败，不能吞掉后
                    # 忙等到满 deadline 抛 TimeoutError（烧 600s + 假超时 +
                    # 破坏 MuseGenerationError 契约）。
                    if not getattr(self.ws, "connected", True):
                        raise CDPError(f"{method}: CDP 连接已断开: {str(exc)[:80]}")
                    time.sleep(0.05)
                    continue
                if msg.get("id") == mid:
                    if "error" in msg:
                        raise CDPError(f"{method}: {msg['error']}")
                    return msg
                if msg.get("id") is not None:          # 泵在跑时它负责入队；兜底丢弃
                    continue
            raise TimeoutError(f"CDP {method} 超时")

    def js(self, expr: str, await_promise: bool = False, timeout: float | None = None):
        r = self.send("Runtime.evaluate",
                      {"expression": expr, "returnByValue": True,
                       "awaitPromise": await_promise}, timeout)
        res = r.get("result", {}).get("result", {})
        if "value" in res:
            return res["value"]
        return r.get("result")

    def pump(self, seconds: float, on_event=None, sock_timeout: float = 2.0):
        """在 seconds 秒内持续读取事件，交给回调处理。"""
        end = time.time() + seconds
        self.ws.settimeout(sock_timeout)
        while time.time() < end:
            try:
                ev = json.loads(self.ws.recv())
            except Exception:  # noqa: BLE001
                continue
            if on_event and "method" in ev:
                on_event(ev)

    def pump_ws(self, on_frame=None, stop=None, sock_timeout: float = 0.5):
        """patch27：长驻事件泵——把 CDP 事件推给 on_event / on_frame。

        与主线程 self.send 并发：本方法只消费**事件帧**（带 method 的消息），
        send 的响应帧（带 id）原样回填队列，由 send() 消费——否则泵会把
        输入转发/抓 cookie 的响应吞掉，send 全部假超时。
        screencastFrame 收到后必须回 ackPageFrame，否则 Chromium 停止推帧。
        """
        self.ws.settimeout(sock_timeout)
        while stop is None or not stop.is_set():
            try:
                ev = json.loads(self.ws.recv())
            except websocket.WebSocketTimeoutException:
                continue
            except Exception:                          # noqa: BLE001
                if not getattr(self.ws, "connected", True):
                    raise CDPError("事件泵：CDP 连接已断开")
                continue
            if ev.get("id") is not None:
                # send() 的响应：回填队列（send 等待时先捞这里），泵不消费
                self._pending.put_nowait(ev)
                continue
            method = ev.get("method")
            if not method:
                continue
            if method == "Page.screencastFrame" and on_frame:
                try:
                    on_frame(ev["params"])
                finally:
                    try:
                        self.send("Page.screencastFrameAck",
                                  {"sessionId": ev["params"]["sessionId"]},
                                  timeout=5)
                    except Exception:                  # noqa: BLE001
                        pass
            cb = self._event_cb
            if cb:
                cb(ev)

    def close(self):
        with self._lock:
            try:
                self.ws.close()
            except Exception:  # noqa: BLE001
                pass


def http_json(url: str, timeout: float = 5.0):
    return requests.get(url, timeout=timeout).json()
