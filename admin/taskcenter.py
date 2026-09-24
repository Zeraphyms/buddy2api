"""任务中心：全账号任务扫描 + 执行队列。

解决「不知道哪些账号有哪些任务没做」与「只能逐账号点」两个问题：
  - 扫描：并发拉取每个账号的成长任务，汇总「未完成且可自动化」的待办清单（只读）
  - 队列：把待办按账号分组排队执行——账号内串行（复用 per-account 锁，
    与单任务/一键完成互斥），账号间受并发上限约束。队列状态可轮询。

只覆盖成长任务（growth）；开学季等需要独立指纹体系的活动不在此列。
"""
import threading
import time

from . import autotask, tasks

ITEM_GAP = 1.05  # 队列项之间的节流（与上报间隔同口径）
MAX_CONCURRENCY = 4


def is_pending(row):
    """任务是否「未完成且可自动化」。

    已领取、上游锁定、超出可自动化范围的都不进待办。已达标未领的仍入队，
    队列跑完会自动领奖。
    """
    if row.get("claimed"):
        return False
    if row.get("locked"):
        return False
    return row.get("task_code") in autotask.ACTION_INDEX


class QueueState:
    """队列运行状态。seq 每轮 +1，前端只渲染自己启动的那一轮。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.started_at = 0.0
        self.items = []
        self.concurrency = 1
        self.seq = 0

    def snapshot(self):
        with self.lock:
            items = [dict(it) for it in self.items]
            return {
                "running": self.running,
                "started_at": self.started_at,
                "seq": self.seq,
                "concurrency": self.concurrency,
                "total": len(items),
                "done": sum(1 for i in items if i["status"] == "done"),
                "failed": sum(1 for i in items if i["status"] == "error"),
                "items": items,
            }


class TaskCenter:
    """扫描与排队执行。pool 提供账号、锁与动作执行入口。"""

    def __init__(self, pool):
        self.pool = pool
        self.state = QueueState()

    # ---------------- 扫描 ----------------

    def _account_tasks(self, aid, item):
        """取单账号任务列表；失败返回 (None, 错误信息)。"""
        try:
            client = self.pool._task_client(aid, item)
            return client.list_tasks(), None
        except Exception as exc:
            return None, str(getattr(exc, "message", exc))[:120]

    def scan(self):
        """并发扫描全部启用账号，汇总待办。只读，不执行任何动作。"""
        with self.pool.store.lock:
            accounts = [(aid, dict(item))
                        for aid, item in self.pool.store.data["accounts"].items()
                        if item.get("enabled")]
        results = [None] * len(accounts)
        threads = []

        def worker(index, aid, item):
            rows, error = self._account_tasks(aid, item)
            results[index] = {"id": aid, "name": item.get("name") or aid,
                              "tasks": rows or [], "error": error}

        for index, (aid, item) in enumerate(accounts):
            thread = threading.Thread(target=worker, args=(index, aid, item), daemon=True)
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join(timeout=90)

        pending_total = 0
        out = []
        for entry in results:
            if entry is None:
                continue
            rows = entry["tasks"]
            pending = [r for r in rows if is_pending(r)]
            claimable = [r for r in rows if r.get("claimable")]
            pending_total += len(pending)
            out.append({
                "id": entry["id"],
                "name": entry["name"],
                "total": len(rows),
                "claimed": sum(1 for r in rows if r.get("claimed")),
                "claimable": len(claimable),
                "pending": [{"code": r["task_code"], "title": r.get("title") or "",
                             "current": r.get("current", 0), "target": r.get("target", 0),
                             "credit": r.get("credit", 0)} for r in pending],
                "error": entry["error"],
            })
        out.sort(key=lambda x: -len(x["pending"]))
        return {"accounts": out, "pending_count": pending_total}

    # ---------------- 队列 ----------------

    def start(self, concurrency=1):
        """扫描待办并启动执行队列。返回 (ok, message, total)。"""
        try:
            concurrency = max(1, min(int(concurrency), MAX_CONCURRENCY))
        except (TypeError, ValueError):
            concurrency = 1
        with self.state.lock:
            if self.state.running:
                return False, "队列正在执行中（可在任务中心查看进度）", 0

        scan = self.scan()
        items = []
        for account in scan["accounts"]:
            for row in account["pending"]:
                items.append({"uid": account["id"], "nickname": account["name"],
                              "code": row["code"], "status": "pending", "message": ""})
        if not items:
            return True, "全部账号没有待办任务", 0

        with self.state.lock:
            self.state.running = True
            self.state.started_at = time.time()
            self.state.items = items
            self.state.concurrency = concurrency
            self.state.seq += 1
        thread = threading.Thread(target=self._run, args=(concurrency,), daemon=True)
        thread.start()
        return True, "队列已启动：%d 项待办" % len(items), len(items)

    def _mark(self, uid, code, status, message):
        with self.state.lock:
            for item in self.state.items:
                if (item["uid"] == uid and item["code"] == code
                        and item["status"] in ("pending", "running")):
                    item["status"], item["message"] = status, message
                    return

    def _run(self, concurrency):
        """执行主体：账号间并发（信号量），账号内串行（复用账号锁）。"""
        try:
            with self.state.lock:
                by_account = {}
                for item in self.state.items:
                    by_account.setdefault(item["uid"], []).append(item["code"])
            semaphore = threading.Semaphore(concurrency)
            threads = []

            def run_account(aid, codes):
                with semaphore:
                    lock = self.pool.operation_lock(aid)
                    if not lock.acquire(blocking=False):
                        for code in codes:
                            self._mark(aid, code, "skipped", "该账号有其它任务动作在执行，跳过")
                        return
                    try:
                        # 先报名未接受的任务：上游对 not_accepted 不计数
                        try:
                            self.pool.accept_tasks(aid, None)
                            time.sleep(ITEM_GAP)
                        except Exception:
                            pass
                        for code in codes:
                            self._mark(aid, code, "running", "")
                            try:
                                result = self.pool.auto_task(aid, code)
                                if result.get("ok"):
                                    self._mark(aid, code, "done", result.get("message") or "")
                                else:
                                    self._mark(aid, code, "error", result.get("message") or "")
                            except Exception as exc:
                                self._mark(aid, code, "error", str(exc)[:120])
                            time.sleep(ITEM_GAP)
                    finally:
                        lock.release()

            for aid, codes in by_account.items():
                thread = threading.Thread(target=run_account, args=(aid, codes), daemon=True)
                thread.start()
                threads.append(thread)
            for thread in threads:
                thread.join()
        finally:
            with self.state.lock:
                self.state.running = False
