"""Bounded process-lifetime counters; never store prompts, responses or keys."""
import json
import threading
import time
from collections import deque

PATHS = {"/v1/chat/completions", "/v1/responses", "/v1/messages"}


class RequestMetrics:
    def __init__(self, usage_sink=None, model_error_sink=None, success_sink=None):
        self.lock = threading.Lock()
        self.usage_sink = usage_sink
        self.model_error_sink = model_error_sink
        self.success_sink = success_sink
        # 由管理后台注入：请求结束时回调，带该请求自己的 model/usage。
        self.request_sink = None
        self.started_at = int(time.time())
        self.in_flight = self.total = self.success = self.http_success = 0
        self.duration_sum = 0
        self.api_count = self.test_count = 0
        self.recent = deque(maxlen=100)
        self.credit_total = 0.0
        self.tokens_total = 0
        self.by_model = {}

    def begin(self):
        with self.lock:
            self.in_flight += 1

    @staticmethod
    def _read_tokens(usage):
        """兼容两套字段名读取 prompt/completion。

        Chat Completions 用 prompt_tokens/completion_tokens，Responses API
        用 input_tokens/output_tokens——只认前者会把 /v1/responses 的用量
        全部漏掉。
        """
        def pick(*names):
            for name in names:
                value = usage.get(name)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    return int(value)
            return None
        return pick("prompt_tokens", "input_tokens"), pick("completion_tokens", "output_tokens")

    def record_usage(self, model, usage):
        """累计真实扣费；仅保留聚合值，不保存请求或回复内容。"""
        if not isinstance(usage, dict):
            return
        prompt, completion = self._read_tokens(usage)
        tokens = 0
        for value in (prompt, completion):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                tokens += int(value)
        credit = usage.get("credit")
        if not isinstance(credit, (int, float)) or isinstance(credit, bool):
            credit = 0.0
        if tokens <= 0 and not credit:
            return
        name = str(model or "unknown")
        with self.lock:
            self.credit_total = round(self.credit_total + float(credit), 6)
            self.tokens_total += tokens
            row = self.by_model.setdefault(name, {"tokens": 0, "credit": 0.0, "requests": 0})
            row["tokens"] += tokens
            row["credit"] = round(row["credit"] + float(credit), 6)
            row["requests"] += 1
        if self.usage_sink is not None:
            try:
                self.usage_sink(name, usage)
            except Exception:
                pass

    def finish(self, path, source, status, ok, duration, outcome, model=None, usage=None):
        with self.lock:
            self.in_flight -= 1
            self.total += 1
            self.success += int(ok)
            self.http_success += int(status is not None and 200 <= status < 300)
            self.duration_sum += duration
            self.api_count += int(source == "api")
            self.test_count += int(source == "test")
            self.recent.appendleft({"time": int(time.time()), "path": path, "source": source,
                                    "status": status, "ok": ok, "duration_ms": round(duration), "outcome": outcome})
        if self.request_sink is not None:
            try:
                self.request_sink({"path": path, "source": source, "status": status, "ok": ok,
                                   "duration_ms": duration, "outcome": outcome,
                                   "model": model, "usage": usage})
            except Exception:
                pass

    def snapshot(self):
        with self.lock:
            return {"started_at": self.started_at, "completed": self.total, "in_flight": self.in_flight,
                    "succeeded": self.success, "failed": self.total - self.success,
                    "success_rate": round(100*self.success/self.total, 1) if self.total else None,
                    "http_success_rate": round(100*self.http_success/self.total, 1) if self.total else None,
                    "avg_duration_ms": round(self.duration_sum/self.total) if self.total else None,
                    "api_count": self.api_count, "test_count": self.test_count,
                    "credit_total": self.credit_total, "tokens_total": self.tokens_total,
                    "by_model": {k: dict(v) for k, v in self.by_model.items()},
                    "recent": list(self.recent)}


