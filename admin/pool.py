"""Account-scoped billing, refresh, daily check-in and request routing."""
import asyncio
import hashlib
import json
import math
import os
import threading
import time
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from core import converter

from . import autotask, events
from . import tasks
from .taskcenter import TaskCenter

REQUEST_CREDENTIAL = ContextVar("pool_credential", default=None)
# 当前请求实际使用的账号区域（cn / intl），供倍率统计归属
REQUEST_REGION = ContextVar("pool_region", default=None)
# 当前请求实际使用的账号 id，供按账号学习模型可用性
REQUEST_ACCOUNT = ContextVar("pool_account", default=None)
CN = timezone(timedelta(hours=8))
BILLING = "https://www.codebuddy.cn/v2/billing/meter/"

# 请求体读取上限，仅用于挡住明显异常的请求；长上下文会话（历史图片每轮以
# base64 重发）会自然撑到几十 MiB，真正的超限判断交给上游。
MAX_REQUEST_BYTES = int(os.environ.get("WB2A_MAX_REQUEST_MB", "256")) * 1024 * 1024

# 积分（余额）后台刷新周期：tick() 每 60s 跑一次，距上次成功查询超过该值才查一次，
# 因此它是「最小间隔」而非精确定时。改这里即可调整查积分的频率。
CREDITS_SYNC_SECONDS = 1800
# 积分数据的「新鲜度窗口」：超过该时长未更新则视为陈旧——
# 面板显示 stale 标记，且不再据此判定账号已耗尽。
# 必须 >= CREDITS_SYNC_SECONDS，否则每次同步前数据就被判陈旧。
CREDITS_FRESH_SECONDS = 3600


def number(value):
    if isinstance(value, bool):
        raise ValueError("invalid number")
    try:
        n = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("invalid number")
    if not n.is_finite():
        raise ValueError("invalid number")
    return max(Decimal(0), n)


def summarize_packages(packages):
    remain = Decimal(0)
    size = Decimal(0)
    for p in packages:
        # Precise values carry fractional credit balances; cycle values are spendable now.
        prefix = "Cycle" if any(k in p for k in ["CycleCapacityRemainPrecise", "CycleCapacityRemain"]) else ""
        def field(name, default=None):
            return p.get(prefix + name + "Precise", p.get(prefix + name, default))
        r = number(field("CapacityRemain"))
        capacity = number(field("CapacitySize", r))
        remain += min(r, capacity) if capacity else r
        size += capacity
    return {"remaining": float(remain), "capacity": float(size), "used": float(max(Decimal(0), size - remain)), "packages": len(packages)}


class BillingError(Exception):
    def __init__(self, message, status=502, code=None):
        super().__init__(message)
        self.status, self.code = status, code


