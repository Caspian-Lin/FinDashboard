"""标记:本目录所有测试归为 unit。

具体 marker 见根 ``pyproject.toml`` 的 ``[tool.pytest.ini_options].markers``。
"""

# 占位文件 —— pytest 在 import 阶段需要 __init__.py 才能稳定收集 tests/unit/* 测试。
