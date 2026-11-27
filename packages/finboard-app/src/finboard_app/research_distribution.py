"""本机研究版的 worker 与 Docker 操作命令。"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import tempfile
import zipfile
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from finboard_app.config import load_settings
from finboard_app.starter_data import import_starter_bundle
from finboard_data.releases import DatasetReleaseError
from finboard_data.trading_calendar import install_calendar_store
from finboard_mcp.scopes import preset_scopes
from finboard_persistence import (
    PgTradingCalendarStore,
    TradeCalRepository,
    create_async_engine,
    session_factory,
)
from finboard_persistence.mcp_client_repo import McpClientRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_TUSHARE = Path("private/tushare")
_STARTER_LIMIT = 32 * 1024 * 1024
_PRESETS = {"readonly", "topic-writer"}


async def _client_operation(
    action: str, sessions: async_sessionmaker[AsyncSession],
) -> dict[str, Any]:
    req = json.loads(sys.stdin.buffer.read(4096).decode("utf-8-sig"))
    client_id = req["client_id"]
    if not re.fullmatch(r"MC-[0-9a-f]{12}", client_id):
        raise ValueError("invalid_client_id")
    async with sessions.begin() as session:
        repo = McpClientRepository(session)
        if action == "register":
            preset = req["preset"]
            client = req["client"]
            if (
                not re.fullmatch(r"[0-9a-f]{64}", req["token_hash"])
                or not re.fullmatch(r"[A-Za-z0-9_-]{16}", req["token_prefix"])
                or preset not in _PRESETS
                or not re.fullmatch(r"(codex|dsh|opencode|smoke|generic-[a-z0-9][a-z0-9_-]{0,31})", client)
            ):
                raise ValueError("invalid_client_credential_material")
            await repo.append(
                client_id=client_id, name=f"research-edition-{client}-{client_id}",
                token_hash=req["token_hash"], token_prefix=req["token_prefix"],
                scopes=sorted(preset_scopes(preset)), created_by="research-edition",
            )
            return {"client_id": client_id, "registered": True}
        return {"client_id": client_id, "revoked": await repo.revoke(client_id) is not None}


def _classify_tushare(exc: Exception) -> str:
    detail = str(exc)
    if any(part in detail for part in ("每分钟", "每天最多", "访问频次", "频率", "quota")):
        return "quota_exhausted"
    if any(part in detail for part in ("无权限", "没有访问", "权限", "积分", "permission")):
        return "permission_denied"
    if "token" in detail.lower() or "认证" in detail:
        return "invalid_credentials"
    return "connection_failed"


def _source_capabilities(token: str) -> dict[str, str]:
    if not token:
        return {"daily": "credentials_missing"}
    import tushare as ts

    client = ts.pro_api(token, timeout=12)
    capabilities = {}
    for name, args in (
        ("daily", {"ts_code": "600000.SH", "trade_date": "20240603"}),
        ("adj_factor", {"ts_code": "600000.SH", "trade_date": "20240603"}),
        ("index_daily", {"ts_code": "000300.SH", "trade_date": "20240603"}),
        ("daily_basic", {"ts_code": "600000.SH", "trade_date": "20240603"}),
        ("fina_indicator", {"ts_code": "600000.SH", "period": "20240331"}),
    ):
        try:
            rows = client.query(name, **args)
        except Exception as exc:
            capabilities[name] = _classify_tushare(exc)
        else:
            capabilities[name] = "available" if not rows.empty else "coverage_insufficient"
    return capabilities


def _save_tushare(token: str) -> None:
    _TUSHARE.parent.mkdir(mode=0o700, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=_TUSHARE.parent, prefix=".tushare-", delete=False, mode="w", encoding="utf-8",
    ) as destination:
        destination.write(token)
        temporary = Path(destination.name)
    temporary.replace(_TUSHARE)


def _tushare_calendar(token: str) -> dict[str, set[date]]:
    import tushare as ts

    client = ts.pro_api(token, timeout=12)
    year = date.today().year
    calendars: dict[str, set[date]] = {}
    for exchange in ("SSE", "SZSE"):
        days: set[date] = set()
        for start_year in range(2005, year + 1, 5):
            rows = client.query(
                "trade_cal", exchange=exchange, is_open="1", fields="cal_date",
                start_date=f"{start_year}0101", end_date=f"{min(start_year + 4, year)}1231",
            )
            if rows.empty:
                raise ValueError("calendar_coverage_insufficient")
            days.update(date.fromisoformat(str(day)) for day in rows["cal_date"])
        calendars[exchange] = days
    return calendars


async def _configure_source(token: str, *, db_url: str) -> dict[str, str]:
    checks = await asyncio.to_thread(_source_capabilities, token)
    if checks["daily"] != "available":
        return checks
    try:
        calendars = await asyncio.to_thread(_tushare_calendar, token)
    except Exception as exc:
        checks["trade_cal"] = (
            "coverage_insufficient"
            if str(exc) == "calendar_coverage_insufficient"
            else _classify_tushare(exc)
        )
        return checks
    engine = create_async_engine(db_url, pool_size=1, max_overflow=0)
    try:
        async with session_factory(engine).begin() as session:
            for exchange, days in calendars.items():
                await TradeCalRepository(session).upsert_trading_days(
                    days, exchanges=(exchange,), source="tushare",
                )
    finally:
        await engine.dispose()
    _save_tushare(token)
    checks["trade_cal"] = "available"
    return checks


async def _run(command: str) -> None:
    settings = load_settings()
    if command.startswith("source-"):
        configured = command == "source-configure"
        token = (
            sys.stdin.buffer.readline(4096).decode("utf-8-sig").strip()
            if configured
            else settings.tushare_token
        )
        checks = (
            await _configure_source(token, db_url=settings.db_url)
            if configured
            else await asyncio.to_thread(_source_capabilities, token)
        )
        print(json.dumps({
            "source": "tushare", "capabilities": checks,
            "other_sources": {"akshare": ["bars"], "yfinance": ["bars"]},
        }))
        if configured and checks.get("trade_cal") != "available":
            raise SystemExit(1)
        return

    engine = create_async_engine(
        settings.db_url, pool_size=settings.db_pool_size, max_overflow=settings.db_max_overflow,
    )
    sessions = session_factory(engine)
    try:
        if command == "worker":
            from finboard_app.cli import _run_worker

            install_calendar_store(PgTradingCalendarStore(sessions))
            await _run_worker(settings, database=(engine, sessions))
            return
        if command == "import":
            with tempfile.NamedTemporaryFile(suffix=".zip") as bundle:
                body = sys.stdin.buffer.read(_STARTER_LIMIT + 1)
                if len(body) > _STARTER_LIMIT:
                    raise ValueError("starter_bundle_too_large")
                bundle.write(body)
                bundle.flush()
                release = await import_starter_bundle(
                    Path(bundle.name), root=Path("data_releases"), sessions=sessions,
                )
            result: dict[str, Any] = {
                "release_id": release.release_id, "checksum": release.release_checksum,
                "source": release.source, "version": release.version,
                "start_date": release.start_date.isoformat(), "end_date": release.end_date.isoformat(),
                "symbol_count": release.symbol_count,
            }
        else:
            result = await _client_operation(command.removeprefix("client-"), sessions)
        print(json.dumps(result))
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=(
        "worker", "import", "client-register", "client-revoke", "source-configure", "source-status",
    ))
    try:
        asyncio.run(_run(parser.parse_args().command))
    except (ValueError, DatasetReleaseError, KeyError, OSError, zipfile.BadZipFile) as exc:
        # 只回传可信错误名,上游异常 / 用户提交内容可能包含密钥。
        code = str(exc)
        if not code.startswith(("starter_bundle_", "invalid_client_")) or not re.fullmatch(
            r"[a-z_]{5,80}", code
        ):
            code = type(exc).__name__
        print(json.dumps({"error": code}), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
