"""准备离线 MSI 使用的两张 Linux amd64 镜像、安装器和合成样例。"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

# 升级此 digest 后重新验证迁移及保留数据重装。
POSTGRES_IMAGE = "postgres@sha256:f2f98726820d0342055351c6e9d36e705f5bfffb3dd53ff556325114b08afb6d"
_HOST_FILES = (
    "finboard.ps1", "finboard.cmd", "installer-clients.ps1", "mcp-bridge.ps1",
    "desktop-host.ps1", "compose.yaml", "compose.desktop.yaml",
)
_VERSION = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z")


def _docker(*args: str) -> str:
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout.strip()


def _inspect(reference: str) -> str:
    identity, platform = _docker("image", "inspect", "--format", "{{.Id}} {{.Os}}/{{.Architecture}}", "--", reference).split()
    if platform != "linux/amd64" or not re.fullmatch(r"sha256:[0-9a-f]{64}", identity):
        raise ValueError(f"{reference} must be a Linux amd64 content-addressed Docker image")
    return identity


def _write_image_archive(path: Path, tags: tuple[str, ...]) -> None:
    # Docker load 按 gzip 魔数识别。流式写入避免额外落一份未压缩镜像。
    with (
        path.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=1, mtime=0) as compressed,
        subprocess.Popen(["docker", "image", "save", *tags], stdout=subprocess.PIPE) as process,
    ):
        assert process.stdout is not None
        shutil.copyfileobj(process.stdout, compressed, length=1024 * 1024)
        if process.wait() != 0:
            raise RuntimeError("Docker failed to save the offline images")


def package(
    output: Path, config: Path, starter: Path, app: str, postgres: str, version: str, *, preview: bool = False,
) -> None:
    match = _VERSION.fullmatch(version)
    if match is None or any(int(part) > limit for part, limit in zip(match.groups(), (255, 255, 65535), strict=True)):
        raise ValueError("MSI version must be a numeric major.minor.patch within Windows Installer limits")
    root = Path(__file__).resolve().parents[1]
    sources = (app, postgres)
    tag_version = f"preview-{version}" if preview else version
    tags = (f"finboard-research-desktop/app:{tag_version}", f"finboard-research-desktop/postgres:{tag_version}")
    identities = [_inspect(source) for source in sources]
    output = output.resolve()
    config = config.resolve()
    if output.exists():
        raise FileExistsError(f"choose an unused offline resource directory: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    config.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".desktop-pack-", dir=output.parent) as temporary:
        staged = Path(temporary) / "edition"
        staged.mkdir()
        for name in _HOST_FILES:
            shutil.copyfile(root / "docker" / "research" / name, staged / name)
        shutil.copytree(root / ".agents" / "skills" / "finboard-research", staged / "finboard-research")
        shutil.copyfile(starter, staged / "starter.zip")
        (staged / "image.txt").write_text(f"{tags[0]}\n", encoding="utf-8")
        archive = staged / "images.tar"
        created_tags: list[tuple[str, str]] = []
        try:
            for source, tag, identity in zip(sources, tags, identities, strict=True):
                try:
                    existing = _inspect(tag)
                except subprocess.CalledProcessError as error:
                    if "No such image" not in (error.stderr or ""):
                        raise
                    _docker("image", "tag", source, tag)
                    created_tags.append((tag, identity))
                else:
                    if existing != identity:
                        raise ValueError(f"offline tag {tag} already points to a different image")
            _write_image_archive(archive, tags)
        finally:
            for tag, identity in created_tags:
                if _inspect(tag) == identity:
                    _docker("image", "rm", tag)
        with archive.open("rb") as image_file:
            checksum = hashlib.file_digest(image_file, "sha256").hexdigest()
        manifest = {
            "schema_version": 1,
            "archive_sha256": checksum,
            "images": [{"tag": tag, "id": image_id} for tag, image_id in zip(tags, identities, strict=True)],
        }
        if preview:
            manifest["channel"] = "preview"
        (staged / "offline-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        staged.rename(output)

    relative = Path(os.path.relpath(output, config.parent)).as_posix()
    bundle_config = {
        "version": version,
        "bundle": {"resources": {relative: "edition/"}},
    }
    if preview:
        bundle_config.update(productName="FinDashboard Research Preview", identifier="com.findashboard.research.preview")
    config.write_text(json.dumps(bundle_config, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"archive_bytes": (output / "images.tar").stat().st_size, **manifest}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--starter", type=Path, required=True)
    parser.add_argument("--app-image", required=True)
    parser.add_argument("--postgres-image", default=POSTGRES_IMAGE)
    parser.add_argument("--version", required=True)
    parser.add_argument("--preview", action="store_true")
    options = parser.parse_args()
    package(
        options.output, options.config, options.starter, options.app_image, options.postgres_image,
        options.version, preview=options.preview,
    )
