# finboard-data

历史行情数据源(akshare / tushare)+ 本地 parquet 缓存。

## 使用

```python
from datetime import date

from finboard_data import AkShareProvider
from finboard_shared.models import Symbol
from finboard_shared.types import BarPeriod, Market

provider = AkShareProvider()
bars = await provider.fetch_bars(
    Symbol(code="510300.SH", market=Market.A_SHARE),
    BarPeriod.D1,
    start=date(2023, 1, 1),
    end=date(2024, 12, 31),
    adjust="qfq",
)
```

## CLI

```bash
finboard data fetch 510300.SH --period D1 --start 2023-01-01 --end 2024-12-31 --adjust qfq
```

## 依赖

`akshare` 和 `pyarrow` 为可选依赖(lazy import),Linux CI 无需安装:

```bash
uv pip install -e ".[akshare,cache]"
```
