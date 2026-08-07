from __future__ import annotations

from datetime import date

from sqlalchemy import select, update

from app.domain.algorithms import forecast_demand
from app.domain.dashboard import refresh_dashboard_projection
from app.shared.database import SessionLocal
from app.shared.models import AlgorithmRun, DemandHistory, TransportPlan, UserSession, utcnow


def run_scheduled_jobs() -> dict[str, int]:
    now = utcnow()
    forecast_count = 0
    with SessionLocal() as db:
        db.execute(
            update(UserSession)
            .where(UserSession.revoked_at.is_(None), UserSession.expires_at <= now)
            .values(revoked_at=now)
        )
        expired_plans = list(
            db.scalars(
                select(TransportPlan).where(
                    TransportPlan.published_qr_token.is_not(None),
                    TransportPlan.qr_expires_at <= now,
                )
            )
        )
        for plan in expired_plans:
            plan.published_qr_token = None
            plan.object_version += 1

        pairs = list(db.execute(select(DemandHistory.enterprise_id, DemandHistory.product_id).distinct()))
        for enterprise_id, product_id in pairs:
            result = forecast_demand(db, enterprise_id, product_id, date.today())
            db.add(
                AlgorithmRun(
                    algorithm_type="DEMAND_FORECAST",
                    rules_version="forecast-auto-v1",
                    input_snapshot={"enterprise_id": enterprise_id, "product_id": product_id, "scheduled": True},
                    output_snapshot=result,
                )
            )
            forecast_count += 1
        refresh_dashboard_projection(db, commit=False)
        db.commit()
    return {"forecasts": forecast_count, "expired_qr_tokens_cleared": len(expired_plans)}


def main() -> None:
    print(run_scheduled_jobs())


if __name__ == "__main__":
    main()
