from __future__ import annotations

from uuid import uuid4

from sqlalchemy import func, select

from app.domain.procurement import procurement_cycle
from app.shared.database import SessionLocal
from app.shared.models import Enterprise, ProcurementAggregation, Product


def test_multi_enterprise_procurement_aggregation_and_confirmation(b01_client, admin_headers):
    cycle_start, cycle_end = procurement_cycle()
    with SessionLocal() as db:
        enterprises = list(db.scalars(select(Enterprise).order_by(Enterprise.code).limit(2)))
        product = db.scalar(select(Product).where(Product.unit == "kg").order_by(Product.code))

    manual = b01_client.post(
        "/api/v1/web/procurement/demands",
        json={
            "enterprise_id": enterprises[0].id,
            "product_id": product.id,
            "cycle_start": cycle_start.isoformat(),
            "quantity": 1.2,
            "unit": "吨",
            "reason": "企业已确认下周生产需求",
        },
        headers={**admin_headers, "Idempotency-Key": f"proc-demand-{uuid4()}"},
    )
    assert manual.status_code == 201, manual.text
    incompatible = b01_client.post(
        "/api/v1/web/procurement/demands",
        json={
            "enterprise_id": enterprises[1].id,
            "product_id": product.id,
            "cycle_start": cycle_start.isoformat(),
            "quantity": 10,
            "unit": "箱",
            "reason": "原始系统尚未配置箱规",
        },
        headers={**admin_headers, "Idempotency-Key": f"proc-demand-{uuid4()}"},
    )
    assert incompatible.status_code == 201, incompatible.text

    key = f"proc-generate-{uuid4()}"
    generated = b01_client.post(
        "/api/v1/web/procurement/aggregations/generate",
        json={"cycle_start": cycle_start.isoformat()},
        headers={**admin_headers, "Idempotency-Key": key},
    )
    assert generated.status_code == 201, generated.text
    body = generated.json()
    assert body["cycle_start"] == cycle_start.isoformat()
    assert body["cycle_end"] == cycle_end.isoformat()
    assert body["rules_version"] == "procurement-aggregation-v1"
    assert body["data_cutoff"]
    target = next(item for item in body["items"] if item["product_id"] == product.id)
    enterprise_rows = target["input_snapshot"]["enterprise_demands"]
    manual_row = next(item for item in enterprise_rows if item["enterprise_id"] == enterprises[0].id)
    assert manual_row["manual_confirmed_quantity"] == 1200
    assert manual_row["selected_source"] == "MANUAL_CONFIRMED"
    assert target["unit_conversion_warnings"]
    assert any(item["supplier_id"] for item in target["candidates"])
    assert all("recommendation_reason" in item for item in target["candidates"])

    repeated = b01_client.post(
        "/api/v1/web/procurement/aggregations/generate",
        json={"cycle_start": cycle_start.isoformat()},
        headers={**admin_headers, "Idempotency-Key": key},
    )
    assert repeated.status_code == 201
    assert repeated.json()["items"] == body["items"]
    with SessionLocal() as db:
        assert (
            db.scalar(
                select(func.count()).select_from(ProcurementAggregation).where(
                    ProcurementAggregation.cycle_start == cycle_start
                )
            )
            == len(body["items"])
        )

    confirmed = b01_client.post(
        f"/api/v1/web/procurement/aggregations/{target['id']}/confirm",
        json={
            "object_version": target["object_version"],
            "adjusted_quantity": target["automatic_quantity"] + 25,
            "adjustment_reason": "考虑安全库存增加二十五公斤",
        },
        headers={**admin_headers, "Idempotency-Key": f"proc-confirm-{uuid4()}"},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "CONFIRMED"
    assert confirmed.json()["confirmed_quantity"] == target["automatic_quantity"] + 25

    stale = b01_client.post(
        f"/api/v1/web/procurement/aggregations/{target['id']}/confirm",
        json={"object_version": target["object_version"]},
        headers={**admin_headers, "Idempotency-Key": f"proc-stale-{uuid4()}"},
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "VERSION_CONFLICT"

    listed = b01_client.get(
        f"/api/v1/web/procurement/aggregations?cycle_start={cycle_start.isoformat()}",
        headers=admin_headers,
    )
    assert listed.status_code == 200
    listed_target = next(item for item in listed.json()["items"] if item["id"] == target["id"])
    assert listed_target["status"] == "CONFIRMED"
    assert listed_target["adjustment_reason"] == "考虑安全库存增加二十五公斤"
