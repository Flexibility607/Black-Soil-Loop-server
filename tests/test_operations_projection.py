from __future__ import annotations

from datetime import date, timedelta
from uuid import uuid4

from sqlalchemy import func, select

from app.domain.services import add_outbox
from app.shared.database import SessionLocal
from app.shared.models import (
    InventoryMovement,
    InventoryMovementProjection,
    Product,
    Receipt,
    Store,
    TransportTask,
    User,
    utcnow,
)
from app.shared.periods import period_window
from app.worker import process_pending_once
from tests.conftest import login


def _bearer(body: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {body['access_token']}"}


def test_worker_projects_operations_and_web_lists_them(b01_client, b02_client):
    suffix = uuid4().hex[:10]
    with SessionLocal() as db:
        task = db.scalar(
            select(TransportTask)
            .where(~select(Receipt.id).where(Receipt.task_id == TransportTask.id).exists())
            .order_by(TransportTask.id)
        )
        product = db.scalar(select(Product).order_by(Product.id))
        actor = db.scalar(select(User).where(User.username == "admin"))
        assert task and product and actor
        receipt = Receipt(
            task_id=task.id,
            store_id=task.store_id,
            receipt_status="PARTIAL",
            details=[
                {
                    "product_id": product.id,
                    "expected_quantity": 12.0,
                    "received_quantity": 9.0,
                    "note": "外包装破损三件",
                }
            ],
            idempotency_key=f"projection-receipt-{suffix}",
            actor_user_id=actor.id,
            received_at=utcnow(),
        )
        db.add(receipt)
        db.flush()
        db.add(
            InventoryMovement(
                store_id=task.store_id,
                product_id=product.id,
                movement_type="IN",
                quantity_before=20.0,
                quantity_delta=9.0,
                quantity_after=29.0,
                source_type="RECEIPT",
                source_id=receipt.id,
                idempotency_key=f"projection-movement-{suffix}",
            )
        )
        add_outbox(
            db,
            "store.receipt.completed",
            "receipt",
            receipt.id,
            1,
            {"receipt_id": receipt.id},
        )
        db.commit()

    manager = login(b02_client, "/api/v1/mobile", "manager_t001")
    manager_headers = _bearer(manager)
    products = b01_client.get(
        "/api/v1/web/master-data/products",
        headers=_bearer(login(b01_client, "/api/v1/web", "admin")),
    ).json()["items"]
    product_id = products[0]["id"]
    stockout = b02_client.post(
        "/api/v1/mobile/stockouts",
        json={"product_id": product_id, "requested_quantity": 18.0, "reason": "周末客流增加"},
        headers={**manager_headers, "Idempotency-Key": f"projection-stockout-{suffix}"},
    )
    assert stockout.status_code == 201, stockout.text
    report = b02_client.post(
        "/api/v1/mobile/daily-reports",
        json={
            "report_date": (date.today() - timedelta(days=20)).isoformat(),
            "sales_amount": "5680.50",
            "order_count": 73,
            "authorized_for_dashboard": True,
            "summary": {"source": "operations-projection-test"},
        },
        headers={**manager_headers, "Idempotency-Key": f"projection-report-{suffix}"},
    )
    assert report.status_code == 201, report.text

    while process_pending_once():
        pass

    admin_headers = _bearer(login(b01_client, "/api/v1/web", "admin"))
    receipts = b01_client.get("/api/v1/web/receipts", headers=admin_headers)
    assert receipts.status_code == 200, receipts.text
    projected_receipt = next(
        item for item in receipts.json()["items"] if item["source_receipt_id"] == receipt.id
    )
    assert projected_receipt["receipt_status_label"] == "差异签收"
    assert projected_receipt["difference_total"] == 3.0
    assert receipts.json()["data_cutoff"]

    movements = b01_client.get("/api/v1/web/inventory-movements", headers=admin_headers)
    assert movements.status_code == 200, movements.text
    projected_movement = next(
        item for item in movements.json()["items"] if item["source_id"] == receipt.id
    )
    assert projected_movement["quantity_before"] == 20.0
    assert projected_movement["quantity_after"] == 29.0
    assert projected_movement["quantity_context"] == "RECORDED"
    assert projected_movement["quantity_context_label"] == "业务写入时记录"
    window = period_window("30d")
    with SessionLocal() as db:
        movement_cutoff = db.scalar(
            select(func.max(InventoryMovementProjection.business_at)).where(
                InventoryMovementProjection.business_at >= window.start_at,
                InventoryMovementProjection.business_at < window.end_at,
            )
        )
    assert movement_cutoff is not None
    assert movements.json()["data_cutoff"] == movement_cutoff.isoformat()

    stockouts = b01_client.get("/api/v1/web/stockout-demands", headers=admin_headers)
    assert stockouts.status_code == 200, stockouts.text
    assert any(item["source_stockout_id"] == stockout.json()["id"] for item in stockouts.json()["items"])

    reports = b01_client.get("/api/v1/web/store-daily-reports", headers=admin_headers)
    assert reports.status_code == 200, reports.text
    assert any(item["source_report_id"] == report.json()["id"] for item in reports.json()["items"])

    summary = b01_client.get("/api/v1/web/operations/summary", headers=admin_headers)
    assert summary.status_code == 200, summary.text
    body = summary.json()
    assert body["period"] == "30d"
    assert body["generated_at"]
    assert body["data_cutoff"]
    assert body["receipts"]["difference_count"] >= 1
    assert body["stockouts"]["third_space"]


def test_operations_require_web_management_role(b01_client, b02_client):
    driver = login(b02_client, "/api/v1/mobile", "driver_demo")
    response = b01_client.get("/api/v1/web/operations/summary", headers=_bearer(driver))
    assert response.status_code == 403


def test_enterprise_user_cannot_select_another_enterprise_store(b01_client):
    enterprise = login(b01_client, "/api/v1/web", "enterprise_demo")
    enterprise_id = enterprise["user"]["enterprise_id"]
    with SessionLocal() as db:
        another_store = db.scalar(select(Store).where(Store.enterprise_id != enterprise_id))
        assert another_store
    response = b01_client.get(
        f"/api/v1/web/stockout-demands?store_id={another_store.id}",
        headers=_bearer(enterprise),
    )
    assert response.status_code == 200
    assert response.json()["items"] == []