class AccountPool:
    def __init__(self, store, clock=time.time, client_factory=None):
        self.store, self.clock = store, clock
        self.client_factory = client_factory or (lambda: httpx.Client(timeout=20, follow_redirects=False))
        self.locks = {}
        self.cursor = 0
        self.jobs = asyncio.Lock()
        self.last_sync = 0
        with store.lock:
            store.data.setdefault("pool", {"routing": "round_robin", "auto_checkin": True, "checkin_time": "09:00"})
            store.data.setdefault("account_status", {})
            store.data.setdefault("session_bindings", {})
            store.save()

    def operation_lock(self, aid):
        """账号操作锁。

        用可重入锁：队列执行时已持有该账号的锁，内部的动作（auto_task /
        accept_tasks）还会再取一次；若是普通 Lock，同线程重入会死锁。
        RLock 保证账号间互斥语义不变，又允许同线程嵌套获取。
        """
        with self.store.lock:
            return self.locks.setdefault(aid, threading.RLock())

    def snapshot(self, aid):
        with self.store.lock:
            item = self.store.data["accounts"].get(aid)
            if not item:
                raise HTTPException(404, "账号不存在")
            return dict(item), dict(self.store.data["account_status"].get(aid, {}))

    def update(self, aid, **values):
        with self.store.lock:
            if aid in self.store.data["accounts"]:
                self.store.data["account_status"].setdefault(aid, {}).update(values)
                self.store.save()

    def state(self, row):
        status = self.store.data["account_status"].get(row["id"], {})
        if not row["enabled"]:
            return "paused"
        if row["status"] == "invalid" or status.get("auth_invalid"):
            return "invalid"
        if status.get("cooldown_until", 0) > self.clock():
            return "cooling"
        if status.get("credits_updated", 0) > self.clock() - CREDITS_FRESH_SECONDS and status.get("remaining") == 0 and status.get("packages", 0) > 0:
            return "exhausted"
        return "available"

    def rows(self, rows):
        today = datetime.fromtimestamp(self.clock(), CN).strftime("%Y-%m-%d")
        for row in rows:
            status = self.store.data["account_status"].get(row["id"], {})
            row.update({"pool_state": self.state(row), "remaining": status.get("remaining"), "credits_updated": status.get("credits_updated"),
                        "credits_stale": status.get("credits_updated", 0) < self.clock() - CREDITS_FRESH_SECONDS,
                        "today_checked_in": status.get("checkin_date") == today, "cooldown_until": status.get("cooldown_until", 0),
                        "last_error": status.get("last_error"), "token_refreshed": status.get("token_refreshed"),
                        "request_count": status.get("request_count", 0)})
        return rows

    def select(self, affinity_key=None):
        with self.store.lock:
            candidates = [row for row in self.store.account_rows() if self.state(row) == "available"]
            if self.store.data["pool"]["routing"] == "manual":
                candidates = [row for row in candidates if row["id"] == self.store.data["active"]]
            if not candidates:
                raise HTTPException(503, "暂无可用账号：请检查暂停、积分、冷却或登录状态")
            now = self.clock()
            bindings = self.store.data["session_bindings"]
            for key in list(bindings):
                if bindings[key].get("expires", 0) <= now:
                    del bindings[key]
            bound = bindings.get(affinity_key, {}).get("account_id")
            if bound in {row["id"] for row in candidates}:
                aid = bound
            else:
                aid = candidates[self.cursor % len(candidates)]["id"]
                self.cursor += 1
            if affinity_key:
                if affinity_key not in bindings and len(bindings) >= 4096:
                    del bindings[min(bindings, key=lambda key: bindings[key]["expires"])]
                bindings[affinity_key] = {"account_id": aid, "expires": now + 86400}
            item = self.store.data["accounts"][aid]
            return aid, self.store.manager_for(aid, item)

    def record(self, aid, status=200):
        with self.store.lock:
            if aid not in self.store.data["accounts"]:
                return
            data = self.store.data["account_status"].setdefault(aid, {})
            data["request_count"] = data.get("request_count", 0) + 1
            data["last_request"] = int(self.clock())
            if status in (401, 403):
                data.update(cooldown_until=self.clock() + 300, last_error="上游认证或访问被拒绝，已冷却 5 分钟")
            elif status in (402, 429):
                # 限流冷却：实测多为频率上限（额度仍在），5 分钟足够恢复。
                data.update(cooldown_until=self.clock() + 300, last_error="上游额度或频率限制，已冷却 5 分钟")
            self.store.save()

    def billing(self, client, headers, path, body=None):
        # 积分/签到服务与对话网关同源：国内 .cn、国际 .ai。
        # 国际凭据打到 .cn 积分服务会被 401 拒绝，反之亦然。
        base = converter.billing_base_for(headers.get("X-Domain"))
        response = client.post(base + path, headers=headers, json=body or {})
        if response.status_code != 200:
            raise BillingError("积分服务请求失败", response.status_code)
        try:
            d = response.json()
        except ValueError:
            raise BillingError("积分服务返回格式异常")
        if not isinstance(d, dict) or d.get("code") != 0:
            raise BillingError("上游未接受该操作，请检查账号或稍后重试", code=d.get("code") if isinstance(d, dict) else None)
        return d.get("data")

    def credits(self, client, headers):
        packages = []
        now = datetime.fromtimestamp(self.clock(), CN)
        for page in range(1, 101):
            d = self.billing(client, headers, "get-user-resource", {"PageNumber": page, "PageSize": 100, "ProductCode": "p_tcaca", "Status": [0, 3], "PackageEndTimeRangeBegin": now.strftime("%Y-%m-%d %H:%M:%S"), "PackageEndTimeRangeEnd": "2126-01-01 00:00:00"})
            try:
                data = d["Response"]["Data"]
                current = data["Accounts"]
                total = int(data["TotalCount"])
                if not isinstance(current, list) or total < 0:
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                raise BillingError("积分数据格式异常，上次余额已保留")
            packages.extend(current)
            if len(packages) >= total:
                return summarize_packages(packages)
            if not current:
                break
        raise BillingError("积分包分页不完整，上次余额已保留")

    def checkin_status(self, client, headers):
        try:
            d = self.billing(client, headers, "checkin-activity-status")
        except BillingError as e:
            if e.status not in (404, 405):
                raise
            d = self.billing(client, headers, "checkin-status")
        if not isinstance(d, dict):
            raise BillingError("签到状态格式异常")
        def flag(snake, camel):
            v = d.get(snake, d.get(camel))
            if v not in (True, False, 0, 1, "true", "false", "1", "0"):
                raise BillingError("签到状态缺少必要字段")
            return v in (True, 1, "true", "1")
        return {"active": flag("active", "active"), "checked": flag("today_checked_in", "todayCheckedIn")}

    # ---------------- 积分任务 ----------------

    def _task_client(self, aid, item):
        """构造带该账号凭据的任务客户端。"""
        manager = self.store.manager_for(aid, item)
        return tasks.TaskClient(manager.get_headers())

    def list_tasks(self, aid):
        """查询账号的全量任务（进度 / 奖励 / 状态）。"""
        with self.operation_lock(aid):
            item, _ = self.snapshot(aid)
            try:
                rows = self._task_client(aid, item).list_tasks()
            except tasks.TaskError as exc:
                return {"id": aid, "ok": False, "message": exc.message, "tasks": []}
            return {"id": aid, "ok": True, "tasks": rows}

    def accept_tasks(self, aid, codes=None):
        """接受任务（报名）。codes 为空时接受全部未接受的任务。"""
        with self.operation_lock(aid):
            item, _ = self.snapshot(aid)
            try:
                client = self._task_client(aid, item)
                if not codes:
                    rows = client.list_tasks()
                    codes = [t["task_code"] for t in rows
                             if t.get("accept_status") not in ("accepted", "claimed")
                             and not t.get("locked")]
                if not codes:
                    return {"id": aid, "ok": True, "message": "没有需要接受的任务", "accepted": []}
                client.accept(codes)
            except tasks.TaskError as exc:
                return {"id": aid, "ok": False, "message": exc.message, "accepted": []}
            return {"id": aid, "ok": True, "message": "已接受 %d 个任务" % len(codes), "accepted": list(codes)}

    def claim_task(self, aid, code):
        """领取单个任务奖励。"""
        with self.operation_lock(aid):
            item, _ = self.snapshot(aid)
            try:
                credit, energy = self._task_client(aid, item).claim(code)
            except tasks.TaskError as exc:
                return {"id": aid, "ok": False, "message": exc.message}
            if not credit and not energy:
                return {"id": aid, "ok": True, "message": "该任务已领取过", "credit": 0, "energy": 0}
            # 有新增积分则刷新余额展示
            if credit:
                self.update(aid, last_error=None)
            return {"id": aid, "ok": True,
                    "message": "已领取 +%d 积分 +%d 能量" % (credit, energy),
                    "credit": credit, "energy": energy}

    def _auto_runner(self, aid, item):
        """构造「一键完成」执行器。"""
        manager = self.store.manager_for(aid, item)
        headers = manager.get_headers()
        summary = {}
        try:
            summary = manager.summary() or {}
        except Exception:
            summary = {}
        uid = summary.get("uid") or headers.get("X-User-Id", "")
        nickname = summary.get("nickname") or ""
        client = tasks.TaskClient(headers)
        reporter = events.EventReporter(headers, uid=uid, nickname=nickname)
        return autotask.AutoTaskRunner(client, reporter, client.list_tasks)

    def auto_task(self, aid, code):
        """对一个任务执行「一键完成」。"""
        with self.operation_lock(aid):
            item, _ = self.snapshot(aid)
            if not item.get("enabled"):
                return {"id": aid, "ok": False, "message": "账号已暂停"}
            entry = autotask.ACTION_INDEX.get(code)
            if entry is None:
                return {"id": aid, "ok": False, "message": "该任务暂不支持一键完成"}
            desc, method = entry
            runner = self._auto_runner(aid, item)
            try:
                ok, message = getattr(runner, method)()
            except (tasks.TaskError, events.EventError) as exc:
                return {"id": aid, "ok": False, "message": exc.message, "code": code}
            except Exception as exc:
                return {"id": aid, "ok": False, "message": "执行失败：%s" % str(exc)[:100], "code": code}
            return {"id": aid, "ok": ok, "message": message, "code": code}

    def auto_tasks_all(self, aid):
        """对一个账号执行全部「一键完成」动作，然后领取可领奖励。"""
        with self.operation_lock(aid):
            item, _ = self.snapshot(aid)
            if not item.get("enabled"):
                return {"id": aid, "ok": False, "message": "账号已暂停", "results": []}
            runner = self._auto_runner(aid, item)
            results = []
            for code, desc, method in autotask.ACTIONS:
                try:
                    ok, message = getattr(runner, method)()
                except (tasks.TaskError, events.EventError) as exc:
                    ok, message = False, exc.message
                except Exception as exc:
                    ok, message = False, "执行失败：%s" % str(exc)[:100]
                results.append({"code": code, "ok": ok, "message": message})
            # 动作跑完再统一领奖（进度可能刚被点亮）
            claimed, credit = [], 0
            try:
                client = tasks.TaskClient(item and self.store.manager_for(aid, item).get_headers())
                for row in client.list_tasks():
                    if not row.get("claimable"):
                        continue
                    try:
                        c, _ = client.claim(row["task_code"])
                    except tasks.TaskError:
                        continue
                    if c:
                        claimed.append(row["task_code"])
                        credit += c
            except Exception:
                pass
            done = sum(1 for r in results if r["ok"])
            return {"id": aid, "ok": True,
                    "message": "已执行 %d/%d 个动作，领取 %d 个奖励（+%d 积分）" % (
                        done, len(results), len(claimed), credit),
                    "results": results, "claimed": claimed, "credit": credit}

    def claim_all_tasks(self, aid):
        """领取该账号所有可领取的任务。"""
        with self.operation_lock(aid):
            item, _ = self.snapshot(aid)
            try:
                client = self._task_client(aid, item)
                rows = client.list_tasks()
            except tasks.TaskError as exc:
                return {"id": aid, "ok": False, "message": exc.message, "claimed": []}
            done, total_credit, total_energy = [], 0, 0
            for row in rows:
                if not row.get("claimable"):
                    continue
                try:
                    credit, energy = client.claim(row["task_code"])
                except tasks.TaskError:
                    continue
                if credit or energy:
                    done.append(row["task_code"])
                    total_credit += credit
                    total_energy += energy
            if not done:
                return {"id": aid, "ok": True, "message": "没有可领取的任务", "claimed": []}
            return {"id": aid, "ok": True,
                    "message": "已领取 %d 个任务：+%d 积分 +%d 能量" % (len(done), total_credit, total_energy),
                    "claimed": done, "credit": total_credit, "energy": total_energy}

    # ---------------- 任务中心 ----------------

    def task_center(self):
        """惰性构造任务中心（依赖 pool 自身）。"""
        center = getattr(self, "_task_center", None)
        if center is None:
            center = TaskCenter(self)
            self._task_center = center
        return center

    def scan_tasks(self):
        """扫描全部账号的待办任务（只读）。"""
        return self.task_center().scan()

    def queue_tasks(self, concurrency=1):
        """启动执行队列。"""
        ok, message, total = self.task_center().start(concurrency)
        return {"ok": ok, "message": message, "total": total}

    def queue_status(self):
        """队列实时状态。"""
        return self.task_center().state.snapshot()

    def clear_cooldown(self, aid):
        """手动取消冷却：清掉冷却时间与错误标记，让账号立即回到可用池。

        适用场景：上游限流已恢复但本地冷却尚未到期。
        """
        with self.operation_lock(aid):
            item, _ = self.snapshot(aid)
            if not item.get("enabled"):
                return {"id": aid, "ok": False, "message": "账号已暂停，请先恢复"}
            self.update(aid, cooldown_until=0, last_error=None)
            return {"id": aid, "ok": True, "message": "已取消冷却，账号恢复可用"}

    def operate(self, aid, action):
        with self.operation_lock(aid):
            item, previous = self.snapshot(aid)
            if not item["enabled"]:
                return {"id": aid, "ok": False, "message": "账号已暂停，未执行操作"}
            manager = self.store.manager_for(aid, item)
            try:
                if action == "refresh":
                    with manager._lock:
                        manager._refresh()
                    self.update(aid, token_refreshed=int(self.clock()), auth_invalid=False, cooldown_until=0, last_error=None)
                    return {"id": aid, "ok": True, "message": "登录凭据已刷新"}
                headers = manager.get_headers()
                today = datetime.fromtimestamp(self.clock(), CN).strftime("%Y-%m-%d")
                with self.client_factory() as client:
                    message = "积分状态已更新"
                    checkin_error = None
                    try:
                        ci = self.checkin_status(client, headers)
                        if ci["checked"]:
                            self.update(aid, checkin_date=today)
                        if action == "checkin":
                            if ci["checked"]:
                                message = "今日已签到，无需重复签到"
                            elif not ci["active"]:
                                message = "当前没有可参与的签到活动"
                            else:
                                self.billing(client, headers, "daily-checkin")
                                ci = self.checkin_status(client, headers)
                                if not ci["checked"]:
                                    raise BillingError("签到已提交，但上游尚未确认；请刷新状态")
                                self.update(aid, checkin_date=today)
                                message = "签到成功"
                    except (BillingError, httpx.HTTPError) as e:
                        if action == "checkin":
                            raise
                        checkin_error = "签到状态暂时无法查询"
                    data = self.credits(client, headers)
                    # A successful balance lookup is proof of current authentication.
                    self.update(aid, **data, credits_updated=int(self.clock()), auth_invalid=False, last_error=checkin_error)
                    if data["remaining"] > 0 and action == "checkin":
                        self.update(aid, cooldown_until=0)
                    return {"id": aid, "ok": True, "message": message, "remaining": data["remaining"]}
            except BillingError as e:
                self.update(aid, last_error=str(e))
                if e.status in (401, 403):
                    self.update(aid, cooldown_until=self.clock()+300)
                return {"id": aid, "ok": False, "message": str(e)}
            except (httpx.HTTPError, OSError, ValueError, RuntimeError, KeyError):
                # Do not expose raw refresh responses or bearer credentials.
                message = "操作失败：请检查登录凭据或网络，必要时重新授权"
                self.update(aid, last_error=message)
                return {"id": aid, "ok": False, "message": message}

    async def batch(self, action):
        if self.jobs.locked():
            raise HTTPException(409, "已有批量任务进行中，请稍后刷新")
        async with self.jobs:
            with self.store.lock:
                ids = [aid for aid, item in self.store.data["accounts"].items() if item["enabled"]]
            semaphore = asyncio.Semaphore(3)
            async def run(aid):
                async with semaphore:
                    try:
                        return await asyncio.to_thread(self.operate, aid, action)
                    except HTTPException:
                        return {"id": aid, "ok": False, "message": "账号已移除"}
            return {"results": await asyncio.gather(*(run(aid) for aid in ids))}

    async def tick(self):
        now = datetime.fromtimestamp(self.clock(), CN)
        with self.store.lock:
            cfg = dict(self.store.data["pool"])
            due = [aid for aid, item in self.store.data["accounts"].items() if item["enabled"] and self.store.data["account_status"].get(aid, {}).get("checkin_date") != now.strftime("%Y-%m-%d") and self.store.data["account_status"].get(aid, {}).get("auto_attempt", 0) < self.clock()-1800]
        if cfg["auto_checkin"] and now.strftime("%H:%M") >= cfg["checkin_time"]:
            for aid in due:
                self.update(aid, auto_attempt=int(self.clock()))
                try:
                    await asyncio.to_thread(self.operate, aid, "checkin")
                except HTTPException:
                    pass
        if self.clock() - self.last_sync > CREDITS_SYNC_SECONDS and not self.jobs.locked():
            await self.batch("status")
            self.last_sync = self.clock()


