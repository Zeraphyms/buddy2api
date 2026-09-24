"""任务「一键完成」：行为事件驱动的自动推进。

上游任务进度靠行为事件点亮，而不是独立端点。每个动作的判据不同，
这里按任务逐个实现，动作表见 ACTIONS。

原则：
  - 幂等：已 claimed / 已达标的任务直接跳过，不重复消耗配额
  - 限速：连续上报之间留间隔，避免触发风控
  - 判据优先：只做已确知能点亮任务的事件序列，不确定的不做
"""
import time

from . import events
from .tasks import TaskError

REPORT_GAP = 1.05  # 连续上报间隔（实测口径，防风控）


class AutoTaskRunner:
    """对单个账号执行「一键完成」动作。"""

    def __init__(self, task_client, reporter, task_lister):
        self.tasks = task_client
        self.reporter = reporter
        self.list_tasks = task_lister

    def _progress(self, code):
        """取某任务的 (current, target)，不存在时返回 None。"""
        for row in self.list_tasks():
            if row.get("task_code") == code:
                return row
        return None

    def _accept(self, code):
        """报名；失败不阻塞（行为事件才是判据）。"""
        try:
            self.tasks.accept([code])
        except TaskError:
            pass

    # ---------------- 各任务动作 ----------------

    def run_chat5(self):
        """chat_5：补足 5 条对话活跃上报。"""
        row = self._progress("chat_5")
        if row is None:
            return False, "任务不存在"
        target = row["target"] or 5
        need = target - row["current"]
        if need <= 0:
            return True, "进度已达标，无需上报"
        for i in range(need):
            stamp = int(time.time() * 1000)
            self.reporter.chat_request_send("wb2api-chat5-%d-%d" % (stamp, i))
            if i < need - 1:
                time.sleep(REPORT_GAP)
        return True, "已补报 %d 条对话事件" % need

    def run_first_buddy(self):
        """first_buddy：活跃上报 → 同意协议 → 领养。

        注意：协议接口在「已同意」或「已有 Buddy」时也会返回错误，
        这类属于状态而非故障，不阻断后续领养尝试。
        """
        stamp = int(time.time() * 1000)
        self.reporter.chat_request_send("wb2api-adopt-%d" % stamp)
        time.sleep(REPORT_GAP)
        try:
            self.tasks.buddy_agreement()
        except TaskError:
            pass  # 已同意 / 已有 Buddy —— 继续尝试领养
        try:
            self.tasks.buddy_first()
        except TaskError as exc:
            return False, exc.message
        return True, "已尝试领养 Buddy（+300 分）"

    def run_model_chat(self):
        """Model_chat_GLM5.2：报名 → 按模型上报。"""
        self._accept("Model_chat_GLM5.2")
        time.sleep(REPORT_GAP)
        stamp = int(time.time() * 1000)
        self.reporter.chat_request_send("wb2api-mc-%d" % stamp,
                                        request_id="wb2api-mc-req-%d" % stamp,
                                        model_id="glm-5.2", model_name="GLM-5.2")
        return True, "已按 GLM-5.2 模型上报对话事件"

    def run_richmeow(self):
        """RichMeow_Chat：桌面端完整对话事件链。"""
        stamp = int(time.time() * 1000)
        conv = "wb2api-rm-%d" % stamp
        req = "wb2api-rm-req-%d" % stamp
        msg = "req-%d-user" % stamp
        chain = events.desktop_chat_sequence(conv, req, msg)
        self.reporter.report_desktop(chain)
        return True, "已上报桌面端对话事件链"

    def run_buddy_app(self):
        """Buddy_App / Buddy_App_QQ：进入 Buddy 应用五连事件。"""
        chain = events.desktop_buddy_app_sequence()
        self.reporter.report_desktop(chain)
        return True, "已上报 Buddy 应用进入事件链"

    def run_automation(self):
        """automation_1：定时任务创建事件。"""
        self.reporter.report_desktop([events.desktop_automation_event()])
        return True, "已上报定时任务创建事件"

    def run_library_read(self):
        """Library_read：Web 域资料库阅读点击。"""
        doc_url = "https://www.workbuddy.cn/space/d/o0KWYeynteVv06UnAZqIFm"
        self.reporter.report_web("web_element_click", doc_url,
                                 "library_doc_intro_click", "WorkBuddy资料库介绍")
        return True, "已上报资料库阅读事件"

    def run_template_5(self):
        """template_5：使用模板事件组 ×5。"""
        templates = [("1", "深度研究"), ("2", "周报生成"), ("3", "竞品分析"),
                     ("4", "活动策划"), ("5", "代码评审")]
        for i, (tid, tname) in enumerate(templates):
            stamp = int(time.time() * 1000)
            chain = events.desktop_template_sequence(
                "wb2api-tpl-%d-%d" % (stamp, i),
                "wb2api-tpl-req-%d-%d" % (stamp, i), tid, tname)
            self.reporter.report_desktop(chain)
            if i < len(templates) - 1:
                time.sleep(0.3)
        return True, "已上报 template_used ×5"

    def run_playbook_prompt(self):
        """playbook_prompt：灵感案例发送 Prompt 事件组。"""
        stamp = int(time.time() * 1000)
        chain = events.desktop_playbook_sequence(
            "wb2api-pb-%d" % stamp, "wb2api-pb-req-%d" % stamp,
            "pm-gtm-launch-plan", "新产品上市 GTM 发布计划一页纸")
        self.reporter.report_desktop(chain)
        return True, "已上报灵感案例事件组"

    def run_create_canvas(self):
        """create_canvas：设计创意画布创建事件组（+300 分）。"""
        stamp = int(time.time() * 1000)
        chain = events.desktop_canvas_sequence(
            "wb2api-canvas-%d" % stamp, "wb2api-canvas-req-%d" % stamp)
        self.reporter.report_desktop(chain)
        return True, "已上报设计画布创建事件"

    def run_hp_appearance(self):
        """Hp_Appearance：设置主题 + 皮肤生效事件。"""
        theme = "theme-tkmw7j"
        self.tasks.set_appearance_theme(theme)
        time.sleep(2)
        self.reporter.report_desktop([events.desktop_skin_apply_event(theme)])
        return True, "已设置主题并上报皮肤生效事件"

    def run_black_cat(self):
        """black_cat：夜猫子（23:00–08:00 窗口内 glm-5.2 对话）。"""
        hour = time.localtime().tm_hour
        if not (hour >= 23 or hour < 8):
            return False, "当前不在 23:00–08:00 计数窗口，行为不计分"
        self._accept("black_cat")
        time.sleep(REPORT_GAP)
        stamp = int(time.time() * 1000)
        self.reporter.chat_request_send("wb2api-bc-%d" % stamp,
                                        request_id="wb2api-bc-req-%d" % stamp,
                                        model_id="glm-5.2", model_name="GLM-5.2")
        return True, "已在夜间窗口上报 glm-5.2 对话事件"


