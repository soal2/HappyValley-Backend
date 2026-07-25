"""用户文本净化：静默清除脏话/侮辱性词汇，保证下游 LLM 与生图不被内容审核拒绝。

绘画贡献、回顾发言等所有用户输入都会先经过 :func:`sanitize_user_text`，
把可能触发上游内容策略（如 StepFun 的 451）的辱骂/脏话片段悄悄抹掉，
其余文字原样保留；若整段都是脏话则回退到一句温和的占位文字，
既不中断疗愈流程，也不把攻击性内容喂给文本模型或生图模型。
"""

import re

# 整段被清空时的温和占位（前端仍会展示，LLM 也能安全处理）。
_GENTLE_PLACEHOLDER = "（此处的表达已温柔省略）"

# 英文脏话/侮辱词根：命中即连同常见变体一起清除（大小写不敏感）。
_EN_PROFANITY_ROOTS = (
    "fuck",
    "shit",
    "bitch",
    "bastard",
    "asshole",
    "dick",
    "cunt",
    "pussy",
    "whore",
    "slut",
    "nigger",
    "faggot",
    "retard",
    "motherfucker",
    "wanker",
    "damn",
)

# 中文脏话/侮辱词：命中即清除。
_ZH_PROFANITY = (
    "傻逼",
    "煞笔",
    "沙比",
    "傻叉",
    "傻缺",
    "垃圾",
    "废物",
    "贱人",
    "婊子",
    "婊",
    "妈的",
    "他妈",
    "你妈",
    "去死",
    "滚蛋",
    "王八蛋",
    "混蛋",
    "畜生",
    "狗东西",
    "操你",
    "草你",
    "日你",
    "屌",
    "逼",
    "妈逼",
    "尼玛",
    "沙雕",
    "脑残",
    "智障",
    "白痴",
)


def _english_pattern(root: str) -> re.Pattern[str]:
    """把词根编成能匹配 ``f*ck``/``fuuck``/``f u c k`` 等变体的正则。"""
    # 每个字母允许重复，字母之间允许穿插非字母的分隔/掩码字符（*、空格、标点等）。
    body = r"[^a-z]{0,2}".join(f"{ch}+" for ch in root)
    return re.compile(rf"\b{body}\b", re.IGNORECASE)


_EN_PATTERNS = tuple(_english_pattern(root) for root in _EN_PROFANITY_ROOTS)


def sanitize_user_text(text: str) -> str:
    """静默清除脏话/侮辱性词汇；整段被清空时回退到温和占位文字。

    仅移除攻击性片段，其余文字（含正常中英文、标点）原样保留。
    例如 ``"fuck the duck"`` → ``"the duck"``；``"傻逼"`` → 温和占位。
    """
    cleaned = text
    for pattern in _EN_PATTERNS:
        cleaned = pattern.sub(" ", cleaned)
    for term in _ZH_PROFANITY:
        cleaned = cleaned.replace(term, "")
    # 折叠清除后残留的多余空白。
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
    return cleaned or _GENTLE_PLACEHOLDER