def request_affinity(headers, body):
    """Persist only a hash of caller/model/session identity, never raw prompts or keys."""
    if not isinstance(body, dict):
        return None
    identity = None
    for name in (b"x-session-id", b"session_id", b"x-conversation-id"):
        if headers.get(name):
            identity = [name.decode(), headers[name].decode(errors="replace")]
            break
    if identity is None:
        for name in ("prompt_cache_key", "conversation_id", "session_id"):
            if body.get(name):
                identity = [name, body[name]]
                break
    if identity is None:
        messages = body.get("messages", body.get("input", []))
        if isinstance(messages, str) and messages:
            identity = ["first_user", messages]
        elif isinstance(messages, list):
            for message in messages:
                if isinstance(message, dict) and message.get("role") == "user":
                    identity = ["first_user", message.get("content", "")]
                    break
    if identity is None:
        return None
    caller = headers.get(b"authorization") or headers.get(b"x-api-key", b"")
    value = [caller.decode(errors="replace"), body.get("model", "auto"), identity]
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class PoolMiddleware:
    def __init__(self, app, pool):
        self.app, self.pool = app, pool

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] not in {"/v1/chat/completions", "/v1/responses", "/v1/messages", "/v1/messages/count_tokens"}:
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        try:
            self.pool.store.check_api(headers.get(b"authorization", b"").decode(), headers.get(b"x-api-key", b"").decode())
            raw = bytearray()
            oversized = False
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                if not oversized:
                    raw.extend(message.get("body", b""))
                    if len(raw) > MAX_REQUEST_BYTES:
                        # 先排空剩余分片再报错，避免上传中途断开导致客户端重试风暴。
                        oversized = True
                        raw = bytearray()
                if not message.get("more_body", False):
                    break
            if oversized:
                raise HTTPException(413, f"请求体超过 {MAX_REQUEST_BYTES // 1024 // 1024} MiB")
            raw = bytes(raw)
            try:
                body = json.loads(raw) if raw else {}
            except ValueError:
                body = {}
            affinity_key = request_affinity(headers, body)
            original_receive = receive
            delivered = False
            async def replay_receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": raw, "more_body": False}
                return await original_receive()
            receive = replay_receive
            aid, manager = self.pool.select(affinity_key)
            # Refresh before entering a stream; retry another eligible account only
            # if credential preparation fails, never replay a partially emitted response.
            try:
                await asyncio.to_thread(manager.get_headers)
            except Exception:
                self.pool.update(aid, cooldown_until=self.pool.clock()+300, last_error="凭据暂不可用，已冷却 5 分钟")
                aid, manager = self.pool.select(affinity_key)
                await asyncio.to_thread(manager.get_headers)
        except HTTPException as e:
            return await JSONResponse({"detail": e.detail}, e.status_code)(scope, receive, send)
        except Exception:
            return await JSONResponse({"detail": "账号凭据暂不可用，请刷新凭据或重新授权"}, 503)(scope, receive, send)
        token = REQUEST_CREDENTIAL.set(manager)
        try:
            domain = (manager.get_headers() or {}).get("X-Domain", "")
        except Exception:
            domain = ""
        # 故意不 reset：外层 MetricsMiddleware 在 PoolMiddleware 返回之后才读取该值，
        # 提前 reset 会把所有用量都误记到默认区域。每个请求都是一个独立 asyncio Task，
        # 其 context 是创建时的副本，因此这里的赋值不会泄漏到其它请求。
        REQUEST_REGION.set("intl" if converter.is_intl_domain(domain) else "cn")
        REQUEST_ACCOUNT.set(aid)
        status = 200
        buffer = b""
        async def observe(message):
            nonlocal status, buffer
            if message["type"] == "http.response.start":
                status = message["status"]
            if message["type"] == "http.response.body":
                buffer += message.get("body", b"")
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if line.startswith(b"data:"):
                        try:
                            obj = json.loads(line[5:])
                            err = obj.get("error") or (obj.get("response") or {}).get("error") or {}
                            code = err.get("code") if isinstance(err, dict) else None
                            if isinstance(code, str) and code.isdigit():
                                code = int(code)
                            if code in (401, 402, 403, 429):
                                status = code
                        except (ValueError, AttributeError):
                            pass
                if len(buffer) > 65536:
                    buffer = b""
            await send(message)
        try:
            await self.app(scope, receive, observe)
        finally:
            REQUEST_CREDENTIAL.reset(token)
            self.pool.record(aid, status)
