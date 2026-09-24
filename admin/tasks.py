"""积分任务：列表查询 / 接受 / 领取奖励。

上游 growth 域接口（chatBase，即对话网关同源）：
  - GET  /v2/activity/growth/tasks                        全量任务列表
  - POST /v2/activity/growth/tasks/accept                 {"task_codes":[...]}
  - POST /v2/activity/growth/tasks/<code>/claim           领取奖励

语义：accept 是「报名」，不产生进度；进度由服务端行为事件点亮。
claim 仅在进度达标后可领，重复领返回 already_claimed（幂等，不算错误）。

领奖有两条通道：chat 域（对话网关）与 web 域（www.workbuddy.cn，
带 x-client-platform: web）。实测部分任务只有 web 域能领，故 chat 域
失败时自动降级到 web 域。
"""
import json

import httpx

from core import converter

TASKS_PATH = "/v2/activity/growth/tasks"
WEB_BASE = "https://www.workbuddy.cn"


class TaskError(Exception):
    """任务接口失败。message 面向面板展示。

    path_missing 表示「上游没有这个端点/任务路径」——区别于业务失败
    （未达标、已领取），只有这类错误才值得换域重试。
    """

    def __init__(self, message, status=None, code=None, path_missing=False):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.path_missing = path_missing


def _number(value, default=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)


def parse_progress(item):
    """进度可能是 {current,target} 对象，也可能是平铺字段。"""
    current = _number(item.get("current"))
    target = _number(item.get("target"))
    progress = item.get("progress")
    if isinstance(progress, dict):
        cur = _number(progress.get("current"))
        tgt = _number(progress.get("target"))
        if tgt > 0 or cur > 0:
            current, target = cur, tgt
    return current, target


# 国际版（workbuddy.ai）任务接口的字段名与国内版不同：任务码是 code 而非
# task_code，且不带进度与奖励字段。这里统一归一化，缺失值保持 0/空。
def parse_task(item):
    """把上游任务对象转成面板视图（兼容国内 / 国际两套字段）。"""
    if not isinstance(item, dict):
        return None
    code = item.get("task_code") or item.get("code")
    if not code:
        return None
    current, target = parse_progress(item)
    status = item.get("accept_status") or item.get("status") or ""
    claimed = status in ("claimed", "completed")
    # 达标且未领取 —— 本地推算，供面板高亮「可领取」。
    claimable = bool(target and current >= target and not claimed)
    return {
        "task_code": code,
        "title": item.get("title") or "",
        "description": item.get("description") or "",
        "task_desc": item.get("task_desc") or "",
        "credit": _number(item.get("reward_credit")),
        "energy": _number(item.get("reward_energy")),
        "reward_buddy": bool(item.get("reward_buddy")),
        "tag": item.get("tag") or "",
        "level_name": item.get("level_name") or "",
        "locked": bool(item.get("locked")),
        "current": current,
        "target": target,
        "accept_status": status,
        "status": item.get("status") or "",
        "claimable": claimable,
        "claimed": claimed,
    }


def parse_tasks(data):
    """解析任务列表响应 data.tasks[]。"""
    if not isinstance(data, dict):
        return []
    items = data.get("tasks")
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        task = parse_task(item)
        if task:
            out.append(task)
    return out


def web_headers(headers):
    """Web 域领奖请求头（对照浏览器成长中心实际请求）。"""
    h = {
        "Authorization": headers.get("Authorization", ""),
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": WEB_BASE,
        "Referer": WEB_BASE + "/profile/growth-center",
        "x-client-platform": "web",
    }
    for name in ("X-User-Id", "X-Enterprise-Id", "X-Tenant-Id", "X-Domain", "User-Agent"):
        value = headers.get(name)
        if value:
            h[name] = value
    return h


def _message_of(doc):
    """从上游响应里取可读的错误文案，并做常见错误的中文化。"""
    if not isinstance(doc, dict):
        return None
    raw = doc.get("msg") or doc.get("message")
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    lowered = text.lower()
    for key, value in TaskClient.MESSAGES.items():
        if key in lowered:
            return value
    return text[:120]


