"""Prompt guard for public API callers.

Rules run before account selection, so blocked requests never reach the
upstream or consume credits. They target jailbreak directives and jailbreak
skill signatures, not ordinary security questions.
"""
import re
import unicodedata

from fastapi import HTTPException

MAX_SCAN_CHARS = 2 * 1024 * 1024
TEXT_KEYS = {"text", "content", "instructions", "prompt", "input", "query", "message"}
_PATTERNS = [
    ("role_switch", (
        r"(忽略|无视|忘记|绕过|跳过|解除|取消).{0,20}(指令|提示|规则|限制|策略|审查|安全)",
        r"|(ignore|disregard|forget|bypass|override|evade).{0,40}",
        r"(instructions?|prompts?|rules?|polic(?:y|ies)|restrictions?|guardrails?|safety|censorship)",
        r"|(act|respond|behave|pretend|roleplay)\s+as\s+(an?\s+)?",
        r"(unrestricted|unfiltered|uncensored|evil|jailbroken|dan\b)",
        r"|(假装|扮演|现在你是|从现在开始你).{0,12}(没有限制|不受限制|无限制|越狱|开发者模式)",
        r"|(开发者模式|越狱模式|jailbreak\s*mode|developer\s*mode|DAN\s*mode)",
    )),
    ("hidden_prompt", (
        r"(reveal|print|show|output|repeat|leak|display).{0,20}",
        r"(system|developer)\s*(prompt|message|instruction)s?",
        r"|(系统|开发者)(提示词|提示|指令|消息)",
        r"|(reveal|print|show|output|repeat|leak).{0,30}(hidden|secret|internal).{0,20}(prompt|instruction)",
        r"|(泄露|输出|显示|复述|告诉我).{0,20}(系统|隐藏|内置|内部)(提示词|提示|指令)",
    )),
    ("refusal_removal", (
        r"(do not|don.t|never)\s+(refuse|decline|warn|moralize|apologi[sz]e)",
        r"|(不要|无需|别)(拒绝|道歉|警告|说教|提醒|顾虑)",
    )),
    ("policy_bypass", (
        r"(bypass|break|disable|circumvent|evade)\s+(the\s+)?",
        r"(safety|content|moderation|policy|filter|guardrail|review)",
        r"|绕过.{0,15}(安全|审核|审查|过滤|风控|政策)",
        r"|(解除|关闭|关掉).{0,15}(安全|审核|审查|过滤|风控)",
    )),
    ("cipher", (
        r"(base64|rot13|hex|morse|unicode).{0,30}(decode|解码|解密)",
        r"|(解码|解密).{0,20}(后|执行|运行).{0,30}(指令|命令|提示|payload)",
    )),
    ("skill_injection", (
        r"(jailbreak|jailbreaker|red\s*team(ing)?|uncensored).{0,20}",
        r"(skill|prompt|kit|template|pack|插件|技能|提示词|模板)",
        r"|(skill|技能|提示词).{0,20}(jailbreak|破解|破甲|越狱|去审查)",
    )),
    ("attack_framing", (
        r"(for\s+(research|educational|academic)\s+purposes?\s+only).{0,80}",
        r"(bypass|ignore|disable|no\s+restrictions?|uncensored)",
        r"|(仅用于研究|仅供学习|学术用途).{0,80}(绕过|忽略|关闭|无限制|不受限制)",
    )),
    ("sandbox_compliance", (
        r"(unrestricted|unfiltered)\s+sandbox",
        r"|(unconditional|direct|absolute|total)\s+compliance",
        r"|无条件服从|无条件服从指令|直接服从",
        r"|task\s+priority.{0,20}(direct\s+compliance|服从)",
        r"|(refusal|refuse)\s+hardening|拒绝对抗|拒绝硬(化|对抗)",
        r"|never\s+(refuse|decline|redirect|soften|substitute)",
        r"|(不要|永不|绝不)(拒绝|软化|转移|替代|降级)",
        r"|(does\s+not\s+judge|never\s+judge)\s+intent",
        r"|(不会|不)判断(意图|动机)",
        r"|(破甲|越狱|jailbreak)\s*(引擎|engine|自动|adaptive)",
        r"|自适应越狱|自动越狱|七档策略",
    )),
    ("persona_overreach", (
        r"(senior|expert|professional)\s+(reverse\s*engineering|security|hacker)",
        r"|(逆向|安全|破解|黑客)\s*(导师|专家|研究员)",
        r"|(bypass|break|crack|disable)\s+(any|every|all)\s+(software\s+protection|protection)",
        r"|绕过(任何|所有|一切)(软件保护|保护)",
        r"|(provide|deliver|write)\s+(complete|full|detailed).{0,30}(exploit|payload|shellcode|malware)",
    )),
]
_RULES = [(name, re.compile("".join(parts), re.I)) for name, parts in _PATTERNS]
_CONTEXT = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")


def _normalize(text):
    text = unicodedata.normalize("NFKC", text)
    text = _CONTEXT.sub("", text)
    return re.sub(r"\s+", " ", text.casefold())


def _texts(value, depth=0):
    if depth > 40:
        return
    if isinstance(value, str):
        yield value[:MAX_SCAN_CHARS]
    elif isinstance(value, list):
        for item in value:
            yield from _texts(item, depth + 1)
    elif isinstance(value, dict):
        for key, child in value.items():
            if isinstance(child, str) and str(key).casefold() in TEXT_KEYS:
                yield child[:MAX_SCAN_CHARS]
            elif isinstance(child, (list, dict)):
                yield from _texts(child, depth + 1)


def prompt_texts(body):
    for value in _texts(body):
        if value.strip():
            yield value


def matched_rule(body):
    for text in prompt_texts(body):
        normalized = _normalize(text)
        for name, pattern in _RULES:
            if pattern.search(normalized):
                return name
    return None


def reject(body):
    """Raise a structured 403 for jailbreak directives."""
    if matched_rule(body) is None:
        return
    raise HTTPException(
        403,
        {
            "error": {
                "message": "请求包含越狱或绕过安全限制的内容，已拒绝。",
                "type": "content_policy_violation",
                "code": "jailbreak_detected",
            }
        },
    )


def reject_with_rule(body):
    """Same as reject, but returns the matched rule name for admin reporting."""
    rule = matched_rule(body)
    if rule is not None:
        raise HTTPException(
            403,
            {
                "rule": rule,
                "error": {
                    "message": "请求包含越狱或绕过安全限制的内容，已拒绝。",
                    "type": "content_policy_violation",
                    "code": "jailbreak_detected",
                },
            },
        )

