from __future__ import annotations

import re
import sys
from pathlib import Path

from sqlalchemy import func, select

from app.domain.dashboard import build_dashboard_snapshot
from app.domain.dashboard_catalogs import (
    load_information_catalog,
    load_map_catalog,
    load_showcase_catalog,
    validate_all_catalogs,
)
from app.domain.dashboard_showcase import assert_public_showcase_safe, build_algorithm_showcase
from app.shared.config import get_settings
from app.shared.database import SessionLocal
from app.shared.models import AlgorithmRun, DashboardMapPoint, OutboxEvent
from scripts.generate_algorithm_showcase import generate
from scripts.import_dashboard_map_catalog import import_catalog
from scripts.validate_e02_catalogs import WEB_MAP_PATH, validate_algorithms, validate_geojson
from scripts.validate_e02_catalogs import main as validate_catalogs_main
from tests.conftest import login

UUID_PATTERN = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b"
)


def test_e02_catalogs_geojson_and_algorithms_are_deterministic():
    summary = validate_all_catalogs()
    assert summary["map"]["item_count"] == 8
    assert summary["information"]["item_count"] == 8
    assert summary["showcase"]["item_count"] == 3
    assert len(load_map_catalog().points) == 8
    assert {item.kind for item in load_information_catalog().items} == {"NEWS", "POLICY"}
    assert {item.public_key for item in load_showcase_catalog().scenarios} == {
        "city-morning-delivery",
        "jingyue-cold-chain",
        "suburban-third-space",
    }
    geo = validate_geojson()
    assert geo["feature_count"] >= 1
    assert len(geo["sha256"]) == 64
    algorithm = validate_algorithms()
    assert len(algorithm["scenarios"]) == 3


def test_catalog_validator_accepts_explicit_geojson_path(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["validate_e02_catalogs", "--geojson", str(WEB_MAP_PATH)])
    validate_catalogs_main()
    assert '"feature_count"' in capsys.readouterr().out


def test_map_catalog_import_is_idempotent_and_public_map_has_no_internal_identifiers():
    path = Path("app/content/e02-map-points.v1.json")
    dry_run = import_catalog(path, apply=False, expected_version="2026-08-13.1")
    assert dry_run["creates"] + dry_run["unchanged"] + dry_run["updates"] == 8
    applied = import_catalog(path, apply=True, expected_version="2026-08-13.1")
    assert applied["creates"] + applied["unchanged"] + applied["updates"] == 8
    repeated = import_catalog(path, apply=True, expected_version="2026-08-13.1")
    assert repeated["unchanged"] == 8
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(DashboardMapPoint)) == 8
        original_mode = get_settings().dashboard_map_mode
        get_settings().dashboard_map_mode = "changchun"
        try:
            public_map = build_dashboard_snapshot(db)["map"]
        finally:
            get_settings().dashboard_map_mode = original_mode
    assert public_map["schema_version"] == "2.0"
    assert len(public_map["points"]) == 8
    assert sum(item["point_type"] == "THIRD_SPACE" for item in public_map["points"]) == 3
    demo = next(item for item in public_map["points"] if item["is_demo"])
    assert demo["display_name"] == "大食味（演示门店）"
    assert "不提供导航" in demo["coordinate_note"]
    rendered = str(
        {
            "points": public_map["points"],
            "active_routes": public_map["active_routes"],
            "excluded_route_summary": public_map["excluded_route_summary"],
        }
    )
    assert "object_version" not in rendered
    assert not UUID_PATTERN.search(rendered)


