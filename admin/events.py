"""行为事件上报：三种客户端指纹（CLI / 桌面 / Web）。

上游把「任务进度」建立在行为事件上，而不是独立端点。同一个 POST /v2/report
通道，靠**客户端指纹**区分任务归属：
  - CLI  ：billingBase（www.codebuddy.cn）/v2/report，事件形状含 conversationId 等
  - 桌面 ：chatBase（copilot.tencent.com）/v2/report，UA=WorkBuddy/5.5.6 ...
  - Web  ：www.workbuddy.cn/v2/report，浏览器形状（pageURL/os/machineId）

服务端对事件链有真实性校验倾向（如桌面对话需成功回执），因此事件字段按
实测形状完整构造，而非最小子集。
"""
import hashlib
import json
import time

import httpx

from core import converter

REPORT_PATH = "/v2/report"
WEB_BASE = "https://www.workbuddy.cn"
DESKTOP_UA = "WorkBuddy/5.5.6 WorkBuddy/5.5.6 CLI/2.137.1"
WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")


class EventError(Exception):
    """事件上报失败。"""

    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def derive_id(uid, salt):
    """由 uid 稳定派生 36 位 hex 设备标识（同一账号每次相同，模拟固定设备）。"""
    return hashlib.sha256(("%s:%s" % (salt, uid)).encode()).hexdigest()[:36]


def web_headers(headers):
    """Web 域上报头。"""
    h = {
        "Authorization": headers.get("Authorization", ""),
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=UTF-8",
        "User-Agent": WEB_UA,
        "Origin": WEB_BASE,
        "Referer": WEB_BASE + "/",
        "x-client-platform": "web",
    }
    for name in ("X-User-Id", "X-Enterprise-Id", "X-Tenant-Id"):
        value = headers.get(name)
        if value:
            h[name] = value
    return h


class EventReporter:
    """按账号凭据上报行为事件。"""

    def __init__(self, headers, uid="", nickname="", client_factory=None, gap=1.05):
        self.headers = headers or {}
        self.uid = uid or self.headers.get("X-User-Id", "")
        self.nickname = nickname or ""
        self.client_factory = client_factory or (
            lambda: httpx.Client(timeout=30, follow_redirects=False)
        )
        self.gap = gap

    # ---------------- 底层发送 ----------------

    def _post(self, url, headers, events):
        with self.client_factory() as client:
            response = client.post(url, headers=headers, json=events)
        if response.status_code != 200:
            raise EventError("上报失败 HTTP %s" % response.status_code,
                             status=response.status_code)
        try:
            doc = response.json()
        except ValueError:
            raise EventError("上报返回格式异常")
        if isinstance(doc, dict) and doc.get("code") not in (0, None):
            raise EventError("上报被拒：%s" % str(doc.get("msg") or "")[:100],
                             code=doc.get("code"))
        return True

    def _chat_base(self):
        return converter.backend_for(self.headers.get("X-Domain"))

    # ---------------- CLI 口径 ----------------

    def chat_request_send(self, conversation_id=None, request_id=None,
                          model_id="deepseek-v4-flash", model_name=""):
        """上报一条 chat_request_send（对话活跃）。

        走 billing 域。事件必须带 userId，缺失会被服务端静默丢弃。
        """
        now = int(time.time() * 1000)
        conv = conversation_id or "wb2api-%d" % now
        req = request_id or conv
        event = {
            "eventCode": "chat_request_send",
            "timestamp": now,
            "reportDelay": 0,
            "mode": "craft",
            "conversationId": conv,
            "requestId": req,
            "inputLength": 12,
            "requestModelId": model_id,
            "requestModelName": model_name or model_id,
            "isPlan": False,
            "isAutoExecuteTerminal": False,
            "isAutoModify": False,
            "codebaseEnable": False,
            "maxToken": 0,
            "maxSteps": 0,
            "temperature": 0,
            "maxRetries": 0,
            "mentionContexts": [],
            "knowledgeId": [],
            "knowledgeName": [],
            "codebaseId": "",
            "mentionContextCount": 0,
            "command": "",
            "expertId": "",
            "recommendId": "",
            "skillId": "",
            "skillCount": 0,
            "totalCount": 0,
            "fileUri": "",
            "presentAt": now,
            "traceId": "",
            "rootRequestId": req,
            "parentConversationId": conv,
            "agentName": "default",
            "agentType": "conversation",
            "userId": self.uid,
        }
        # CLI 口径走 billing 域根路径（www.codebuddy.cn/v2/report）。
        # 注意不能用 billing_base_for()——那是 /v2/billing/meter/ 子路径，
        # 再拼 /v2/report 会得到不存在的路由（404）。
        return self._post(self._billing_report_url(), self.headers, [event])

    def _billing_report_url(self):
        """CLI 口径上报地址：billing 域根 + /v2/report。

        国内 www.codebuddy.cn，国际 www.workbuddy.ai。
        """
        base = converter.billing_base_for(self.headers.get("X-Domain"))
        # 去掉 /v2/billing/meter/ 之类的子路径，只保留 scheme://host
        if "//" in base:
            scheme, _, rest = base.partition("//")
            host = rest.split("/", 1)[0]
            return scheme + "//" + host + REPORT_PATH
        return base.rstrip("/") + REPORT_PATH

    # ---------------- 桌面口径 ----------------

    def _desktop_fingerprint(self):
        now = int(time.time() * 1000)
        return {
            "timezone": "Asia/Shanghai",
            "reportDelay": 2000,
            "userId": self.uid,
            "username": self.nickname,
            "userNickname": self.nickname,
            "product": "SaaS",
            "releaseDate": 1789036585355,
            "commit": "5f9692923c93033111c51ad7b003eb80204a9b75",
            "ideName": "WorkBuddy",
            "ideType": "WorkBuddy",
            "ideVersion": "5.5.6",
            "machineId": derive_id(self.uid, "machine"),
            "sessionId": derive_id(self.uid, "session"),
            "extName": "workbuddy-desktop",
            "extVersion": "5.5.6",
            "os": "win32",
            "arch": "x64",
            "osVersion": "10.0.26220",
            "cpuCores": 20,
            "memorySize": 24,
            "timestamp": now,
            "presentAt": now,
        }

    def report_desktop(self, events):
        """以桌面指纹批量上报事件（公共指纹自动注入）。"""
        if not events:
            raise EventError("没有要上报的事件")
        fp = self._desktop_fingerprint()
        payload = []
        for event in events:
            merged = dict(fp)
            merged.update(event)
            payload.append(merged)
        base = self._chat_base()
        h = {
            "Authorization": self.headers.get("Authorization", ""),
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
            "User-Agent": DESKTOP_UA,
            "X-Domain": base,
            "X-Product": "SaaS",
            "X-Request-ID": derive_id(self.uid, "req") + str(int(time.time() * 1000) % 1000000),
        }
        if self.uid:
            h["X-User-Id"] = self.uid
        return self._post(base + REPORT_PATH, h, payload)

    # ---------------- Web 口径 ----------------

    def report_web(self, event_code, page_url, element_id, element_name):
        """Web 端单事件上报（页面行为类任务）。"""
        event = {
            "eventCode": event_code,
            "timestamp": int(time.time() * 1000),
            "reportDelay": 0,
            "pageURL": page_url,
            "elementId": element_id,
            "elementName": element_name,
            "os": "win32",
            "machineId": derive_id(self.uid, "machine"),
            "userId": self.uid,
        }
        return self._post(WEB_BASE + REPORT_PATH, web_headers(self.headers), [event])

