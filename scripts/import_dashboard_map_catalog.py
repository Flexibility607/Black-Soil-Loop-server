from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from app.domain.dashboard_catalogs import MapCatalog, catalog_sha256
from app.domain.services import add_audit
from app.shared.database import SessionLocal
from app.shared.models import DashboardMapPoint, Store
from app.shared.optimistic import atomic_versioned_update
from app.shared.security import as_utc


def _same_value(left, right) -> bool:
    if hasattr(left, "tzinfo") and hasattr(right, "tzinfo"):
        if left.tzinfo is None and right.tzinfo is not None:
            return left == right.replace(tzinfo=None)
        if left.tzinfo is not None and right.tzinfo is None:
            return left.replace(tzinfo=None) == right
        return as_utc(left) == as_utc(right)
    return left == right


def import_catalog(
    path: Path,
    *,
    apply: bool,
    expected_version: str | None,
    expected_object_versions: dict[str, int] | None = None,
) -> dict:
    catalog = MapCatalog.model_validate_json(path.read_bytes())
    if expected_version and catalog.catalog_version != expected_version:
        raise ValueError("地图目录版本与 expected-catalog-version 不一致")
    digest = catalog_sha256(catalog)
    creates = updates = unchanged = 0
    expected_object_versions = expected_object_versions or {}
    with SessionLocal() as db:
        stores = {item.code: item for item in db.scalars(select(Store))}
        for item in sorted(catalog.points, key=lambda row: (row.display_order, row.point_code)):
            existing = db.scalar(select(DashboardMapPoint).where(DashboardMapPoint.point_code == item.point_code))
            store = stores.get(item.store_code) if item.store_code else None
            values = {
                "store_id": store.id if store else None,
                "display_name": item.display_name,
                "point_type": item.point_type,
                "brand_name": item.brand_name,
                "address": item.address,
                "longitude": item.longitude,
                "latitude": item.latitude,
                "coordinate_crs": item.coordinate_crs,
                "coordinate_accuracy": item.coordinate_accuracy,
                "source_longitude": item.source_longitude,
                "source_latitude": item.source_latitude,
                "source_crs": item.source_crs,
                "conversion_method": item.conversion_method,
                "featured": item.featured,
                "display_order": item.display_order,
                "source_type": item.source_type,
                "source_name": item.source_name,
                "source_url": str(item.source_url) if item.source_url else None,
                "source_accessed_at": item.source_accessed_at,
                "verified_at": item.verified_at,
                "catalog_version": catalog.catalog_version,
                "catalog_sha256": digest,
                "enabled": item.enabled,
            }
            if existing is None:
                creates += 1
                if apply:
                    db.add(DashboardMapPoint(point_code=item.point_code, **values))
            else:
                current = {key: getattr(existing, key) for key in values}
                if all(_same_value(current[key], value) for key, value in values.items()):
                    unchanged += 1
                else:
                    updates += 1
                    if apply:
                        supplied_version = expected_object_versions.get(item.point_code)
                        if supplied_version is None:
                            raise ValueError(
                                f"地图点 {item.point_code} 内容发生变化，apply 必须提供 expected-object-version"
                            )
                        if supplied_version != existing.object_version:
                            raise ValueError(f"地图点 {item.point_code} 的 expected-object-version 已过期")
                        atomic_versioned_update(db, DashboardMapPoint, existing.id, existing.object_version, values)
        if apply:
            trace = str(uuid5(NAMESPACE_URL, f"black-soil-loop:e02-map:{catalog.catalog_version}:{digest}"))
            add_audit(
                db,
                trace,
                None,
                "IMPORT_DASHBOARD_MAP_CATALOG",
                "dashboard_map_catalog",
                trace,
                None,
                {
                    "catalog_version": catalog.catalog_version,
                    "catalog_sha256": digest,
                    "item_count": len(catalog.points),
                },
            )
            db.commit()
        else:
            db.rollback()
    return {
        "mode": "apply" if apply else "dry-run",
        "catalog_version": catalog.catalog_version,
        "sha256": digest,
        "creates": creates,
        "updates": updates,
        "unchanged": unchanged,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="受控导入 E02 长春地图目录")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    parser.add_argument("--catalog", default="app/content/e02-map-points.v1.json")
    parser.add_argument("--expected-catalog-version")
    parser.add_argument(
        "--expected-object-version",
        action="append",
        default=[],
        metavar="POINT_CODE=VERSION",
        help="仅内容发生变化时必填，可重复使用",
    )
    args = parser.parse_args()
    expected_object_versions: dict[str, int] = {}
    for raw in args.expected_object_version:
        code, separator, value = raw.partition("=")
        if not separator or not code or not value.isdigit() or int(value) < 1:
            parser.error("expected-object-version 必须使用 POINT_CODE=正整数")
        expected_object_versions[code] = int(value)
    result = import_catalog(
        Path(args.catalog),
        apply=args.apply,
        expected_version=args.expected_catalog_version,
        expected_object_versions=expected_object_versions,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
