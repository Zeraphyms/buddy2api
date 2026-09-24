"""模型倍率：上游真实目录（含促销）+ 实测扣费。

上游 `GET /v2/enterprises/personal/models` 返回每个模型的官方倍率
（`credits` 字段，如 `"x0.79 credits"`）以及限时免费促销
（`modelPromotions`，`factor: 0` + 有效期）。

实测发现静态倍率并不可靠：目录可能滞后于实际计费。因此本模块同时累计
真实请求里 `usage.credit` 与 token 数，给出实测倍率供对照。
"""
import threading
import time
from datetime import datetime

import httpx

from core import converter

from . import modelregistry

MODELS_PATH = "/v2/enterprises/personal/models"
CATALOG_TTL = 3600
# credit 字段只有 0.01 精度：样本 token 太少时，单次量化误差就能让实测倍率
# 偏离数倍。低于该 token 量只展示原始累计，不给出实测倍率。
MEASURED_MIN_TOKENS = 2000



# 上游上下文长度有两种形态：数字，或 {defaultLength, supportedLengths}。
# 后者表示「多个可选档位」——例如默认 300K、可选到 1M。对外按上限
# 报告 context_length（OpenAI 惯例指的是上限），并单独给出默认值。
def context_window(value, ceiling=None):
    """归一化上下文长度：返回 (上限, 默认值, 档位列表)。"""
    default = None
    options = []
    if isinstance(value, dict):
        raw_default = value.get("defaultLength")
        if isinstance(raw_default, (int, float)) and not isinstance(raw_default, bool):
            default = int(raw_default)
        sizes = value.get("supportedLengths")
        if isinstance(sizes, list):
            for item in sizes:
                if isinstance(item, (int, float)) and not isinstance(item, bool) and item > 0:
                    options.append(int(item))
    elif isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        default = int(value)
    options = sorted(set(options))
    # 上限：显式给定优先，其次档位最大值，最后回落到默认值。
    top = None
    if isinstance(ceiling, (int, float)) and not isinstance(ceiling, bool) and ceiling > 0:
        top = int(ceiling)
    if options:
        top = max(top or 0, options[-1]) or None
    if top is None:
        top = default
    return top, default, options