class MetricsMiddleware:
    def __init__(self, app, metrics, source="api"):
        self.app, self.metrics, self.source = app, metrics, source

    async def __call__(self, scope, receive, send):
        path = scope.get("path")
        if scope["type"] != "http" or scope["method"] != "POST" or path not in PATHS:
            return await self.app(scope, receive, send)
        start = time.monotonic()
        self.metrics.begin()
        status = None
        completed = failed = streaming = terminal = disconnected = False
        buffer = b""
        oversized_line = False
        seen_model = None
        requested_model = None
        seen_usage = None
        seen_error = None
        request_buffer = b""
        request_oversized = False

        def observe_request(message):
            """从请求体里取客户端请求的模型名。

            响应里的 `model` 字段可能缺失或与请求不一致（适配层改写、上游回显别名），
            而「这个账号能用这个模型」的判据应当以请求的模型为准。
            """
            nonlocal requested_model, request_buffer, request_oversized
            if message["type"] != "http.request":
                return
            chunk = message.get("body", b"")
            if request_oversized:
                return
            if len(request_buffer) + len(chunk) > 65536:
                request_oversized = True
                request_buffer = b""
                return
            request_buffer += chunk
            # 请求体通常一次到齐；也允许分片，尽量在拿到可解析内容时就抽出模型名。
            if not message.get("more_body", False) or requested_model is None:
                try:
                    doc = json.loads(request_buffer)
                except ValueError:
                    doc = None
                if isinstance(doc, dict) and isinstance(doc.get("model"), str):
                    requested_model = doc["model"]

        def event(line):
            nonlocal failed, terminal, seen_model, seen_usage, seen_error
            if not line.startswith(b"data:"):
                return
            payload = line[5:].strip()
            if payload == b"[DONE]":
                terminal = True
                return
            try:
                value = json.loads(payload)
            except ValueError:
                return
            if not isinstance(value, dict):
                return
            # 采集模型与真实扣费（usage.credit），用于模型倍率实测
            if isinstance(value.get("model"), str):
                seen_model = value["model"]
            if isinstance(value.get("usage"), dict):
                seen_usage = value["usage"]
            # 采集上游错误码：11102 = 该账号没有此模型
            code = value.get("code")
            if isinstance(code, int):
                seen_error = code
            response = value.get("response")
            if isinstance(response, dict) and isinstance(response.get("model"), str):
                seen_model = response["model"]
            if isinstance(response, dict) and isinstance(response.get("usage"), dict):
                seen_usage = response["usage"]
            message = value.get("message")
            if isinstance(message, dict):
                if isinstance(message.get("model"), str):
                    seen_model = message["model"]
                if isinstance(message.get("usage"), dict):
                    seen_usage = message["usage"]
            typ = value.get("type")
            if value.get("error") or typ in ("error", "response.failed", "response.incomplete"):
                failed = True
            if typ in ("response.completed", "message_stop"):
                terminal = True
            if isinstance(response, dict) and (response.get("error") or response.get("status") in ("failed", "incomplete")):
                failed = True

        async def observed_receive():
            nonlocal disconnected
            message = await receive()
            if message["type"] == "http.disconnect" and not completed:
                disconnected = True
            else:
                observe_request(message)
            return message

        async def observed_send(message):
            nonlocal status, streaming, completed, buffer, failed, oversized_line, seen_model, seen_usage
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = dict(message.get("headers", []))
                streaming = b"text/event-stream" in headers.get(b"content-type", b"").lower()
            elif message["type"] == "http.response.body":
                if streaming:
                    # Bound storage even if the upstream sends a huge unterminated line.
                    for fragment in message.get("body", b"").splitlines(keepends=True):
                        end = fragment.endswith(b"\n")
                        if not oversized_line and len(buffer) + len(fragment) <= 65536:
                            buffer += fragment
                        else:
                            oversized_line = True
                            buffer = b""
                        if end:
                            if not oversized_line:
                                event(buffer)
                            buffer = b""
                            oversized_line = False
                else:
                    # 非流式 JSON：整体解析一次以取得 model / usage
                    chunk = message.get("body", b"")
                    if len(buffer) + len(chunk) <= 65536:
                        buffer += chunk
                if not message.get("more_body", False):
                    if streaming and buffer:
                        event(buffer)
                    elif not streaming and buffer:
                        try:
                            doc = json.loads(buffer)
                        except ValueError:
                            doc = None
                        if isinstance(doc, dict):
                            if isinstance(doc.get("model"), str):
                                seen_model = doc["model"]
                            if isinstance(doc.get("usage"), dict):
                                seen_usage = doc["usage"]
                            detail = doc.get("detail")
                            if isinstance(detail, dict) and isinstance(detail.get("code"), int):
                                seen_error = detail["code"]
                            elif isinstance(doc.get("code"), int):
                                seen_error = doc["code"]
                    await send(message)
                    completed = True
                    return
            await send(message)

        try:
            await self.app(scope, observed_receive, observed_send)
        except BaseException:
            failed = True
            raise
        finally:
            http_ok = status is not None and 200 <= status < 300
            ok = http_ok and completed and not disconnected and not failed and (not streaming or terminal)
            outcome = "success" if ok else "stream_error" if failed and streaming else "interrupted" if not completed or disconnected or (streaming and not terminal) else "http_error"
            if seen_usage:
                self.metrics.record_usage(seen_model or requested_model, seen_usage)
            if self.metrics.model_error_sink is not None:
                try:
                    self.metrics.model_error_sink(seen_model or requested_model, seen_error)
                except Exception:
                    pass
            # 成功返回即证明该账号能用该模型（与是否带 usage 无关）。
            # 仅在一次完整、健康、未中断的响应之后计入。
            if ok and self.metrics.success_sink is not None:
                try:
                    self.metrics.success_sink(seen_model or requested_model)
                except Exception:
                    pass
            # model/usage 从本请求的局部变量传入，避免并发请求互相覆盖共享字段。
            self.metrics.finish(path, self.source, status, ok, (time.monotonic()-start)*1000, outcome,
                                model=seen_model or requested_model, usage=seen_usage)
