"""按客户端 Key 的每日对话次数配额。

按东八区自然日计数，跨天自动清零。计数保存在管理数据文件的
"quota_daily" 字段，随其他状态一起持久化，重启不丢。

默认上限 1000 次/天，可用 .env 的 DAILY_REQUEST_LIMIT 调整；
设为 0 表示关闭限额。
"""
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta

from fastapi import HTTPException

CN = timezone(timedelta(hours=8))
DEFAULT_LIMIT = 1000
DEFAULT_BLOCK_LIMIT = 10


class DailyQuota:
    """按 Key 统计当天对话请求数，超限抛出 429。"""

    def __init__(self, store, limit=DEFAULT_LIMIT, clock=time.time,
                 block_limit=DEFAULT_BLOCK_LIMIT):
        self.store = store
        self.limit = int(limit)
        self.block_limit = int(block_limit)
        self.clock = clock
        self.lock = threading.Lock()
        self.recent_blocks = defaultdict(deque)

    def _today(self):
        return datetime.fromtimestamp(self.clock(), CN).strftime("%Y-%m-%d")

    def used(self, token):
        """某 Key 今天已用次数（不计数）。"""
        with self.lock:
            day = self._today()
            entry = (self.store.data.get("quota_daily") or {}).get(day) or {}
            return int(entry.get(token) or 0)

    def check_and_count(self, token):
        """校验并累加一次。超限抛 429，不累加。"""
        if self.limit <= 0:
            return
        with self.lock:
            day = self._today()
            daily = self.store.data.setdefault("quota_daily", {})
            # 只保留当天，避免无限增长
            for old in [k for k in daily if k != day]:
                daily.pop(old, None)
            entry = daily.setdefault(day, {})
            used = int(entry.get(token) or 0)
            if used >= self.limit:
                raise HTTPException(
                    status_code=429,
                    detail={
                        "error": {
                            "message": f"今日请求已达上限（{self.limit} 次），明天再试",
                            "type": "rate_limit_error",
                        }
                    },
                )
            entry[token] = used + 1
            self.store.save()

    def blocked(self, token):
        """审核拒绝后不计入配额；一小时内连续拦截过多则临时封禁。"""
        with self.lock:
            day = self._today()
            entry = (self.store.data.get("quota_daily") or {}).get(day) or {}
            used = int(entry.get(token) or 0)
            if used > 0:
                entry[token] = used - 1
                self.store.save()
            now = self.clock()
            hits = self.recent_blocks[token]
            while hits and hits[0] < now - 3600:
                hits.popleft()
            hits.append(now)
            if self.block_limit and len(hits) >= self.block_limit:
                raise HTTPException(
                    429,
                    {
                        "error": {
                            "message": "安全审核拦截次数过多，请 10 分钟后再试",
                            "type": "rate_limit_error",
                        }
                    },
                )

