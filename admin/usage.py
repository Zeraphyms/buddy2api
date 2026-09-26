"""按账号 / 模型 / 域 / 时间窗累计 token 用量。

只保留聚合计数：不存请求体、回复内容或密钥。数据随 state.json 持久化，
结构为 {bucket: {tokens, prompt, completion, credit, requests, failed, duration_ms}}，
bucket 形如 "account:<aid>" / "model:<region>:<model>" / "realm:<region>"。

另有按本地日期（东八区）索引的 "usage_daily"，供「今日用量」使用；
不用 usage_series 计算，因为 series 只保留最近 MAX_SERIES_POINTS 个点，
跨不过一整天。
"""
import threading
import time
from datetime import datetime, timedelta, timezone

MAX_SERIES_POINTS = 240
MAX_DAILY_ENTRIES = 60
CN = timezone(timedelta(hours=8))


def _number(value, default=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return value


class UsageStats:
    """用量聚合。写入走内存 + 落盘节流，读取只做快照。"""

    def __init__(self, store, clock=time.time, persist_interval=20):
        self.store = store
        self.clock = clock
        self.persist_interval = persist_interval
        self.lock = threading.RLock()
        self._dirty = False
        self._last_persist = 0.0
        with store.lock:
            store.data.setdefault("usage_stats", {})
            store.data.setdefault("usage_series", [])
            store.data.setdefault("usage_totals", {})
            store.data.setdefault("usage_daily", {})

    def record(self, aid, region, model, usage, failed=False, duration_ms=None):
        """记一次真实请求的用量。usage 为上游返回的 usage 字典。"""
        if not isinstance(usage, dict):
            usage = {}
        # 兼容 Chat（prompt_tokens/completion_tokens）与 Responses
        # （input_tokens/output_tokens）两套字段名。
        def pick(*names):
            for name in names:
                value = usage.get(name)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    return int(value)
            return 0
        prompt = pick("prompt_tokens", "input_tokens")
        completion = pick("completion_tokens", "output_tokens")
        credit = round(float(_number(usage.get("credit"))), 6)
        tokens = prompt + completion
        if tokens <= 0 and not credit and not failed:
            return
        region = region or "cn"
        model = str(model or "unknown")
        duration = int(_number(duration_ms)) if duration_ms else 0

        with self.store.lock:
            buckets = self.store.data["usage_stats"]
            targets = []
            if aid:
                targets.append("account:%s" % aid)
            targets.append("model:%s:%s" % (region, model))
            targets.append("realm:%s" % region)
            for key in targets:
                row = buckets.setdefault(key, {})
                if key.startswith("account:"):
                    row["region"] = region
                row["prompt"] = row.get("prompt", 0) + prompt
                row["completion"] = row.get("completion", 0) + completion
                row["tokens"] = row.get("tokens", 0) + tokens
                row["credit"] = round(row.get("credit", 0.0) + credit, 6)
                row["requests"] = row.get("requests", 0) + 1
                row["failed"] = row.get("failed", 0) + (1 if failed else 0)
                if duration:
                    row["duration_ms"] = row.get("duration_ms", 0) + duration
                    row["timed"] = row.get("timed", 0) + 1
            totals = self.store.data["usage_totals"]
            totals["prompt"] = totals.get("prompt", 0) + prompt
            totals["completion"] = totals.get("completion", 0) + completion
            totals["tokens"] = totals.get("tokens", 0) + tokens
            totals["credit"] = round(totals.get("credit", 0.0) + credit, 6)
            totals["requests"] = totals.get("requests", 0) + 1
            totals["failed"] = totals.get("failed", 0) + (1 if failed else 0)
            if duration:
                totals["duration_ms"] = totals.get("duration_ms", 0) + duration
                totals["timed"] = totals.get("timed", 0) + 1

            series = self.store.data["usage_series"]
            minute = int(self.clock() // 60) * 60
            if series and series[-1].get("t") == minute:
                point = series[-1]
            else:
                point = {"t": minute, "prompt": 0, "completion": 0, "tokens": 0, "requests": 0}
                series.append(point)
                del series[:-MAX_SERIES_POINTS]
            point["prompt"] += prompt
            point["completion"] += completion
            point["tokens"] += tokens
            point["requests"] += 1

            # 按本地日期（东八区）累计，供「今日用量」使用。
            daily = self.store.data["usage_daily"]
            day = datetime.fromtimestamp(self.clock(), CN).strftime("%Y-%m-%d")
            entry = daily.setdefault(day, {
                "prompt": 0, "completion": 0, "tokens": 0,
                "requests": 0, "failed": 0, "credit": 0.0,
            })
            entry["prompt"] += prompt
            entry["completion"] += completion
            entry["tokens"] += tokens
            entry["requests"] += 1
            if failed:
                entry["failed"] += 1
            entry["credit"] = round(entry["credit"] + credit, 6)
            if len(daily) > MAX_DAILY_ENTRIES:
                for stale in sorted(daily)[:-MAX_DAILY_ENTRIES]:
                    daily.pop(stale, None)
            self._dirty = True
            if self.clock() - self._last_persist >= self.persist_interval:
                self.store.save()
                self._last_persist = self.clock()
                self._dirty = False

    def flush(self):
        """把未落盘的累计写回 state.json（进程退出前调用）。"""
        with self.store.lock:
            if self._dirty:
                self.store.save()
                self._dirty = False

    def reset(self):
        with self.store.lock:
            self.store.data["usage_stats"] = {}
            self.store.data["usage_series"] = []
            self.store.data["usage_totals"] = {}
            self.store.data["usage_daily"] = {}
            self.store.save()

    @staticmethod
    def _merge_legacy_models(buckets, legacy_usage):
        """把旧 model_usage 并入 model bucket（tokens/credit/samples 守恒）。"""
        for key, row in legacy_usage.items():
            if ":" not in key:
                continue
            tokens = int(_number(row.get("tokens")))
            if tokens <= 0:
                continue
            bucket = buckets.setdefault("model:%s" % key, {})
            bucket["legacy_tokens"] = bucket.get("legacy_tokens", 0) + tokens
            bucket["legacy_credit"] = round(bucket.get("legacy_credit", 0.0) + float(_number(row.get("credit"))), 6)
            bucket["legacy_samples"] = bucket.get("legacy_samples", 0) + int(_number(row.get("samples")))
            bucket["tokens"] = bucket.get("tokens", 0) + tokens
            bucket["requests"] = bucket.get("requests", 0) + int(_number(row.get("samples")))
            bucket["credit"] = round(bucket.get("credit", 0.0) + float(_number(row.get("credit"))), 6)

    @staticmethod
    def _merge_legacy_accounts(buckets, legacy_accounts):
        """把旧 request_count 并入 account bucket（只有请求数，token 无从还原）。"""
        for aid, row in legacy_accounts.items():
            count = int(_number(row.get("request_count")))
            if count <= 0:
                continue
            bucket = buckets.setdefault("account:%s" % aid, {})
            bucket["legacy_requests"] = bucket.get("legacy_requests", 0) + count
            bucket["requests"] = bucket.get("requests", 0) + count

    def snapshot(self, labels=None, regions=None):
        """聚合快照：本模块实时统计 + 并入改动前遗留的历史累计。

        labels 为 {aid: 显示名}。历史数据来自两个旧来源：
        - `model_usage`（region:model 维度，只有 tokens/credit/samples，无 prompt 拆分）
        - `account_status.request_count`（账号维度，只有请求数，无 token）
        两者都标记 legacy，与本模块启用后的实时统计区分。
        """
        labels = labels or {}
        regions = regions or {}
        with self.store.lock:
            buckets = {k: dict(v) for k, v in self.store.data["usage_stats"].items()}
            series = [dict(p) for p in self.store.data["usage_series"]]
            totals = dict(self.store.data["usage_totals"])
            daily = {k: dict(v) for k, v in (self.store.data.get("usage_daily") or {}).items()}
            legacy_usage = {k: dict(v) for k, v in (self.store.data.get("model_usage") or {}).items()}
            legacy_accounts = {k: dict(v) for k, v in (self.store.data.get("account_status") or {}).items()}
        accounts, models, realms = [], [], []
        self._merge_legacy_models(buckets, legacy_usage)
        self._merge_legacy_accounts(buckets, legacy_accounts)
        for key, row in buckets.items():
            if ":" not in key:
                continue
            kind, rest = key.split(":", 1)
            item = dict(row)
            item["id"] = rest
            if kind == "account":
                item["name"] = labels.get(rest) or rest
                item["detail"] = rest
                if not item.get("region"):
                    item["region"] = regions.get(rest)
                accounts.append(item)
            elif kind == "model":
                region, _, model = rest.partition(":")
                item["region"], item["model"] = region, model
                item["name"] = ("%s:%s" % (region, model)) if region else model
                models.append(item)
            elif kind == "realm":
                item["realm"] = rest
                item["name"] = rest
                realms.append(item)
        # realm 维度没有独立历史来源，按模型行的 region 汇总补齐。
        realm_index = {r["realm"]: r for r in realms}
        for row in models:
            name = row.get("region")
            if not name:
                continue
            bucket = realm_index.get(name)
            if bucket is None:
                bucket = {"realm": name, "name": name}
                realm_index[name] = bucket
                realms.append(bucket)
            bucket["tokens"] = bucket.get("tokens", 0) + row.get("tokens", 0)
            bucket["requests"] = bucket.get("requests", 0) + row.get("requests", 0)
            bucket["prompt"] = bucket.get("prompt", 0) + row.get("prompt", 0)
            bucket["completion"] = bucket.get("completion", 0) + row.get("completion", 0)
            bucket["credit"] = round(bucket.get("credit", 0.0) + row.get("credit", 0.0), 6)
            bucket["legacy_tokens"] = bucket.get("legacy_tokens", 0) + row.get("legacy_tokens", 0)
        accounts.sort(key=lambda r: r.get("tokens", 0), reverse=True)
        models.sort(key=lambda r: r.get("tokens", 0), reverse=True)
        realms.sort(key=lambda r: r.get("tokens", 0), reverse=True)
        # 总量：实时累计 + 并入的历史（历史只有 tokens/credit，无 prompt 拆分）。
        legacy_tokens = sum(row.get("legacy_tokens", 0) for row in models)
        legacy_credit = sum(row.get("legacy_credit", 0.0) for row in models)
        today = datetime.fromtimestamp(self.clock(), CN).strftime("%Y-%m-%d")
        today_row = daily.get(today) or {
            "prompt": 0, "completion": 0, "tokens": 0,
            "requests": 0, "failed": 0, "credit": 0.0,
        }
        return {
            "totals": {
                "requests": totals.get("requests", 0),
                "failed": totals.get("failed", 0),
                "prompt": totals.get("prompt", 0),
                "completion": totals.get("completion", 0),
                "tokens": totals.get("tokens", 0) + legacy_tokens,
                "credit": round(totals.get("credit", 0.0) + legacy_credit, 6),
                "avg_duration_ms": round(totals["duration_ms"] / totals["timed"])
                if totals.get("timed") else None,
            },
            "legacy": {"tokens": legacy_tokens, "credit": legacy_credit},
            "today": dict(today_row, date=today),
            "accounts": accounts,
            "models": models,
            "realms": realms,
            "series": series,
        }