def test_information_api_etag_aliases_and_feature_isolation(b01_client):
    settings = get_settings()
    original = settings.public_information_enabled
    settings.public_information_enabled = False
    disabled = b01_client.get("/api/v1/public/dashboard/information")
    assert disabled.status_code == 503
    assert disabled.json()["code"] == "PUBLIC_INFORMATION_UNAVAILABLE"
    assert b01_client.get("/api/v1/public/dashboard/snapshot").status_code == 200
    settings.public_information_enabled = True
    try:
        response = b01_client.get("/api/v1/public/dashboard/information?kind=all&limit=8")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"] == 8
        assert len(body["items"]) == 8
        assert body["data_cutoff"] == max(item["published_at"] for item in body["items"])
        assert response.headers["cache-control"] == "public, max-age=60, stale-while-revalidate=300"
        etag = response.headers["etag"]
        cached = b01_client.get(
            "/api/v1/public/dashboard/information?kind=all&limit=8",
            headers={"If-None-Match": etag},
        )
        assert cached.status_code == 304
        assert cached.content == b""
        news = b01_client.get("/api/v1/public/dashboard/news?limit=6").json()
        policies = b01_client.get("/api/v1/public/dashboard/policies?limit=6").json()
        assert all(item["kind"] == "NEWS" for item in news["data"])
        assert all(item["kind"] == "POLICY" for item in policies["data"])
    finally:
        settings.public_information_enabled = original


def test_preview_presentation_is_business_facing_and_keeps_private_response_compatibility(b01_client):
    auth = login(b01_client, "/api/v1/web", "admin")
    response = b01_client.post(
        "/api/v1/web/algorithms/carpool/preview",
        headers={"Authorization": f"Bearer {auth['access_token']}"},
        json={"scenario_code": "SCENARIO_1"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["match_run_id"]
    assert body["candidates"]
    presentation = body["presentation"]
    assert presentation["title"] == "拼车联配测算结果"
    assert presentation["allocations"]
    assert not UUID_PATTERN.search(str(presentation))
    assert "vehicle_id" not in str(presentation)
    assert "order_ids" not in str(presentation)


def test_showcase_generation_is_idempotent_and_public_contract_is_safe():
    batch = "test-e02-presentation"
    checked = generate(apply=False, batch_key=batch)
    assert len(checked["runs"]) == 6
    assert all(item["candidate_count"] >= 1 for item in checked["runs"])
    first = generate(apply=True, batch_key=batch)
    second = generate(apply=True, batch_key=batch)
    assert not any(item["existing"] for item in first["runs"])
    assert all(item["existing"] for item in second["runs"])
    with SessionLocal() as db:
        showcase = build_algorithm_showcase(db)
        assert [item["kind"] for item in showcase["panels"]] == [
            "CARPOOL",
            "WAREHOUSE",
            "PROCUREMENT",
            "FORECAST",
        ]
        assert len(showcase["panels"][0]["cases"]) == 3
        assert len(showcase["panels"][1]["cases"]) == 3
        assert_public_showcase_safe(showcase)
        runs = list(db.scalars(select(AlgorithmRun).where(AlgorithmRun.showcase_batch_key == batch)))
        assert len(runs) == 6
        assert db.scalar(
            select(func.count()).select_from(OutboxEvent).where(OutboxEvent.topic == "algorithm.run.completed")
        ) >= 6


def test_currency_metadata_is_additive_and_operations_summary_keeps_legacy_unit(b01_client, admin_headers):
    snapshot = b01_client.get("/api/v1/web/dashboard/snapshot", headers=admin_headers)
    assert snapshot.status_code == 200
    body = snapshot.json()
    assert body["currency"] == "CNY"
    assert body["sales_amount_unit"] == "yuan"
    assert body["order_count_unit"] == "单"
    summary = b01_client.get("/api/v1/web/operations/summary", headers=admin_headers)
    assert summary.status_code == 200
    operations = summary.json()
    assert operations["currency"] == "CNY"
    assert operations["sales_amount_unit"] == "yuan"
    assert operations["order_count_unit"] == "单"
    assert operations["daily_reports"]["unit"] == "人民币/单"


def test_public_information_and_map_assets_are_tracked_production_content():
    assert WEB_MAP_PATH.is_file()
    assert "frontend-mocks" not in str(WEB_MAP_PATH).lower()