# 动作表：task_code -> (说明, 方法名)。顺序即执行顺序。
ACTIONS = [
    ("chat_5", "上报 5 条对话活跃事件（自动补足差额）", "run_chat5"),
    ("first_buddy", "上报解锁 → 同意协议 → 领取第一只 Buddy（+300 分）", "run_first_buddy"),
    ("Model_chat_GLM5.2", "报名 → 按 GLM-5.2 模型上报对话事件", "run_model_chat"),
    ("RichMeow_Chat", "桌面端完整对话事件链", "run_richmeow"),
    ("Buddy_App", "进入 Buddy 应用事件链", "run_buddy_app"),
    ("Buddy_App_QQ", "进入企鹅教师助手事件链（与上一条共用）", "run_buddy_app"),
    ("automation_1", "定时任务创建事件", "run_automation"),
    ("Library_read", "资料库阅读点击事件", "run_library_read"),
    ("template_5", "使用模板事件组 ×5", "run_template_5"),
    ("playbook_prompt", "灵感案例发送 Prompt 事件组", "run_playbook_prompt"),
    ("create_canvas", "设计创意画布创建事件组（+300 分）", "run_create_canvas"),
    ("Hp_Appearance", "设置主题 + 皮肤生效事件", "run_hp_appearance"),
    ("black_cat", "夜猫子：夜间窗口 glm-5.2 对话", "run_black_cat"),
]

ACTION_INDEX = {code: (desc, method) for code, desc, method in ACTIONS}
