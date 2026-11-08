"""统一数据范围(issue #510)。"""

from finboard_backtest.data_scope.apply import (
    ScopeGateError,
    apply_scope_to_parameters,
)
from finboard_backtest.data_scope.contracts import (
    AffectedStrategy,
    DataScopeSpec,
    ResolvedRelease,
    ScopeAlternative,
    ScopeBlocker,
    ScopeDatasets,
    ScopeDecisionWindow,
    ScopeFilters,
    ScopeGaps,
    ScopePreviewRequest,
    ScopeResolution,
    ScopeUniverse,
    ScopeUniverseResult,
    ScopeWindow,
    scope_checksum,
)
from finboard_backtest.data_scope.resolver import (
    ScopeResolutionError,
    resolve_data_scope,
)

__all__ = [
    "AffectedStrategy",
    "DataScopeSpec",
    "ResolvedRelease",
    "ScopeAlternative",
    "ScopeBlocker",
    "ScopeDatasets",
    "ScopeDecisionWindow",
    "ScopeFilters",
    "ScopeGaps",
    "ScopeGateError",
    "ScopePreviewRequest",
    "ScopeResolution",
    "ScopeResolutionError",
    "ScopeUniverse",
    "ScopeUniverseResult",
    "ScopeWindow",
    "apply_scope_to_parameters",
    "resolve_data_scope",
    "scope_checksum",
]
