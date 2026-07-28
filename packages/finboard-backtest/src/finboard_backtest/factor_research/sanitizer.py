"""敏感数据脱敏(Issue #65)。

防止 API token / 密码 / 账户信息泄漏到 LLM 提示词或审计日志中。
所有发往 LLM 的提示词必须经过 :func:`sanitize_prompt` 处理。
"""

from __future__ import annotations

import re

_SENSITIVE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-[a-zA-Z0-9]{20,}"), "sk-***REDACTED***"),
    (re.compile(r"sk-ant-[a-zA-Z0-9]{20,}"), "sk-ant-***REDACTED***"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AKIA***REDACTED***"),
    (re.compile(r"(?i)(password|passwd|pwd)\s*[:=]\s*\S+"), "***REDACTED***"),
    (re.compile(r"(?i)(api[_-]?key|apikey)\s*[:=]\s*\S+"), "***REDACTED***"),
    (re.compile(r"(?i)(secret|token|credential)\s*[:=]\s*\S+"), "***REDACTED***"),
    (re.compile(r"(?i)Bearer\s+[A-Za-z0-9._\-]+"), "Bearer ***REDACTED***"),
    (re.compile(r"\b1[3-9]\d{9}\b"), "***PHONE***"),
    (re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"), "***EMAIL***"),
    (re.compile(r"\b\d{16,19}\b"), "***CARD***"),
    (re.compile(r"\b\d{17}[\dXx]\b"), "***ID***"),
)


def sanitize_prompt(text: str) -> str:
    """脱敏文本中的敏感信息。

    替换 API key / 密码 / token / 手机号 / 邮箱 / 银行卡号 / 身份证号。
    """
    result = text
    for pattern, replacement in _SENSITIVE_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def contains_sensitive_data(text: str) -> bool:
    """检查文本是否包含疑似敏感数据。"""
    return any(pattern.search(text) for pattern, _ in _SENSITIVE_PATTERNS)
