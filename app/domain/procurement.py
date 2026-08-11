from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.algorithms import forecast_demand, run_procurement
from app.shared.errors import BusinessError
from app.shared.models import (
    DemandHistory,
    Enterprise,
    InventoryBalance,
    ProcurementAggregation,
    ProcurementDemandConfirmation,
    Product,
    ProductionPlan,
    StockoutDemandProjection,
    Store,
    utcnow,
)
from app.shared.optimistic import atomic_versioned_update
from app.shared.periods import SHANGHAI
from app.shared.security import as_utc

PROCUREMENT_RULES_VERSION = "procurement-aggregation-v1"
UNIT_RULES_VERSION = "unit-conversion-v1"


def procurement_cycle(cycle_start: date | None = None, now: datetime | None = None) -> tuple[date, date]:
    if cycle_start is None:
        local_date = (now or utcnow()).astimezone(SHANGHAI).date()
        cycle_start = local_date + timedelta(days=(7 - local_date.weekday()))
    if cycle_start.weekday() != 0:
        raise BusinessError(
            "PROCUREMENT_CYCLE_MUST_START_MONDAY",
            "采购周期开始日期必须是星期一",
            status_code=422,
        )
    return cycle_start, cycle_start + timedelta(days=6)


def convert_to_base_unit(quantity: float, unit: str, base_unit: str) -> tuple[float | None, str | None]:
    source = unit.strip().lower()
    target = base_unit.strip().lower()
    aliases = {"千克": "kg", "公斤": "kg", "吨": "t", "克": "g"}
    source = aliases.get(source, source)
    target = aliases.get(target, target)
    if source == target:
        return float(quantity), None
    factors = {("t", "kg"): 1000.0, ("g", "kg"): 0.001, ("kg", "t"): 0.001}
    factor = factors.get((source, target))
    if factor is None:
        return None, f"{unit} 无法换算为商品基础单位 {base_unit}"
    return float(quantity) * factor, None


def aggregation_data(item: ProcurementAggregation, product: Product | None = None) -> dict[str, Any]:
    effective_quantity = item.adjusted_quantity if item.adjusted_quantity is not None else item.automatic_quantity
    return {
        "id": item.id,
        "product_id": item.product_id,
        "product_name": product.name if product else None,
        "cycle_start": item.cycle_start.isoformat(),
        "cycle_end": item.cycle_end.isoformat(),
        "base_unit": item.base_unit,
        "automatic_quantity": item.automatic_quantity,
        "adjusted_quantity": item.adjusted_quantity,
        "effective_quantity": effective_quantity,
        "status": item.status,
        "rules_version": item.rules_version,
        "input_snapshot": item.input_snapshot,
        "candidates": item.candidate_snapshot,
        "recommendation": item.recommendation_snapshot,
        "unit_conversion_warnings": item.unit_conversion_warnings,
        "data_cutoff": as_utc(item.data_cutoff).isoformat() if item.data_cutoff else None,
        "adjustment_reason": item.adjustment_reason,
        "confirmed_by": item.confirmed_by,
        "confirmed_at": item.confirmed_at.isoformat() if item.confirmed_at else None,
        "object_version": item.object_version,
        "generated_at": item.updated_at.isoformat(),
        "unit": item.base_unit,
    }


def _date_cutoff(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=SHANGHAI).astimezone(UTC)