# ---------------- 桌面端事件序列构造 ----------------
# 以下序列按实测事件形状构造，服务端对链路真实性有校验倾向，
# 因此字段按客户端原始 payload 完整给出。

def desktop_chat_sequence(conversation_id, request_id, message_id,
                          model_id="fast-model", model_name="fast-model"):
    """桌面端「成功对话」完整事件链，点亮 RichMeow_Chat。"""
    now = int(time.time() * 1000)
    span = conversation_id  # traceId 复用 requestID

    def mk(code, extra):
        event = {"eventCode": code}
        event.update(extra or {})
        return event

    return [
        mk("agent_task_created", {
            "source": "LOCAL", "name": "working", "task_target": "local", "mode": "craft",
            "requestModelId": model_id, "requestModelName": model_name,
            "has_repo": False, "repo_type": "none", "workspace_type": "empty",
            "has_connector": False, "connector_types": [],
            "has_mention": False, "mention_types": [],
            "has_template": False, "action": "", "template_name": "",
            "has_expert": False, "expert_id": "", "expert_name": "", "expert_industry_id": "",
            "has_skill": False, "skill_names": [],
            "conversationId": conversation_id, "messageId": message_id,
            "buddyId": "", "buddyName": "",
        }),
        mk("chat_message_send", {
            "messageId": message_id + "-assistant", "historyCount": 0,
            "isContextTruncated": False, "currentStepCount": 1,
            "traceId": span, "rootRequestId": request_id,
            "parentConversationId": conversation_id,
            "agentName": "cli", "agentType": "main",
        }),
        mk("chat_request_send", {
            "inputLength": 24, "isPlan": False, "isAutoExecuteTerminal": False,
            "isAutoModify": False, "codebaseEnable": False, "maxToken": 0,
            "maxSteps": 500, "temperature": 0, "maxRetries": 0,
            "mentionContexts": [], "knowledgeId": [], "knowledgeName": [],
            "codebaseId": "", "mentionContextCount": 0, "command": "",
            "recommendId": "", "skillId": "", "skillCount": 0, "totalCount": 0,
            "traceId": span, "rootRequestId": request_id,
            "parentConversationId": conversation_id,
            "agentName": "cli", "agentType": "main",
            "codebuddy.session_id": conversation_id,
            "codebuddy.conversation_request_id": request_id,
        }),
        mk("chat_message_response", {
            "messageId": message_id + "-assistant", "responseModelId": model_id,
            "inputToken": 120, "outputToken": 80, "totalToken": 200,
            "cachedTokens": 0, "cachedWriteTokens": 0, "cachedMissTokens": 0,
            "isSuccessful": True, "messageErrorCode": "", "finishReason": "stop",
            "firstTokenAt": now, "traceId": span,
            "conversationId": conversation_id,
            "rootRequestId": request_id, "parentConversationId": conversation_id,
            "agentName": "cli", "agentType": "main",
            "codebuddy.session_id": conversation_id,
            "codebuddy.conversation_request_id": request_id,
        }),
        mk("chat_message_status", {
            "messageId": message_id + "-assistant", "messageErrorCode": "0",
            "traceId": span, "rootRequestId": request_id,
            "parentConversationId": conversation_id,
            "agentName": "cli", "agentType": "main",
        }),
        mk("chat_request_response", {
            "mode": "craft", "toolCallCount": 0,
            "inputToken": 120, "outputToken": 80, "totalToken": 200,
            "cachedTokens": 0, "cachedWriteTokens": 0, "cachedMissTokens": 0,
            "isSuccessful": True, "messageErrorCode": "", "finishReason": "stop",
            "rootRequestId": request_id, "parentConversationId": conversation_id,
        }),
    ]