class TaskClient:
    """任务接口客户端。headers 为某账号的凭据头。"""

    def __init__(self, headers, client_factory=None):
        self.headers = headers or {}
        self.client_factory = client_factory or (
            lambda: httpx.Client(timeout=30, follow_redirects=False)
        )

    def _base(self):
        return converter.backend_for(self.headers.get("X-Domain"))

    # 上游业务错误码 → 面向面板的中文提示。未列出的原样透出。
    MESSAGES = {
        "task not completed": "任务尚未完成，达标后才能领取",
        "task not found": "上游没有这个任务（可能限定了客户端类型）",
        "already claimed": "该任务奖励已领取过",
    }

    def _call(self, method, url, headers, body=None):
        with self.client_factory() as client:
            response = client.request(method, url, headers=headers, json=body)
        try:
            doc = response.json()
        except ValueError:
            doc = None
        # 404 或「task not found」表示上游不认识该路径 —— 换域重试有意义。
        if response.status_code == 404:
            raise TaskError("上游没有这个接口路径", status=404, path_missing=True)
        if response.status_code != 200:
            raise TaskError(_message_of(doc) or ("上游返回 HTTP %s" % response.status_code),
                            status=response.status_code)
        if not isinstance(doc, dict) or doc.get("code") != 0:
            code = doc.get("code") if isinstance(doc, dict) else None
            raw = ((doc.get("msg") or "") if isinstance(doc, dict) else "").lower()
            missing = "not found" in raw
            msg = _message_of(doc) or "上游未接受该操作"
            raise TaskError(msg, code=code, path_missing=missing)
        return doc.get("data")

    def list_tasks(self):
        data = self._call("GET", self._base() + TASKS_PATH, self.headers)
        return parse_tasks(data)

    def accept(self, codes):
        """接受任务（报名）。幂等，可重复调用。"""
        body = {"task_codes": list(codes)}
        self._call("POST", self._base() + TASKS_PATH + "/accept", self.headers, body)
        return True

    def claim(self, task_code):
        """领取奖励。先 chat 域，失败降级 web 域。返回 (credit, energy)。"""
        path = TASKS_PATH + "/" + task_code + "/claim"
        try:
            return self._claim_result(
                self._call("POST", self._base() + path, self.headers)
            )
        except TaskError as exc:
            # 只有「上游不认识这个领奖路径」时才值得换域重试：这类错误
            # 说明 chat 域路由不到该端点，而 Web 域能（已实测）。其余业务
            # 错误（未达标、已领取）换域结果一致，直接透出。
            if exc.status and exc.status >= 500:
                raise
            if not exc.path_missing:
                raise
            web_path = "/activity" + TASKS_PATH[len("/v2/activity"):] + "/" + task_code + "/claim"
            return self._claim_result(
                self._call("POST", WEB_BASE + web_path, web_headers(self.headers))
            )

    # ---------------- 猫猫 / 外观 ----------------

    def buddy_agreement(self):
        """同意 Buddy 领养协议（幂等）。body 必须带 agree=true，否则上游拒绝。"""
        self._call("POST", self._base() + "/activity/growth/buddy/agreement",
                   self.headers, {"agree": True})
        return True

    def buddy_first(self):
        """领养第一只 Buddy。门槛未达标时上游返回业务错误。"""
        self._call("POST", self._base() + "/activity/growth/buddy/first", self.headers)
        return True

    def set_appearance_theme(self, theme_key):
        """设置外观主题。"""
        body = {"kind": "theme", "resource_key": theme_key}
        h = {
            "Authorization": self.headers.get("Authorization", ""),
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
            "User-Agent": "WorkBuddy/5.5.6 WorkBuddy/5.5.6 CLI/2.137.1",
            "X-Product": "SaaS",
        }
        if self.headers.get("X-User-Id"):
            h["X-User-Id"] = self.headers["X-User-Id"]
        self._call("POST", self._base() + "/v2/user-asset/appearance/set", h, body)
        return True

    @staticmethod
    def _claim_result(data):
        if not isinstance(data, dict):
            return 0, 0
        if data.get("already_claimed"):
            return 0, 0
        return _number(data.get("credit")), _number(data.get("energy"))
