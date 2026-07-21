"""SQLAlchemy ``DeclarativeBase``。

所有 ORM 模型继承自 ``Base``。表名一律复数,字段名 ``snake_case``。
"""

from __future__ import annotations

from sqlalchemy import BigInteger
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


class IdMixin:
    """统一的自增主键 + created_at/updated_at。

    使用 BigInteger 主键,避免长期运行后 32 位溢出。
    """

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