def _parse_time(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def active_promotion(model_id, promotions, now=None):
    """返回当前对该模型生效的促销；没有则 None。"""
    now = now or datetime.now().astimezone()
    for promo in promotions or []:
        if not isinstance(promo, dict) or not promo.get("enabled"):
            continue
        ids = promo.get("modelIds") or []
        if model_id not in ids:
            continue
        schedule = promo.get("schedule") or {}
        start = _parse_time(schedule.get("validFrom"))
        end = _parse_time(schedule.get("validUntil"))
        if start and now < start:
            continue
        if end and now >= end:
            continue
        return promo
    return None


class ModelRates:
    """按区域缓存上游模型目录，并累计实测扣费。"""

    def __init__(self, store, clock=time.time, client_factory=None, ttl=CATALOG_TTL):
        self.store = store
        self.clock = clock
        self.client_factory = client_factory or (
            lambda: httpx.Client(timeout=20, follow_redirects=False)
        )
        self.ttl = ttl
        self.lock = threading.RLock()
        self.catalogs = {}
        with store.lock:
            store.data.setdefault("model_usage", {})
            store.save()

    # ---------------- 实测累计 ----------------

    def record(self, region, model, usage):
        """记录一次真实请求的扣费。usage 为上游返回的 usage 字典。"""
        if not region or not model or not isinstance(usage, dict):
            return
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        if not isinstance(prompt, (int, float)) or isinstance(prompt, bool):
            prompt = 0
        if not isinstance(completion, (int, float)) or isinstance(completion, bool):
            completion = 0
        tokens = prompt + completion
        if tokens <= 0:
            return
        credit = usage.get("credit")
        if not isinstance(credit, (int, float)) or isinstance(credit, bool):
            credit = 0
        key = f"{region}:{model}"
        with self.store.lock:
            bucket = self.store.data["model_usage"].setdefault(
                key, {"tokens": 0, "credit": 0.0, "samples": 0, "updated": 0}
            )
            bucket["tokens"] += int(tokens)
            bucket["credit"] = round(float(bucket["credit"]) + float(credit), 6)
            bucket["samples"] += 1
            bucket["updated"] = int(self.clock())
            self.store.save()

    def usage_rows(self):
        with self.store.lock:
            return {k: dict(v) for k, v in self.store.data["model_usage"].items()}

    def model_ids(self, region):
        """某区域的模型清单：以权威表为准（与客户端一致）。"""
        return list(modelregistry.model_ids(region))

    def catalog_ids(self, regions=("cn", "intl")):
        """两区目录的并集（仅作候选池，不代表某账号可用）。"""
        ids = []
        with self.lock:
            catalogs = {k: list(v.get("models") or []) for k, v in self.catalogs.items()}
        for region in regions:
            for m in catalogs.get(region, []):
                mid = m.get("id") if isinstance(m, dict) else None
                if mid and mid not in ids:
                    ids.append(mid)
        return ids

    # ---------------- 按账号的可用性 ----------------

    def _availability(self):
        with self.store.lock:
            return self.store.data.setdefault("account_models", {})

    def learn(self, aid, model, ok):
        """从真实请求学习某账号对某模型是否可用。

        上游对不可用模型返回 code 11102（model service info not found），
        对可用模型返回 200。据此可以精确判定「这个号能用哪些模型」。
        """
        if not aid or not model:
            return
        model = str(model)
        with self.store.lock:
            entry = self.store.data.setdefault("account_models", {}).setdefault(
                aid, {"ok": [], "no": [], "updated": 0})
            ok_list, no_list = entry.setdefault("ok", []), entry.setdefault("no", [])
            if ok:
                if model not in ok_list:
                    ok_list.append(model)
                if model in no_list:
                    no_list.remove(model)
            else:
                if model not in no_list:
                    no_list.append(model)
                if model in ok_list:
                    ok_list.remove(model)
            entry["updated"] = int(self.clock())
            self.store.save()

    def learned(self, aid):
        with self.store.lock:
            entry = (self.store.data.get("account_models") or {}).get(aid) or {}
            return list(entry.get("ok") or []), list(entry.get("no") or [])

    def models_for_account(self, aid, region):
        """某账号实际可用的模型。

        基线 = 该区域权威表（与客户端一致的清单）；再剔除实测返回 11102
        （不可用）的模型、叠加实测确认可用的模型。
        """
        ok, no = self.learned(aid)
        result = []
        for mid in modelregistry.model_ids(region):
            if mid not in no and mid not in result:
                result.append(mid)
        # 实测可用的模型（权威表外也可能存在）
        for mid in ok:
            if mid not in no and mid not in result:
                result.append(mid)
        return result

    def models_for_accounts(self, pairs):
        """多账号（轮转）时的并集。pairs 为 [(aid, region), ...]。"""
        result = []
        for aid, region in pairs:
            for mid in self.models_for_account(aid, region):
                if mid not in result:
                    result.append(mid)
        return result

    def probe_candidates(self, aid, region, limit=None):
        """待探测的候选模型：候选池 − 已确认可用 − 已确认不可用。

        候选池来自三处并集：本区域上游目录、两区目录并集、内置知识表。
        只靠上游目录会漏掉未登记但可调用的模型（如国际版 gpt-6-astra）。
        """
        ok, no = self.learned(aid)
        known = set(ok) | set(no)
        candidates = []
        for mid in modelregistry.model_ids(region):
            if mid not in known and mid not in candidates:
                candidates.append(mid)
        return candidates[:limit] if limit else candidates

    def probe(self, aid, cred, models):
        """对指定账号逐个探测模型可用性。返回 {model: bool}。

        每次探测都是一次真实的上游调用（max_tokens=1），会消耗极少量额度。
        """
        results = {}
        headers = cred.get_headers()
        domain = (headers or {}).get("X-Domain", "")
        url = f"{converter.backend_for(domain)}/v2/chat/completions"
        # 探测只判断「模型是否可用」，请求压到最轻。
        # 实测要点：
        #   - 国际版要求首条消息为 system，缺失整批返回 11128；
        #   - 部分模型（如 gpt-6-astra）拒绝过小的 max_tokens（11133
        #     model_param_invalid），故不显式传该参数，用服务端默认值——
        #     提示词极短，输出自然只有几个 token。
        is_intl = converter.is_intl_domain(domain)
        messages = [{"role": "user", "content": "hi"}]
        if is_intl:
            messages = [{"role": "system", "content": "You are helpful."},
                        {"role": "user", "content": "hi"}]
        body_base = {
            "stream": True,
            "messages": messages,
        }
        with self.client_factory() as client:
            for model in models:
                payload = dict(body_base, model=model)
                ok = False
                try:
                    with client.stream("POST", url, headers=headers, json=payload) as response:
                        if response.status_code == 200:
                            ok = True
                        else:
                            raw = b"".join(response.iter_bytes()).decode("utf-8", "replace")
                            try:
                                code = json.loads(raw).get("code")
                            except ValueError:
                                code = None
                            # 只有明确的 11102 才判定为「该账号没有此模型」；
                            # 其它错误（限流、超时）不下结论。
                            if code == 11102:
                                ok = False
                            else:
                                results[model] = None
                                continue
                except Exception:
                    results[model] = None
                    continue
                self.learn(aid, model, ok)
                results[model] = ok
        return results

    def reset_usage(self):
        with self.store.lock:
            self.store.data["model_usage"] = {}
            self.store.save()

    # ---------------- 上游目录 ----------------

    def _fetch(self, cred, domain):
        headers = cred.get_headers()
        url = f"{converter.backend_for(domain)}{MODELS_PATH}"
        with self.client_factory() as client:
            response = client.get(url, headers=headers)
        if response.status_code != 200:
            raise RuntimeError(f"上游返回 HTTP {response.status_code}")
        body = response.json()
        if not isinstance(body, dict) or body.get("code") != 0:
            raise RuntimeError(str(body.get("msg") or "上游返回异常")[:120])
        data = body.get("data") or {}
        models = data.get("models") or []
        promotions = data.get("modelPromotions") or []
        if not isinstance(models, list):
            raise RuntimeError("模型目录格式异常")
        return models, promotions

    def refresh(self, cred, domain):
        """拉取指定凭据所属区域的目录；返回该区域的快照。"""
        region = "intl" if converter.is_intl_domain(domain) else "cn"
        try:
            models, promotions = self._fetch(cred, domain)
            entry = {"fetched": int(self.clock()), "models": models,
                     "promotions": promotions, "error": None}
        except Exception as exc:  # 网络/解析失败不应影响服务
            with self.lock:
                previous = self.catalogs.get(region)
            entry = {"fetched": 0, "models": (previous or {}).get("models", []),
                     "promotions": (previous or {}).get("promotions", []),
                     "error": str(exc)[:200]}
        with self.lock:
            self.catalogs[region] = entry
        return entry

    def ensure(self, region, cred_provider):
        """目录缺失或过期时刷新一次。cred_provider(region) 返回凭据或 None。"""
        with self.lock:
            entry = self.catalogs.get(region)
        if entry and entry.get("models") and self.clock() - entry.get("fetched", 0) < self.ttl:
            return entry
        cred = cred_provider(region)
        if cred is None:
            return entry or {"fetched": 0, "models": [], "promotions": [], "error": "该区域没有可用账号"}
        domain = "www.workbuddy.ai" if region == "intl" else "www.workbuddy.cn"
        return self.refresh(cred, domain)

    def snapshot(self, fallback=None):
        """模型清单与参数以权威表为准，上游目录只补充促销与实测数据。

        上游目录接口返回的清单与客户端不一致（含已下线旧模型、缺少部分
        实际可用模型），参数也常与客户端不符。故清单与参数取自
        modelregistry（与客户端对齐），上游目录仅用于：
          - modelPromotions 促销（限时免费等）
          - 实测扣费累计（usage）
        """
        with self.lock:
            catalogs = {k: dict(v) for k, v in self.catalogs.items()}
        usage = self.usage_rows()
        rows = []
        for region in ("cn", "intl"):
            promotions = (catalogs.get(region) or {}).get("promotions") or []
            for mid in modelregistry.model_ids(region):
                spec = modelregistry.metadata(region, mid) or {}
                model = {}  # 权威表为准，上游规格不再采用
                official = spec.get("credit")
                promo = active_promotion(mid, promotions)
                effective = official
                promo_label = None
                promo_note = None
                if promo:
                    factor = (promo.get("discount") or {}).get("factor")
                    if isinstance(factor, (int, float)) and not isinstance(factor, bool):
                        effective = float(factor)
                    promo_label = (promo.get("badge") or {}).get("label")
                    promo_note = (promo.get("hover") or {}).get("textZh")
                bucket = usage.get(f"{region}:{mid}")
                measured = None
                if bucket and bucket.get("tokens", 0) >= MEASURED_MIN_TOKENS:
                    measured = round(bucket["credit"] / (bucket["tokens"] / 1000), 4)
                rows.append({
                    "region": region,
                    "id": mid,
                    "name": spec.get("name") or mid,
                    "official": official,
                    "effective": effective,
                    "promo": promo_label,
                    "promo_note": promo_note,
                    "measured": measured,
                    "tokens": (bucket or {}).get("tokens", 0),
                    "credit": (bucket or {}).get("credit", 0),
                    "samples": (bucket or {}).get("samples", 0),
                    "context_length": spec.get("context_length"),
                    "context_default_length": None,
                    "context_lengths": [],
                    "max_output_tokens": spec.get("max_output_tokens"),
                    "meta_source": "registry",
                    "is_default": False,
                    "supports_images": spec.get("supports_images"),
                    "supports_tools": spec.get("supports_tool_call"),
                    "description": spec.get("description"),
                    "supports_reasoning": spec.get("supports_reasoning"),
                    "only_reasoning": spec.get("supports_reasoning")
                        and not spec.get("can_disable_thinking"),
                    "can_disable_thinking": spec.get("can_disable_thinking"),
                    "default_effort": spec.get("default_effort"),
                    "default_summary": None,
                    "effort_options": list(spec.get("supported_efforts") or []),
                })
        rows.sort(key=lambda r: (r["region"], r["official"] is None, r["official"] or 0))
        return {
            "models": rows,
            "min_measured_tokens": MEASURED_MIN_TOKENS,
            "regions": {k: {"fetched": v.get("fetched", 0), "count": len(v.get("models") or []),
                            "promotions": len(v.get("promotions") or []), "error": v.get("error")}
                        for k, v in catalogs.items()},
        }
