"""容器研究入口, 只接受分发版支持的进程。"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import quote


def main() -> None:
    command, *arguments = sys.argv[1:]
    password = Path("/run/secrets/postgres_password").read_text(encoding="utf-8").strip()
    os.environ["FINBOARD_DB_URL"] = (
        f"postgresql+psycopg://findashboard:{quote(password, safe='')}@postgres:5432/findashboard"
    )
    token = Path("/state/private/tushare")
    if token.is_file():
        os.environ["FINBOARD_TUSHARE_TOKEN"] = token.read_text(encoding="utf-8").strip()
    match command:
        case "gateway":
            argv = [
                "uvicorn", "finboard_api.research_distribution:create_app", "--factory",
                "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers",
            ]
        case "worker":
            argv = ["python", "-m", "finboard_app.research_distribution", "worker"]
        case "migrate":
            os.chdir("/opt/finboard")
            argv = ["alembic", "upgrade", "head"]
        case "operator":
            argv = ["python", "-m", "finboard_app.research_distribution", *arguments]
        case _:
            raise SystemExit(f"unknown research command: {command}")
    os.execvp(argv[0], argv)


if __name__ == "__main__":
    main()