def _enterprise_product_input(
    db: Session,
    enterprise: Enterprise,
    product: Product,
    cycle_start: date,
    cycle_end: date,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[datetime]]:
    warnings: list[dict[str, Any]] = []
    cutoffs: list[datetime] = []
    manual = db.scalar(
        select(ProcurementDemandConfirmation).where(
            ProcurementDemandConfirmation.enterprise_id == enterprise.id,
            ProcurementDemandConfirmation.product_id == product.id,
            ProcurementDemandConfirmation.cycle_start == cycle_start,
            ProcurementDemandConfirmation.status == "CONFIRMED",
        )
    )
    manual_quantity: float | None = None
    if manual is not None:
        manual_quantity, warning = convert_to_base_unit(manual.quantity, manual.unit, product.unit)
        cutoffs.append(as_utc(manual.updated_at))
        if warning:
            warnings.append(
                {
                    "enterprise_id": enterprise.id,
                    "product_id": product.id,
                    "quantity": manual.quantity,
                    "unit": manual.unit,
                    "reason": warning,
                }
            )

    forecast = forecast_demand(db, enterprise.id, product.id, cycle_start - timedelta(days=1))
    forecast_quantity = float(forecast.get("forecast_quantity") or 0)
    history_cutoff = db.scalar(
        select(func.max(DemandHistory.period_start)).where(
            DemandHistory.enterprise_id == enterprise.id,
            DemandHistory.product_id == product.id,
            DemandHistory.period_start < cycle_start,
        )
    )
    if history_cutoff:
        cutoffs.append(_date_cutoff(history_cutoff))

    production_quantity = float(
        db.scalar(
            select(func.coalesce(func.sum(ProductionPlan.planned_quantity), 0)).where(
                ProductionPlan.enterprise_id == enterprise.id,
                ProductionPlan.product_id == product.id,
                ProductionPlan.plan_date.between(cycle_start, cycle_end),
            )
        )
        or 0
    )
    production_cutoff = db.scalar(
        select(func.max(ProductionPlan.plan_date)).where(
            ProductionPlan.enterprise_id == enterprise.id,
            ProductionPlan.product_id == product.id,
            ProductionPlan.plan_date.between(cycle_start, cycle_end),
        )
    )
    if production_cutoff:
        cutoffs.append(_date_cutoff(production_cutoff))

    stockout_quantity, stockout_cutoff = db.execute(
        select(
            func.coalesce(func.sum(StockoutDemandProjection.requested_quantity), 0),
            func.max(StockoutDemandProjection.business_at),
        )
        .join(Store, Store.id == StockoutDemandProjection.store_id)
        .where(
            Store.enterprise_id == enterprise.id,
            StockoutDemandProjection.product_id == product.id,
            StockoutDemandProjection.status.in_(("SUBMITTED", "OPEN", "CONFIRMED")),
        )
    ).one()
    if stockout_cutoff:
        cutoffs.append(as_utc(stockout_cutoff))

    inventory_quantity, inventory_cutoff = db.execute(
        select(func.coalesce(func.sum(InventoryBalance.quantity), 0), func.max(InventoryBalance.updated_at))
        .join(Store, Store.id == InventoryBalance.store_id)
        .where(Store.enterprise_id == enterprise.id, InventoryBalance.product_id == product.id)
    ).one()
    if inventory_cutoff:
        cutoffs.append(as_utc(inventory_cutoff))

    if manual is not None and manual_quantity is not None:
        selected_quantity = manual_quantity
        selected_source = "MANUAL_CONFIRMED"
    else:
        selected_quantity = max(forecast_quantity, production_quantity)
        selected_source = "FORECAST" if forecast_quantity >= production_quantity else "PRODUCTION_PLAN"
    suggested = max(selected_quantity + float(stockout_quantity or 0) - float(inventory_quantity or 0), 0.0)
    return (
        {
            "enterprise_id": enterprise.id,
            "enterprise_name": enterprise.name,
            "product_id": product.id,
            "forecast_quantity": round(forecast_quantity, 2),
            "production_plan_quantity": round(production_quantity, 2),
            "manual_confirmed_quantity": round(manual_quantity, 2) if manual_quantity is not None else None,
            "manual_original": (
                {"quantity": manual.quantity, "unit": manual.unit, "reason": manual.reason} if manual else None
            ),
            "selected_source": selected_source,
            "selected_quantity": round(selected_quantity, 2),
            "unmet_stockout_quantity": round(float(stockout_quantity or 0), 2),
            "available_inventory_quantity": round(float(inventory_quantity or 0), 2),
            "suggested_quantity": round(suggested, 2),
            "unit": product.unit,
        },
        warnings,
        cutoffs,
    )


def generate_procurement_aggregations(
    db: Session,
    cycle_start: date | None = None,
) -> tuple[date, date, list[ProcurementAggregation]]:
    cycle_start, cycle_end = procurement_cycle(cycle_start)
    enterprises = list(db.scalars(select(Enterprise).where(Enterprise.enabled.is_(True)).order_by(Enterprise.id)))
    products = list(db.scalars(select(Product).order_by(Product.id)))
    results: list[ProcurementAggregation] = []
    for product in products:
        breakdown: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        cutoffs: list[datetime] = []
        for enterprise in enterprises:
            item, item_warnings, item_cutoffs = _enterprise_product_input(
                db, enterprise, product, cycle_start, cycle_end
            )
            breakdown.append(item)
            warnings.extend(item_warnings)
            cutoffs.extend(item_cutoffs)
        automatic_quantity = round(sum(item["suggested_quantity"] for item in breakdown), 2)
        recommendation = run_procurement(db, product.id, automatic_quantity, cycle_start)
        input_snapshot = {
            "cycle_start": cycle_start.isoformat(),
            "cycle_end": cycle_end.isoformat(),
            "product_id": product.id,
            "base_unit": product.unit,
            "unit_rules_version": UNIT_RULES_VERSION,
            "enterprise_demands": breakdown,
        }
        values = {
            "cycle_end": cycle_end,
            "base_unit": product.unit,
            "automatic_quantity": automatic_quantity,
            "rules_version": PROCUREMENT_RULES_VERSION,
            "input_snapshot": input_snapshot,
            "candidate_snapshot": recommendation["candidates"],
            "recommendation_snapshot": recommendation["recommendation"],
            "unit_conversion_warnings": warnings,
            "data_cutoff": max(cutoffs) if cutoffs else None,
        }
        aggregation = db.scalar(
            select(ProcurementAggregation).where(
                ProcurementAggregation.product_id == product.id,
                ProcurementAggregation.cycle_start == cycle_start,
            )
        )
        if aggregation is None:
            aggregation = ProcurementAggregation(
                product_id=product.id,
                cycle_start=cycle_start,
                status="DRAFT",
                **values,
            )
            db.add(aggregation)
            db.flush()
        elif aggregation.status == "DRAFT":
            atomic_versioned_update(
                db,
                ProcurementAggregation,
                aggregation.id,
                aggregation.object_version,
                values,
                conditions=(ProcurementAggregation.status == "DRAFT",),
            )
            aggregation = db.scalar(
                select(ProcurementAggregation)
                .where(ProcurementAggregation.id == aggregation.id)
                .execution_options(populate_existing=True)
            )
        results.append(aggregation)
    return cycle_start, cycle_end, results
