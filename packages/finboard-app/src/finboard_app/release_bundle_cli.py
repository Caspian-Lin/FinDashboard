"""本机冻结发布包命令。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer

from finboard_app.config import load_settings
from finboard_app.release_bundle import (
    bundle_file_checksum,
    export_registered_bundle,
    import_registered_bundle,
    is_legacy_starter,
    preview_registered_bundle,
    release_root,
)
from finboard_app.starter_data import import_starter_bundle
from finboard_data.release_bundle import ReleaseBundleError
from finboard_data.releases import DatasetReleaseError
from finboard_persistence import create_async_engine, session_factory

bundle_cli = typer.Typer(help="导入或导出不可变的研究数据发布")


def _output(value: object) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2))


@bundle_cli.command("export")
def export_command(
    release: Annotated[list[str], typer.Option("--release", help="冻结发布 ID, 可重复")],
    output: Annotated[Path, typer.Argument(help="输出 ZIP 路径")],
) -> None:
    """导出已登记发布和对应日历。请遵循原数据来源的再分发条款。"""
    async def run() -> None:
        engine = create_async_engine(load_settings().db_url)
        try:
            async with session_factory(engine)() as session:
                bundle = await export_registered_bundle(
                    session, release, output, root=release_root(),
                )
            _output({**bundle.summary(), "checksum": await asyncio.to_thread(bundle_file_checksum, output)})
        finally:
            await engine.dispose()

    try:
        asyncio.run(run())
    except (ReleaseBundleError, DatasetReleaseError, OSError) as exc:
        typer.echo(f"导出失败: {exc}", err=True)
        raise typer.Exit(1) from exc


@bundle_cli.command("preview")
def preview_command(path: Path) -> None:
    """核对清单、占用和已有发布冲突。"""
    async def run() -> None:
        engine = create_async_engine(load_settings().db_url)
        try:
            async with session_factory(engine)() as session:
                _output(await preview_registered_bundle(session, path, root=release_root()))
        finally:
            await engine.dispose()

    try:
        asyncio.run(run())
    except (ReleaseBundleError, DatasetReleaseError, OSError) as exc:
        typer.echo(f"预览失败: {exc}", err=True)
        raise typer.Exit(1) from exc


@bundle_cli.command("import")
def import_command(path: Path) -> None:
    """显式导入已确认的发布包, 兼容原有合成样例。"""
    async def run() -> None:
        engine = create_async_engine(load_settings().db_url)
        try:
            sessions = session_factory(engine)
            if await asyncio.to_thread(is_legacy_starter, path):
                release = await import_starter_bundle(path, root=release_root(), sessions=sessions)
                _output({"release_id": release.release_id, "release_checksum": release.release_checksum})
            else:
                bundle = await import_registered_bundle(sessions, path, root=release_root())
                _output(bundle.summary())
        finally:
            await engine.dispose()

    try:
        asyncio.run(run())
    except (ValueError, DatasetReleaseError, OSError) as exc:
        typer.echo(f"导入失败: {exc}", err=True)
        raise typer.Exit(1) from exc
