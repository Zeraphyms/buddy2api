"""模型元数据兜底：上游目录缺失字段时，从 models.dev 补齐。

上游目录是首选来源；但有些模型（国际版 deepseek、新上线模型）在上游
目录里缺少上下文长度、输出上限或能力标记，models.dev 作为外部公开数据
源补上。查询按模型 basename 匹配（忽略 provider 前缀），命中多个时取
上下文更长的那个——同一 slug 在不同 provider 下规格不同，取大值更实用。

结果缓存到本地文件，默认 24 小时刷新一次；网络不可用时静默沿用旧缓存，
不影响网关启动与请求转发。
"""
import json
import threading
import time

import httpx

SOURCE = "https://models.dev/api.json"
CACHE_TTL = 24 * 3600


def _basename(model_id):
    """去掉 vendor 前缀与常见的区域/版本后缀，得到可比对的 slug。"""
    text = str(model_id or "").strip().lower()
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    if ":" in text:
        text = text.rsplit(":", 1)[-1]
    return text


class ModelMeta:
    """models.dev 元数据缓存；只读外部数据，写入独立文件。"""

    def __init__(self, cache_path, clock=time.time, client_factory=None, ttl=CACHE_TTL):
        self.cache_path = cache_path
        self.clock = clock
        self.client_factory = client_factory or (
            lambda: httpx.Client(timeout=30, follow_redirects=True)
        )
        self.ttl = ttl
        self.lock = threading.RLock()
        self.index = {}
        self.fetched = 0
        self.error = None
        self._load()

    def _load(self):
        try:
            with open(self.cache_path, encoding="utf-8") as handle:
                doc = json.load(handle)
            if isinstance(doc, dict):
                index = doc.get("index")
                if isinstance(index, dict):
                    self.index = index
                    self.fetched = int(doc.get("fetched") or 0)
        except (OSError, ValueError):
            pass  # 无缓存或缓存损坏：等首次联网刷新

    def _save(self):
        try:
            tmp = str(self.cache_path) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump({"fetched": self.fetched, "index": self.index}, handle)
            import os
            os.replace(tmp, self.cache_path)
        except OSError:
            pass

    def _fetch(self):
        with self.client_factory() as client:
            response = client.get(SOURCE, headers={"User-Agent": "wb2api-panel/1.0"})
        if response.status_code != 200:
            raise RuntimeError("models.dev 返回 HTTP %s" % response.status_code)
        return response.json()

    @staticmethod
    def _reasoning_options(model):
        """从 reasoning_options 提取 (档位列表, 是否可关)。"""
        values, can_off = [], False
        for opt in model.get("reasoning_options") or []:
            if not isinstance(opt, dict):
                continue
            if opt.get("type") == "effort":
                raw = opt.get("values")
                if isinstance(raw, list):
                    values = [str(v) for v in raw if isinstance(v, str)]
            elif opt.get("type") == "toggle":
                can_off = True
        return tuple(values), can_off

    @classmethod
    def _build(cls, doc):
        """把 models.dev 文档压成 {slug: meta}。

        同名模型会出现在多个 provider 下且规格不一（例如 glm-5.3 有五种
        档位组合），因此对档位取「出现次数最多的组合」——众数比任取一个
        更能代表该模型的通行能力。
        """
        index = {}
        votes = {}
        if not isinstance(doc, dict):
            return index
        for provider in doc.values():
            if not isinstance(provider, dict):
                continue
            models = provider.get("models")
            if not isinstance(models, dict):
                continue
            for mid, model in models.items():
                if not isinstance(model, dict):
                    continue
                slug = _basename(mid)
                if not slug:
                    continue
                limit = model.get("limit") or {}
                context = limit.get("context") if isinstance(limit, dict) else None
                output = limit.get("output") if isinstance(limit, dict) else None
                # 少数 provider 把 output 填成与上下文相同（明显失真），丢弃。
                if isinstance(output, (int, float)) and isinstance(context, (int, float)) and output >= context:
                    output = None
                entry = {
                    "name": model.get("name") or slug,
                    "context_length": context if isinstance(context, (int, float)) else None,
                    "max_output_tokens": output,
                    "supports_reasoning": bool(model.get("reasoning")),
                    "supports_tools": bool(model.get("tool_call")),
                    "supports_images": bool(
                        "image" in (((model.get("modalities") or {}).get("input")) or [])
                    ),
                    "description": model.get("description"),
                    "source": "models.dev",
                }
                options, can_off = cls._reasoning_options(model)
                # 只统计「明确给出档位」的条目：不少 provider 的
                # reasoning_options 为空，若让空值参与投票会把有效档位投没。
                if options:
                    votes.setdefault(slug, {}).setdefault((options, can_off), 0)
                    votes[slug][(options, can_off)] += 1
                previous = index.get(slug)
                if previous is None or (
                    (entry["context_length"] or 0) > (previous.get("context_length") or 0)
                ):
                    index[slug] = entry
        # 档位以众数为准，写回该 slug 的条目。
        for slug, counter in votes.items():
            entry = index.get(slug)
            if entry is None or not counter:
                continue
            (options, can_off), _ = max(counter.items(), key=lambda kv: kv[1])
            entry["effort_options"] = list(options)
            entry["can_disable_thinking"] = can_off
        return index

    def refresh(self, force=False):
        """按 TTL 刷新；失败时保留旧索引并记录错误。"""
        with self.lock:
            fresh = self.index and self.clock() - self.fetched < self.ttl
        if fresh and not force:
            return
        try:
            index = self._build(self._fetch())
            if not index:
                raise RuntimeError("models.dev 数据为空")
        except Exception as exc:
            with self.lock:
                self.error = str(exc)[:200]
            return
        with self.lock:
            self.index = index
            self.fetched = int(self.clock())
            self.error = None
            self._save()

    def lookup(self, model_id):
        slug = _basename(model_id)
        with self.lock:
            return self.index.get(slug)

    def stats(self):
        with self.lock:
            return {"entries": len(self.index), "fetched": self.fetched, "error": self.error}
