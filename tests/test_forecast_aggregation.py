from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select

from app.domain.forecasts import next_week_window
from app.scheduler import run_scheduled_jobs
from app.shared.database import SessionLocal
from app.shared.models import DemandForecastBatch, Enterprise, Product, ProductionPlan


def test_next_week_forecast_batch_fallback_metrics_and_scheduler_idempotency(b01_client, admin_headers):
    forecast_start, forecast_end = next_week_window()
    enterprise_id = str(uuid4())
    with SessionLocal() as db:
        product = db.scalar(select(Product).order_by(Product.code))
        db.add(Enterprise(id=enterprise_id, code=f"F{uuid4().hex[:8]}", name="预测降级测试企业"))
        db.add(
            ProductionPlan(
                enterprise_id=enterprise_id,
                product_id=product.id,
                plan_date=forecast_start,
                planned_quantity=88,
            )
        )
        db.commit()

    key = f"forecast-generate-{uuid4()}"
    generated = b01_client.post(
        "/api/v1/web/forecasts/next-week/generate",
        headers={**admin_headers, "Idempotency-Key": key},
    )
    assert generated.status_code == 201, generated.text
    body = generated.json()
    assert body["period"] == {"start": forecast_start.isoformat(), "end": forecast_end.isoformat()}
    assert body["batch"]["rules_version"] == "next-week-aggregate-v1"
    assert body["data_cutoff"]
    assert len(body["aggregates"]) == 8
    fallback = next(
        item
        for item in body["items"]
        if item["enterprise_id"] == enterprise_id and item["product_id"] == product.id
    )
    assert fallback["method"] == "PRODUCTION_PLAN_FALLBACK"
    assert fallback["forecast_quantity"] == 88
    assert fallback["lower_bound"] < fallback["forecast_quantity"] < fallback["upper_bound"]
    assert fallback["mae"] is None
    assert fallback["smape"] is None
    insufficient = next(
        item
        for item in body["items"]
        if item["enterprise_id"] == enterprise_id and item["product_id"] != product.id
    )
    assert insufficient["method"] == "INSUFFICIENT_DATA"
    assert insufficient["forecast_quantity"] is None

    repeated = b01_client.post(
        "/api/v1/web/forecasts/next-week/generate",
        headers={**admin_headers, "Idempotency-Key": key},
    )
    assert repeated.status_code == 201
    assert repeated.json()["items"] == body["items"]

    listed = b01_client.get("/api/v1/web/forecasts/next-week", headers=admin_headers)
    assert listed.status_code == 200
    assert listed.json()["batch"]["id"] == body["batch"]["id"]
    assert all("suggested_purchase_quantity" in item for item in listed.json()["items"])
    assert all("method_note" in item for item in listed.json()["items"])

    first_schedule = run_scheduled_jobs()
    second_schedule = run_scheduled_jobs()
    assert first_schedule["forecasts"] == len(body["items"])
    assert first_schedule["forecast_changes"] >= 1
    assert second_schedule["forecast_changes"] == 0
    with SessionLocal() as db:
        batches = list(
            db.scalars(select(DemandForecastBatch).where(DemandForecastBatch.forecast_start == forecast_start))
        )
        assert len(batches) == 1
        assert batches[0].scheduled is True
