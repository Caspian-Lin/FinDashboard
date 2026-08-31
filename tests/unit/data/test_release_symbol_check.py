"""release_symbol_check 纯函数(issue #238)—— 按冻结发布成员核对标的。"""

from __future__ import annotations

from types import SimpleNamespace

from finboard_persistence import release_symbol_check


def _release(instruments: list[SimpleNamespace] | None) -> SimpleNamespace:
    return SimpleNamespace(release_id="REL-1", instruments=instruments)


class TestReleaseSymbolCheck:
    def test_matched_and_missing_keep_request_order(self) -> None:
        release = _release(
            [
                SimpleNamespace(code="600000.SH"),
                SimpleNamespace(code="000001.SZ"),
                SimpleNamespace(code="510300.SH"),
            ]
        )
        check = release_symbol_check(release, ["000001.SZ", "999999.SZ", "600000.SH"])
        assert check["requested"] == 3
        assert check["matched"] == ["000001.SZ", "600000.SH"]
        assert check["missing"] == ["999999.SZ"]

    def test_all_missing_when_no_instruments(self) -> None:
        # fail-visible:instruments 空(理论上不可能,发布即冻结)按空集处理
        check = release_symbol_check(_release(None), ["600000.SH"])
        assert check["matched"] == []
        assert check["missing"] == ["600000.SH"]

    def test_blank_codes_dropped_from_requested(self) -> None:
        release = _release([SimpleNamespace(code="600000.SH")])
        check = release_symbol_check(release, ["600000.SH", "  ", ""])
        assert check["requested"] == 1
        assert check["matched"] == ["600000.SH"]
        assert check["missing"] == []

    def test_entries_without_code_ignored(self) -> None:
        release = _release(
            [SimpleNamespace(code=None), SimpleNamespace(code="600000.SH")]
        )
        check = release_symbol_check(release, ["600000.SH"])
        assert check["matched"] == ["600000.SH"]
