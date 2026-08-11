from __future__ import annotations

from sqlalchemy import select, update

from app.domain.dashboard import refresh_all_dashboard_projections
from app.domain.forecasts import generate_next_week_forecasts
from app.domain.services import add_audit, add_outbox
from app.domain.voice_service import cleanup_expired_voice_requests
from app.shared.database import SessionLocal
from app.shared.errors import BusinessError
from app.shared.models import (
    TransportPlan,
    UserSession,
    Warehouse,
    WarehousePoolPlan,
    WarehouseReservation,
    utcnow,
)
from app.shared.optimistic import atomic_versioned_update


def _expire_warehouse_plan(db, plan: WarehousePoolPlan, now) -> bool:
    reservation = db.scalar(
        select(WarehouseReservation).where(
            WarehouseReservation.warehouse_pool_plan_id == plan.id,
            WarehouseReservation.status == "RESERVED",
            WarehouseReservation.expires_at <= now,
        )
    )
    if reservation is None:
        return False
    warehouse = db.get(Warehouse, plan.warehouse_id)
    if warehouse is None:
        raise RuntimeError("过期拼仓计划引用的仓库不存在")
    volume = reservation.reserved_volume_m3
    next_warehouse_version = atomic_versioned_update(
        db,
        Warehouse,
        warehouse.id,
        warehouse.object_version,
        {"reserved_m3": Warehouse.reserved_m3 - volume},
        conditions=(Warehouse.reserved_m3 >= volume,),
    )
    next_reservation_version = atomic_versioned_update(
        db,
        WarehouseReservation,
        reservation.id,
        reservation.object_version,
        {"status": "EXPIRED", "released_at": now},
        conditions=(
            WarehouseReservation.status == "RESERVED",
            WarehouseReservation.expires_at <= now,
        ),
    )
    next_plan_version = atomic_versioned_update(
        db,
        WarehousePoolPlan,
        plan.id,
        plan.object_version,
        {"status": "EXPIRED"},
        conditions=(
            WarehousePoolPlan.status == "RESERVED",
            WarehousePoolPlan.reservation_expires_at <= now,
        ),
    )
    payload = {
        "plan_id": plan.id,
        "warehouse_id": warehouse.id,
        "warehouse_object_version": next_warehouse_version,
        "reservation_object_version": next_reservation_version,
        "status": "EXPIRED",
        "expired_at": now.isoformat(),
    }
    add_outbox(db, "warehouse.pool.expired", "warehouse_pool_plan", plan.id, next_plan_version, payload)
    add_audit(
        db,
        plan.id,
        None,
        "EXPIRE_WAREHOUSE_RESERVATION",
        "warehouse_pool_plan",
        plan.id,
        {"status": "RESERVED", "object_version": plan.object_version},
        {**payload, "object_version": next_plan_version},
    )
    return True


def run_scheduled_jobs() -> dict[str, int]:
    now = utcnow()
    forecast_count = 0
    with SessionLocal() as db:
        voice_cleanup = cleanup_expired_voice_requests(db, now=now, commit=False)
        db.execute(
            update(UserSession)
            .where(UserSession.revoked_at.is_(None), UserSession.expires_at <= now)
            .values(revoked_at=now)
        )
        expired_plans = list(
            db.scalars(
                select(TransportPlan).where(
                    TransportPlan.status == "PUBLISHED",
                    TransportPlan.published_qr_token.is_not(None),
                    TransportPlan.qr_expires_at <= now,
                )
            )
        )
        expired_count = 0
        for plan in expired_plans:
            try:
                atomic_versioned_update(
                    db,
                    TransportPlan,
                    plan.id,
                    plan.object_version,
                    {"published_qr_token": None},
                    conditions=(
                        TransportPlan.status == "PUBLISHED",
                        TransportPlan.published_qr_token.is_not(None),
                        TransportPlan.qr_expires_at <= now,
                    ),
                )
                expired_count += 1
            except BusinessError as exc:
                if exc.code != "VERSION_CONFLICT":
                    raise

        expired_warehouse_plans = list(
            db.scalars(
                select(WarehousePoolPlan).where(
                    WarehousePoolPlan.status == "RESERVED",
                    WarehousePoolPlan.reservation_expires_at <= now,
                )
            )
        )
        expired_warehouse_count = 0
        for plan in expired_warehouse_plans:
            try:
                with db.begin_nested():
                    if _expire_warehouse_plan(db, plan, now):
                        expired_warehouse_count += 1
            except BusinessError as exc:
                if exc.code not in {"VERSION_CONFLICT", "STATE_GUARD_CONFLICT"}:
                    raise
                db.expire_all()

        forecast_batch, forecast_items, forecast_changes = generate_next_week_forecasts(db, scheduled=True, now=now)
        forecast_count = len(forecast_items)
        if forecast_changes:
            add_outbox(
                db,
                "forecast.next_week.generated",
                "demand_forecast_batch",
                forecast_batch.id,
                forecast_batch.object_version,
                {
                    "batch_id": forecast_batch.id,
                    "forecast_start": forecast_batch.forecast_start.isoformat(),
                    "forecast_end": forecast_batch.forecast_end.isoformat(),
                    "item_count": len(forecast_items),
                    "changed_count": forecast_changes,
                    "scheduled_time": "02:10 Asia/Shanghai",
                },
            )
        db.commit()
    with SessionLocal() as projection_db:
        try:
            refresh_all_dashboard_projections(projection_db)
        except BusinessError:
            projection_db.rollback()
    return {
        "forecasts": forecast_count,
        "forecast_changes": forecast_changes,
        "expired_qr_tokens_cleared": expired_count,
        "expired_warehouse_reservations": expired_warehouse_count,
        "expired_voice_requests": voice_cleanup["deleted"],
        "expired_voice_processing": voice_cleanup["expired_processing"],
    }


def main() -> None:
    print(run_scheduled_jobs())


if __name__ == "__main__":
    main()
