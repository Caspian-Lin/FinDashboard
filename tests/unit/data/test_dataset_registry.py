"""目录 registry 唯一性/对齐自检(issue #509)。"""

from __future__ import annotations

from finboard_data.datasets import (
    CATALOG_DATASETS,
    CatalogDataset,
    CatalogStorage,
    catalog_spec,
    catalog_spec_by_release_kind,
    catalog_spec_by_sync_dataset,
    catalog_specs,
)
from finboard_data.releases import RELEASE_FIELDS, ReleaseDatasetKind


def test_registry_covers_all_release_kinds() -> None:
    assert {spec.release_kind for spec in catalog_specs() if spec.release_kind} == set(
        ReleaseDatasetKind
    )
    for kind in ReleaseDatasetKind:
        spec = catalog_spec_by_release_kind(kind)
        assert spec.release_kind is kind
        assert spec.publishable is True


def test_registry_keys_and_sync_names_unique() -> None:
    keys = [spec.key for spec in catalog_specs()]
    assert len(keys) == len(set(keys)) == len(CATALOG_DATASETS) == 10
    sync_names = [spec.sync_dataset for spec in catalog_specs() if spec.sync_dataset]
    assert len(sync_names) == len(set(sync_names))
    assert catalog_spec_by_sync_dataset("daily_metrics").key is CatalogDataset.DAILY_METRICS


def test_bars_uses_release_fields_and_multi_source() -> None:
    bars = catalog_spec(CatalogDataset.BARS)
    assert bars.fields == RELEASE_FIELDS
    assert bars.source is None
    assert bars.storage is CatalogStorage.BARS_CACHE
    assert bars.adjustment == "qfq"


def test_research_releases_are_tushare_none_adjustment() -> None:
    for spec in catalog_specs():
        if spec.release_kind is None or spec.release_kind is ReleaseDatasetKind.BARS:
            continue
        assert spec.source == "tushare", spec.key
        assert spec.adjustment is None, spec.key


def test_unknown_key_rejected() -> None:
    try:
        catalog_spec("not-a-dataset")
    except ValueError:
        return
    raise AssertionError("unknown dataset must raise")
