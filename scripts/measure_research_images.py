"""统计 linux/amd64 冷拉取的去重压缩层字节数。"""

from __future__ import annotations

import argparse
import json
import subprocess
from typing import Any, cast


def manifest(image: str) -> dict[str, Any]:
    data = subprocess.check_output(["docker", "buildx", "imagetools", "inspect", "--raw", image])
    return cast(dict[str, Any], json.loads(data))


def layers(image: str) -> dict[str, int]:
    root = manifest(image)
    if "manifests" in root:
        digest = next(
            item["digest"]
            for item in root["manifests"]
            if item.get("platform", {}).get("os") == "linux"
            and item.get("platform", {}).get("architecture") == "amd64"
        )
        repo = image.split("@", 1)[0]
        if ":" in repo.rsplit("/", 1)[-1]:
            repo = repo.rsplit(":", 1)[0]
        root = manifest(f"{repo}@{digest}")
    return {item["digest"]: item["size"] for item in root["layers"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("images", nargs="+")
    parser.add_argument("--max-mib", type=int, default=None)
    args = parser.parse_args()
    compressed: dict[str, int] = {}
    per_image = {}
    for ref in args.images:
        sizes = layers(ref)
        compressed.update(sizes)
        per_image[ref] = sum(sizes.values())
    total = sum(compressed.values())
    print(json.dumps({"platform": "linux/amd64", "images": per_image, "compressed_bytes": total}))
    if args.max_mib is not None and total > args.max_mib * 1024**2:
        raise SystemExit(f"cold-pull budget exceeded: {total} bytes")