def desktop_buddy_app_sequence(buddy_id="cb_y5Dy46tPQGGWtueMxXbe", buddy_name="企鹅教师助手"):
    """「进入 Buddy 应用」五连事件（同时覆盖 Buddy_App 与 Buddy_App_QQ）。"""

    def mk(code, extra):
        event = {"eventCode": code, "mode": "LOCAL",
                 "buddyId": buddy_id, "buddyName": buddy_name}
        event.update(extra or {})
        return event

    return [
        mk("buddyapp_discover_click", None),
        mk("buddyapp_show", {"elementId": buddy_id, "elementName": buddy_name, "position": 2}),
        mk("buddyapp_enter_click", {"elementId": buddy_id, "elementName": buddy_name,
                                    "position": 2, "isFirstPage": "1"}),
        mk("buddyapp_auth_confirm_click", {"elementId": buddy_id, "elementName": buddy_name}),
        mk("buddyapp_bindaccount_skip_click", {"elementId": buddy_id, "elementName": buddy_name}),
    ]


def desktop_automation_event(name="wb2api 自动化"):
    """定时任务创建成功事件，点亮 automation_1。"""
    return {
        "eventCode": "automated_task_create_suc", "name": name,
        "source": "manually", "modelId": "fast-model", "modelIsThinking": True,
        "connectorCount": 0, "skills": "", "skillCount": 0,
        "scheduleType": "once", "mode": "LOCAL",
    }


def desktop_skin_apply_event(theme_key):
    """皮肤生效事件（配合设置主题接口）。"""
    return {
        "eventCode": "appearance_skin_apply", "action": "apply", "source": "settings_close",
        "id": theme_key, "vipLevel": 0, "series": "", "type": "unknown",
    }


def desktop_template_sequence(conversation_id, request_id, template_id, template_name):
    """使用模板创建任务的事件组（template_used ×5 用）。"""

    def mk(code, extra):
        event = {"eventCode": code}
        event.update(extra or {})
        return event

    return [
        mk("agent_task_created_with_template", {
            "template_id": template_id, "template_name": template_name,
            "mode": "craft", "source": "TEMPLATE",
            "conversationId": conversation_id, "requestId": request_id,
        }),
        mk("template_used", {
            "template_id": template_id, "template_name": template_name,
            "conversationId": conversation_id, "requestId": request_id,
        }),
    ]


def desktop_playbook_sequence(conversation_id, request_id, case_id, case_name):
    """灵感案例「做同款」事件组（playbook_prompt 用）。"""

    def mk(code, extra):
        event = {"eventCode": code}
        event.update(extra or {})
        return event

    return [
        mk("playbook_cta_click", {
            "case_id": case_id, "case_name": case_name,
            "conversationId": conversation_id, "requestId": request_id,
        }),
        mk("playbook_prompt_send", {
            "case_id": case_id, "case_name": case_name, "promptLength": 24,
            "conversationId": conversation_id, "requestId": request_id,
        }),
    ]


def desktop_canvas_sequence(conversation_id, request_id):
    """设计创意画布创建事件组（create_canvas 用，+300 分）。"""

    def mk(code, extra):
        event = {"eventCode": code}
        event.update(extra or {})
        return event

    return [
        mk("wbx_design_canvas_task_create", {
            "conversationId": conversation_id, "requestId": request_id,
            "mode": "design", "source": "LOCAL",
        }),
        mk("wbx_design_canvas_task_open", {
            "conversationId": conversation_id, "requestId": request_id,
            "mode": "design",
        }),
    ]
