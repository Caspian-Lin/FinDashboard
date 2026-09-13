"""#450 追续:PG 连接黑洞加固参数(keepalive)只对 postgres 驱动生效。"""

from finboard_app.config import postgres_connect_args


def test_postgres_urls_get_keepalive_args() -> None:
    args = postgres_connect_args(
        "postgresql+psycopg://findashboard:pw@127.0.0.1:5432/findashboard"
    )
    assert args == {
        "connect_timeout": 10,
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 3,
    }
    assert postgres_connect_args(
        "postgresql://findashboard:pw@127.0.0.1:5432/findashboard"
    ) == args


def test_non_postgres_urls_get_no_args() -> None:
    assert postgres_connect_args("sqlite+aiosqlite:///./local.db") == {}
    assert postgres_connect_args("sqlite:///./local.db") == {}
    assert postgres_connect_args("") == {}
