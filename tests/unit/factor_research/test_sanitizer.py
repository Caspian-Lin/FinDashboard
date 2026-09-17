"""敏感数据脱敏测试。"""

from __future__ import annotations

import pytest

from finboard_backtest.factor_research import (
    contains_sensitive_data,
    sanitize_prompt,
)


class TestSanitizePrompt:
    def test_clean_text_unchanged(self) -> None:
        text = "这是一个普通的因子假设提示词"
        assert sanitize_prompt(text) == text

    @pytest.mark.parametrize(
        ("raw", "should_contain"),
        [
            ("sk-abcd1234efgh5678ijkl9012mnop3456", True),
            ("AKIAABCDEFGHIJKLMNOP", True),
            ("password=mysecret123", True),
            ("api_key=abc123def456", True),
            ("secret=supersecret", True),
            ("token=eyJhbGciOiJIUzI1NiJ9", True),
            ("Bearer eyJhbGciOiJIUzI1NiJ9", True),
            ("联系电话 13912345678", True),
            ("邮箱 test@example.com", True),
            ("银行卡 6222020200112345678", True),
            ("身份证 110101199001011234", True),
        ],
    )
    def test_contains_sensitive(self, raw: str, should_contain: bool) -> None:
        assert contains_sensitive_data(raw) == should_contain

    def test_openai_key_redacted(self) -> None:
        text = "use key sk-abcd1234efgh5678ijkl9012mnop3456qrstuv"
        sanitized = sanitize_prompt(text)
        assert "sk-abcd1234" not in sanitized
        assert "REDACTED" in sanitized

    def test_password_redacted(self) -> None:
        text = "DB password=secret_pass_123"
        sanitized = sanitize_prompt(text)
        assert "secret_pass_123" not in sanitized

    def test_email_redacted(self) -> None:
        text = "联系 trader@example.com 获取详情"
        sanitized = sanitize_prompt(text)
        assert "trader@example.com" not in sanitized
        assert "EMAIL" in sanitized

    def test_phone_redacted(self) -> None:
        text = "手机 13800138000 是我的联系方式"
        sanitized = sanitize_prompt(text)
        assert "13800138000" not in sanitized

    def test_multiple_patterns(self) -> None:
        text = (
            "key=sk-abcdef1234567890abcdef1234567890 "
            "email=a@b.com "
            "phone=13912345678"
        )
        sanitized = sanitize_prompt(text)
        assert "sk-abcdef" not in sanitized
        assert "a@b.com" not in sanitized
        assert "13912345678" not in sanitized

    def test_no_false_positive_on_normal_math(self) -> None:
        text = "rank(ts_mean(close, 20))"
        assert not contains_sensitive_data(text)
        assert sanitize_prompt(text) == text
