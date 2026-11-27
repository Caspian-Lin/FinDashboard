"""打包 Windows 安装器及同版本的研究 Skill。"""

from __future__ import annotations

import argparse
import re
import zipfile
from pathlib import Path


def package(output: Path, image: str) -> None:
    if not re.fullmatch(r"ghcr\.io/[a-z0-9_./-]+(?:[:][a-zA-Z0-9_.-]+|@sha256:[0-9a-f]{64})", image):
        raise ValueError("expected a versioned GHCR image reference")
    root = Path(__file__).resolve().parents[1]
    source = root / "docker/research"
    skill = root / ".agents/skills/finboard-research"
    files = {
        name: (source / name).read_bytes()
        for name in ("finboard.cmd", "finboard.ps1", "installer-clients.ps1", "mcp-bridge.ps1", "compose.yaml")
    }
    files.update({
        str(Path(skill.name) / item.relative_to(skill)).replace("\\", "/"): item.read_bytes()
        for item in skill.rglob("*.md")
    })
    files["README.md"] = (root / "docs/research-edition.md").read_bytes()
    files["image.txt"] = (image + "\n").encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in sorted(files.items()):
            archive.writestr(zipfile.ZipInfo(name), content, compress_type=zipfile.ZIP_DEFLATED)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("image")
    args = parser.parse_args()
    package(args.output, args.image)
