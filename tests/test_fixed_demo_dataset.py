from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import app.domain.dashboard as dashboard_domain
import app.domain.fixed_demo as fixed_demo
import app.showcase_scheduler as showcase_scheduler
import app.worker as worker
from app.domain.assistant import answer_data_question
from app.domain.dashboard import read_dashboard_projection
from app.domain.services import sign_receipt, transition_task
from app.shared.database import SCHEMAS, Base
from app.shared.dependencies import get_current_user
from app.shared.errors import BusinessError
from app.shared.models import (
    Alert,
    DemandForecastProjection,
    DemoCaseInstallation,
    InventoryBalance,
    InventoryMovement,
    OutboxEvent,
    ProcurementAggregation,
    Receipt,
    StockoutRequest,
    StoreDailyReport,
    TaskStop,
    TelemetryIssue,
    TransportPlan,
    TransportTask,
    User,
)
from app.shared.security import decode_token, issue_tokens

FORBIDDEN_SHOWCASE_KEYS = {
    "id",
    "object_version",
    "input_snapshot",
    "output_snapshot",
    "rules_version",
    "confirmed_by",
    "trace_id",
    "scenario_code",
}
UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)


@pytest.fixture
def test_artifact_dir():
    target = Path(__file__).resolve().parents[1] / ".test-artifacts" / uuid4().hex
    target.mkdir(parents=True)
    yield target
    shutil.rmtree(target, ignore_errors=True)


