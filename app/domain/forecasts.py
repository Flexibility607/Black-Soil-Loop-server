from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.algorithms import forecast_demand
from app.shared.dictionaries import FORECAST_METHOD_LABELS, enum_label
from app.shared.models import (
    DemandForecastBatch,
    DemandForecastProjection,
    Enterprise,
    InventoryBalance,
    Product,
    StockoutDemandProjection,
    Store,
    utcnow,
)
from app.shared.optimistic import atomic_versioned_update
from app.shared.periods import SHANGHAI
from app.shared.security import as_utc

FORECAST_RULES_VERSION = "next-week-aggregate-v1"


def next_week_window(now: datetime | None = None) -> tuple[date, date]:
    local_date = (now or utcnow()).astimezone(SHANGHAI).date()
    start = local_date + timedelta(days=(7 - local_date.weekday()))
    return start, start + timedelta(days=6)


def _cutoff_from_text(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = date.fromisoformat(value) if len(value) == 10 else datetime.fromisoformat(value)
    if isinstance(parsed, date) and not isinstance(parsed, datetime):
        return datetime.combine(parsed, time.min, tzinfo=SHANGHAI).astimezone(UTC)
    return as_utc(parsed)


def forecast_projection_data(
    item: DemandForecastProjection,
    enterprise: Enterprise | None = None,
    product: Product | None = None,
) -> dict[str, Any]:
    return {
        "id": item.id,
        "enterprise_id": item.enterprise_id,
        "enterprise_name": enterprise.name if enterprise else None,
        "product_id": item.product_id,
        "product_name": product.name if product else None,
        "unit": product.unit if product else None,
        "forecast_start": item.forecast_start.isoformat(),
        "forecast_end": item.forecast_end.isoformat(),
        "historical_usage": item.historical_usage,
        "production_plan_quantity": item.production_plan_quantity,
        "forecast_quantity": item.forecast_quantity,
        "suggested_purchase_quantity": item.suggested_purchase_quantity,
        "lower_bound": item.lower_bound,
        "upper_bound": item.upper_bound,
        "mae": item.mae,
        "smape": item.smape,
        "method": item.method,
        "method_label": enum_label(FORECAST_METHOD_LABELS, item.method),
        "method_note": item.method_note,
        "data_cutoff": as_utc(item.data_cutoff).isoformat() if item.data_cutoff else None,
        "object_version": item.object_version,
        "generated_at": item.updated_at.isoformat(),
    }


def _inventory_and_stockout(
    db: Session, enterprise_id: str, product_id: str
) -> tuple[float, float, list[datetime]]:
    inventory, inventory_cutoff = db.execute(
        select(func.coalesce(func.sum(InventoryBalance.quantity), 0), func.max(InventoryBalance.updated_at))
        .join(Store, Store.id == InventoryBalance.store_id)
        .where(Store.enterprise_id == enterprise_id, InventoryBalance.product_id == product_id)
    ).one()
    stockout, stockout_cutoff = db.execute(
        select(
            func.coalesce(func.sum(StockoutDemandProjection.requested_quantity), 0),
            func.max(StockoutDemandProjection.business_at),
        )
        .join(Store, Store.id == StockoutDemandProjection.store_id)
        .where(
            Store.enterprise_id == enterprise_id,
            StockoutDemandProjection.product_id == product_id,
            StockoutDemandProjection.status.in_(("SUBMITTED", "OPEN", "CONFIRMED")),
        )
    ).one()
    cutoffs = [as_utc(value) for value in (inventory_cutoff, stockout_cutoff) if value is not None]
    return float(inventory or 0), float(stockout or 0), cutoffs


def generate_next_week_forecasts(
    db: Session,
    *,
    scheduled: bool = False,
    now: datetime | None = None,
) -> tuple[DemandForecastBatch, list[DemandForecastProjection], int]:
    forecast_start, forecast_end = next_week_window(now)
    as_of = forecast_start - timedelta(days=1)
    enterprises = list(db.scalars(select(Enterprise).where(Enterprise.enabled.is_(True)).order_by(Enterprise.id)))
    products = list(db.scalars(select(Product).order_by(Product.id)))
    projections: list[DemandForecastProjection] = []
    changed = 0
    for enterprise in enterprises:
        for product in products:
            result = forecast_demand(
                db,
                enterprise.id,
                product.id,
                as_of,
                forecast_start=forecast_start,
                forecast_end=forecast_end,
            )
            inventory, stockout, operational_cutoffs = _inventory_and_stockout(db, enterprise.id, product.id)
            forecast_quantity = result.get("forecast_quantity")
            demand_quantity = float(forecast_quantity or result.get("production_plan_quantity") or 0)
            suggested = max(demand_quantity + stockout - inventory, 0.0)
            forecast_cutoff = _cutoff_from_text(result.get("data_cutoff"))
            cutoffs = [*operational_cutoffs, *([forecast_cutoff] if forecast_cutoff else [])]
            values = {
                "forecast_end": forecast_end,
                "historical_usage": float(result.get("historical_usage") or 0),
                "production_plan_quantity": float(result.get("production_plan_quantity") or 0),
                "forecast_quantity": float(forecast_quantity) if forecast_quantity is not None else None,
                "suggested_purchase_quantity": round(suggested, 2),
                "lower_bound": result.get("lower_bound"),
                "upper_bound": result.get("upper_bound"),
                "mae": result.get("mae"),
                "smape": result.get("smape"),
                "method": result["method"],
                "method_note": result["method_note"],
                "data_cutoff": max(cutoffs) if cutoffs else None,
                "input_snapshot": {
                    "as_of": as_of.isoformat(),
                    "data_points": result.get("data_points", 0),
                    "available_inventory": round(inventory, 2),
                    "unmet_stockout": round(stockout, 2),
                    "rules_version": FORECAST_RULES_VERSION,
                },
            }
            projection = db.scalar(
                select(DemandForecastProjection).where(
                    DemandForecastProjection.enterprise_id == enterprise.id,
                    DemandForecastProjection.product_id == product.id,
                    DemandForecastProjection.forecast_start == forecast_start,
                )
            )
            if projection is None:
                projection = DemandForecastProjection(
                    enterprise_id=enterprise.id,
                    product_id=product.id,
                    forecast_start=forecast_start,
                    **values,
                )
                db.add(projection)
                db.flush()
                changed += 1
            else:
                current = {
                    key: getattr(projection, key)
                    for key in (
                        "forecast_end",
                        "historical_usage",
                        "production_plan_quantity",
                        "forecast_quantity",
                        "suggested_purchase_quantity",
                        "lower_bound",
                        "upper_bound",
                        "mae",
                        "smape",
                        "method",
                        "method_note",
                        "data_cutoff",
                        "input_snapshot",
                    )
                }
                if current["data_cutoff"] is not None:
                    current["data_cutoff"] = as_utc(current["data_cutoff"])
                if current != values:
                    atomic_versioned_update(
                        db,
                        DemandForecastProjection,
                        projection.id,
                        projection.object_version,
                        values,
                    )
                    projection = db.scalar(
                        select(DemandForecastProjection)
                        .where(DemandForecastProjection.id == projection.id)
                        .execution_options(populate_existing=True)
                    )
                    changed += 1
            projections.append(projection)

    aggregates: list[dict[str, Any]] = []
    for product in products:
        product_rows = [item for item in projections if item.product_id == product.id]
        available = [item for item in product_rows if item.forecast_quantity is not None]
        aggregates.append(
            {
                "product_id": product.id,
                "product_name": product.name,
                "unit": product.unit,
                "enterprise_count": len(product_rows),
                "available_forecast_count": len(available),
                "historical_usage": round(sum(item.historical_usage for item in product_rows), 2),
                "production_plan_quantity": round(
                    sum(item.production_plan_quantity for item in product_rows), 2
                ),
                "forecast_quantity": round(sum(item.forecast_quantity or 0 for item in product_rows), 2),
                "suggested_purchase_quantity": round(
                    sum(item.suggested_purchase_quantity for item in product_rows), 2
                ),
                "lower_bound": round(sum(item.lower_bound or 0 for item in product_rows), 2),
                "upper_bound": round(sum(item.upper_bound or 0 for item in product_rows), 2),
                "data_insufficient_count": sum(item.method == "INSUFFICIENT_DATA" for item in product_rows),
            }
        )
    batch_cutoff = max(
        (as_utc(item.data_cutoff) for item in projections if item.data_cutoff),
        default=None,
    )
    signature_payload = {
        "forecast_start": forecast_start.isoformat(),
        "forecast_end": forecast_end.isoformat(),
        "aggregates": aggregates,
        "versions": {item.id: item.object_version for item in projections},
        "data_cutoff": batch_cutoff.isoformat() if batch_cutoff else None,
    }
    signature = hashlib.sha256(
        json.dumps(signature_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    batch = db.scalar(select(DemandForecastBatch).where(DemandForecastBatch.forecast_start == forecast_start))
    batch_values = {
        "forecast_end": forecast_end,
        "rules_version": FORECAST_RULES_VERSION,
        "data_signature": signature,
        "item_count": len(projections),
        "aggregate_snapshot": aggregates,
        "data_cutoff": batch_cutoff,
        "status": "READY",
        "scheduled": scheduled,
    }
    if batch is None:
        batch = DemandForecastBatch(forecast_start=forecast_start, **batch_values)
        db.add(batch)
        db.flush()
        changed += 1
    elif batch.data_signature != signature or (scheduled and not batch.scheduled):
        atomic_versioned_update(
            db,
            DemandForecastBatch,
            batch.id,
            batch.object_version,
            batch_values,
        )
        batch = db.scalar(
            select(DemandForecastBatch)
            .where(DemandForecastBatch.id == batch.id)
            .execution_options(populate_existing=True)
        )
        changed += 1
    return batch, projections, changed