@pytest.fixture
def showcase_database(test_artifact_dir: Path, monkeypatch: pytest.MonkeyPatch):
    database_path = test_artifact_dir / "black_soil_loop_showcase.sqlite3"
    engine = create_engine(
        f"sqlite+pysqlite:///{database_path}",
        connect_args={"check_same_thread": False},
        execution_options={"schema_translate_map": {schema: None for schema in SCHEMAS}},
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    upload_dir = test_artifact_dir / "showcase_uploads"
    monkeypatch.setattr(
        fixed_demo,
        "get_settings",
        lambda: SimpleNamespace(
            dataset_role="showcase",
            upload_dir=str(upload_dir),
            showcase_upload_dir=str(upload_dir),
        ),
    )
    monkeypatch.setattr(
        dashboard_domain,
        "get_settings",
        lambda: SimpleNamespace(
            dashboard_map_mode="changchun",
            algorithm_showcase_enabled=True,
            map_location_delayed_minutes=10,
            map_location_stale_minutes=30,
        ),
    )
    monkeypatch.setattr(worker, "SessionLocal", maker)
    yield maker
    engine.dispose()


def _install(maker):
    with maker() as db:
        result = fixed_demo.materialize_fixed_demo_case(
            db,
            anchor_date=fixed_demo.today_shanghai(),
            password=SecretStr("Fixed-Showcase-Test-Password-2026"),
            require_process_role=False,
        )
        db.commit()
    for _ in range(20):
        if worker.process_pending_once(limit=500) == 0:
            break
    with maker() as db:
        installation = db.scalar(select(DemoCaseInstallation))
        assert installation.state == "READY"
    return result


def _assert_public_showcase_safe(value):
    if isinstance(value, dict):
        for key, item in value.items():
            assert key not in FORBIDDEN_SHOWCASE_KEYS
            assert not key.endswith("_id")
            assert not key.endswith("_versions")
            _assert_public_showcase_safe(item)
    elif isinstance(value, list):
        for item in value:
            _assert_public_showcase_safe(item)
    elif isinstance(value, str):
        assert not UUID_PATTERN.fullmatch(value)
        assert "SCENARIO_" not in value


def test_fixed_demo_catalog_is_deterministic_and_contains_controlled_assets():
    first = fixed_demo.validate_fixed_demo_catalog()
    second = fixed_demo.validate_fixed_demo_catalog()
    assert first == second
    assert first["counts"] == {
        "accounts": 5,
        "enterprises": 6,
        "stores": 12,
        "products": 8,
        "warehouses": 4,
        "suppliers": 4,
        "fleet": 6,
        "attachments": 2,
    }
    assert len(first["scenarios"]) == 3
    assert all(item["carpool_candidates"] >= 1 and item["warehouse_candidates"] >= 1 for item in first["scenarios"])
    assert fixed_demo.stable_id(first["case_key"], "store", "third-01") == fixed_demo.stable_id(
        first["case_key"], "store", "third-01"
    )


def test_fixed_demo_apply_is_complete_safe_and_idempotent(showcase_database):
    first = _install(showcase_database)
    second = _install(showcase_database)
    assert first["result"] == "applied"
    assert second["result"] == "unchanged"
    assert first["case_revision"] == second["case_revision"] == 1

    with showcase_database() as db:
        assert db.scalar(select(func.count()).select_from(StoreDailyReport)) == 372
        assert db.scalar(select(func.count()).select_from(TransportTask)) == 8
        assert db.scalar(select(func.count()).select_from(Receipt)) == 6
        assert db.scalar(select(func.count()).select_from(InventoryBalance)) == 72
        assert db.scalar(select(func.count()).select_from(InventoryMovement)) == 12
        assert db.scalar(select(func.count()).select_from(StockoutRequest)) == 8
        assert db.scalar(select(func.count()).select_from(Alert)) == 4
        assert db.scalar(select(func.count()).select_from(TelemetryIssue)) == 4
        assert db.scalar(select(func.count()).select_from(ProcurementAggregation)) == 6
        showcase_driver = db.scalar(select(User).where(User.username == "showcase-driver"))
        showcase_store = db.scalar(select(User).where(User.username == "showcase-store"))
        showcase_third = db.scalar(select(User).where(User.username == "showcase-third"))
        assert showcase_store.role == "store_manager"
        assert showcase_third.role == "third_space_manager"
        assert set(
            db.scalars(select(TransportTask.status).where(TransportTask.driver_id == showcase_driver.driver_id))
        ) >= {"PUBLISHED", "DRIVER_ACCEPTED", "PICKED_UP", "IN_TRANSIT"}
        plans = list(db.scalars(select(TransportPlan).where(TransportPlan.published_qr_token.is_not(None))))
        assert len(plans) == 6
        for plan in plans:
            task = db.get(TransportTask, plan.task_id)
            assert task is not None
            assert hashlib.sha256(plan.published_qr_token.encode("utf-8")).hexdigest() == task.qr_token_hash
            assert plan.qr_expires_at == task.qr_expires_at
        assert first["counts"]["forecast_projections"] == 18
        assert set(db.scalars(select(DemandForecastProjection.method))) == {
            "SEASONAL_EXPONENTIAL_SMOOTHING",
            "WEIGHTED_MOVING_AVERAGE",
            "PRODUCTION_PLAN_FALLBACK",
            "INSUFFICIENT_DATA",
        }

        for period, expected_days in (("7d", 7), ("30d", 30)):
            snapshot = read_dashboard_projection(db, period)
            assert snapshot is not None
            assert len(snapshot["charts"]["daily_operations"]) == expected_days
            assert snapshot["summary"]["preorder_count"] > 0
            assert len(snapshot["charts"]["third_space_ranking"]) == 6
            assert len(snapshot["map"]["points"]) == 8
            _assert_public_showcase_safe(snapshot["algorithm_showcase"])
        snapshot = read_dashboard_projection(db, "30d")
        panels = {panel["kind"]: panel for panel in snapshot["algorithm_showcase"]["panels"]}
        assert panels["PROCUREMENT"]["status"] == "PARTIAL"
        assert panels["PROCUREMENT"]["cases"][0]["allocations"]
        assert panels["FORECAST"]["cases"][0]["allocations"]


def test_fixed_demo_assistant_covers_all_read_only_intents(showcase_database):
    _install(showcase_database)
    questions = {
        "OVERVIEW": "园区概览",
        "CHANNEL": "第三空间营业额占比",
        "OPERATIONS": "最近经营营业额",
        "INVENTORY": "当前低库存和缺货",
        "ALERT": "当前温度湿度报警",
        "TRANSPORT": "运输任务和车辆路线",
        "CARPOOL": "拼车方案利用率",
        "WAREHOUSE": "拼仓可用库容",
        "PROCUREMENT": "集中采购供应商建议",
        "FORECAST": "下周预测和采购建议",
    }
    with showcase_database() as db:
        for expected_intent, question in questions.items():
            result = answer_data_question(db, question, period="30d", allow_external_classifier=False)
            assert result["intent"] == expected_intent
            assert result["answer"]
            assert result["chart"]["data"]


def test_showcase_driver_and_store_can_complete_one_b02_chain(showcase_database):
    _install(showcase_database)
    with showcase_database() as db:
        driver = db.scalar(select(User).where(User.username == "showcase-driver"))
        store = db.scalar(select(User).where(User.username == "showcase-third"))
        task = db.scalar(
            select(TransportTask).where(
                TransportTask.driver_id == driver.driver_id,
                TransportTask.status == "PUBLISHED",
            )
        )
        plan = db.scalar(select(TransportPlan).where(TransportPlan.task_id == task.id))
        assert plan.published_qr_token

        for index, action in enumerate(("ACCEPT", "PICKUP", "START_TRANSIT", "DELIVER"), start=1):
            task = db.scalar(
                select(TransportTask)
                .where(TransportTask.id == task.id)
                .execution_options(populate_existing=True)
            )
            payload = {"object_version": task.object_version}
            if action in {"PICKUP", "DELIVER"}:
                payload["qr_token"] = plan.published_qr_token
            transition_task(
                db,
                task=task,
                user=driver,
                action=action,
                expected_version=task.object_version,
                idempotency_key=f"showcase-chain-{index}",
                trace_id=str(uuid4()),
                payload=payload,
            )

        task = db.scalar(
            select(TransportTask)
            .where(TransportTask.id == task.id)
            .execution_options(populate_existing=True)
        )
        stop = db.scalar(select(TaskStop).where(TaskStop.task_id == task.id, TaskStop.store_id == store.store_id))
        lines = [
            {
                "product_id": item["product_id"],
                "expected_quantity": item["expected_quantity"],
                "received_quantity": item["expected_quantity"],
                "note": None,
            }
            for item in stop.delivery_lines
        ]
        result = sign_receipt(
            db,
            task=task,
            user=store,
            receipt_status="FULL",
            lines=lines,
            qr_token=plan.published_qr_token,
            expected_version=task.object_version,
            idempotency_key="showcase-chain-receipt",
            trace_id=str(uuid4()),
        )
        assert result["task_status"] == "COMPLETED"


def test_fixed_demo_reset_is_worker_owned_and_invalidates_revision(showcase_database, monkeypatch):
    _install(showcase_database)
    with showcase_database() as db:
        procurement_ids = set(db.scalars(select(ProcurementAggregation.id)))
        forecast_ids = set(db.scalars(select(DemandForecastProjection.id)))
        qr_tokens = set(
            db.scalars(
                select(TransportPlan.published_qr_token).where(
                    TransportPlan.published_qr_token.is_not(None)
                )
            )
        )
        installation = db.scalar(select(DemoCaseInstallation))
        installation.state = "REFRESHING"
        db.add(
            OutboxEvent(
                event_id=fixed_demo.stable_id(installation.case_key, "event", "reset-test"),
                topic="demo.case.reset_requested",
                object_type="demo_case_installation",
                object_id=installation.id,
                object_version=installation.object_version,
                payload={
                    "expected_case_revision": installation.case_revision,
                    "reset_reason": "TEST_RESET",
                },
            )
        )
        db.flush()
        superseded_event_id = fixed_demo.stable_id(
            installation.case_key, "event", "old-revision-pending"
        )
        db.add(
            OutboxEvent(
                event_id=superseded_event_id,
                topic="transport.plan.publish_requested",
                object_type="transport_plan",
                object_id=fixed_demo.stable_id(installation.case_key, "transport-plan", "1"),
                object_version=1,
                payload={"case_revision": installation.case_revision},
            )
        )
        db.commit()
    assert worker.process_pending_once(limit=1) == 1
    with showcase_database() as db:
        installation = db.scalar(select(DemoCaseInstallation))
        assert installation.state == "REFRESHING"
        assert installation.case_revision == 2
        assert read_dashboard_projection(db, "30d") is not None
        superseded = db.scalar(
            select(OutboxEvent).where(OutboxEvent.event_id == superseded_event_id)
        )
        assert superseded.status == "SUPERSEDED"
    for _ in range(20):
        if worker.process_pending_once(limit=500) == 0:
            break
    with showcase_database() as db:
        installation = db.scalar(select(DemoCaseInstallation))
        assert installation.state == "READY"
        assert installation.case_revision == 2
        event = db.scalar(select(OutboxEvent).where(OutboxEvent.topic == "demo.case.reset_requested"))
        assert event.status == "PUBLISHED"
        assert set(db.scalars(select(ProcurementAggregation.id))) == procurement_ids
        assert set(db.scalars(select(DemandForecastProjection.id))) == forecast_ids
        refreshed_qr_tokens = set(
            db.scalars(select(TransportPlan.published_qr_token).where(TransportPlan.published_qr_token.is_not(None)))
        )
        assert refreshed_qr_tokens.isdisjoint(qr_tokens)


def test_daily_scheduler_only_queues_a_single_worker_owned_reset(showcase_database, monkeypatch):
    _install(showcase_database)
    monkeypatch.setattr(showcase_scheduler, "SessionLocal", showcase_database)
    monkeypatch.setattr(
        showcase_scheduler,
        "get_settings",
        lambda: SimpleNamespace(showcase_case_key="changchun-fixed-showcase-v1"),
    )
    first = showcase_scheduler.run_showcase_reset()
    second = showcase_scheduler.run_showcase_reset()
    assert first["result"] == "queued"
    assert second["result"] == "pending"
    with showcase_database() as db:
        installation = db.scalar(select(DemoCaseInstallation))
        events = list(db.scalars(select(OutboxEvent).where(OutboxEvent.topic == "demo.case.reset_requested")))
        assert installation.state == "REFRESHING"
        assert installation.case_revision == 1
        assert len(events) == 1


def test_showcase_token_is_signed_and_old_case_revision_is_rejected(showcase_database):
    _install(showcase_database)
    with showcase_database() as db:
        user = db.scalar(select(User).where(User.username == "showcase-admin"))
        tokens = issue_tokens(db, user, dataset_mode="showcase", case_revision=1)
        db.commit()
        payload = decode_token(tokens.access_token, "access")
        assert payload["ds"] == "showcase"
        assert payload["case_rev"] == 1

        header, encoded_payload, signature = tokens.access_token.split(".")
        padding = "=" * (-len(encoded_payload) % 4)
        tampered_payload = json.loads(base64.urlsafe_b64decode(encoded_payload + padding))
        tampered_payload["ds"] = "live"
        encoded_tampered = base64.urlsafe_b64encode(
            json.dumps(tampered_payload, separators=(",", ":")).encode()
        ).decode().rstrip("=")
        with pytest.raises(BusinessError) as tampered_error:
            decode_token(f"{header}.{encoded_tampered}.{signature}", "access")
        assert tampered_error.value.code == "TOKEN_INVALID"

        installation = db.scalar(select(DemoCaseInstallation))
        installation.case_revision = 2
        request = SimpleNamespace(
            state=SimpleNamespace(dataset_mode="showcase"),
            client=SimpleNamespace(host="127.0.0.1"),
        )
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=tokens.access_token)
        with pytest.raises(BusinessError) as error:
            get_current_user(request=request, credentials=credentials, db=db)
        assert error.value.code == "SHOWCASE_CASE_UPDATED"


def test_fixed_demo_refuses_a_database_without_showcase_name(test_artifact_dir: Path):
    engine = create_engine(
        f"sqlite+pysqlite:///{test_artifact_dir / 'live.sqlite3'}",
        execution_options={"schema_translate_map": {schema: None for schema in SCHEMAS}},
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)
    with maker() as db, pytest.raises(RuntimeError, match="showcase"):
        fixed_demo.materialize_fixed_demo_case(
            db,
            password=SecretStr("Fixed-Showcase-Test-Password-2026"),
            require_process_role=False,
        )
    engine.dispose()
