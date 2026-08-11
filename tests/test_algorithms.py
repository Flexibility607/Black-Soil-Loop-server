from __future__ import annotations

from datetime import date

from sqlalchemy import select

from app.domain.algorithms import forecast_demand, run_carpool, run_procurement, run_warehouse_pool
from app.shared.database import SessionLocal
from app.shared.models import Enterprise, Product, TransportOrder, Vehicle, Warehouse


def test_seeded_complex_scenarios_and_algorithms(seeded_database):
    with SessionLocal() as db:
        for scenario in ("SCENARIO_1", "SCENARIO_2", "SCENARIO_3"):
            orders = list(db.scalars(select(TransportOrder).where(TransportOrder.scenario_code == scenario)))
            assert len(orders) == 15
            assert len({item.enterprise_id for item in orders}) >= 3
            assert len({item.product_id for item in orders}) >= 5
            result = run_carpool(orders, list(db.scalars(select(Vehicle))))
            assert result["candidates"]
            repeated = run_carpool(list(reversed(orders)), list(reversed(list(db.scalars(select(Vehicle))))))
            assert repeated == result
            assert all(item["route_label"] == "经纬度估算路线" for item in result["candidates"])
            vehicles = {item.id: item for item in db.scalars(select(Vehicle))}
            for candidate in result["candidates"]:
                assert candidate["departure_span_minutes"] <= 60
                assert candidate["total_weight_kg"] <= vehicles[candidate["vehicle_id"]].max_weight_kg
                assert candidate["total_volume_m3"] <= vehicles[candidate["vehicle_id"]].max_volume_m3
                displayed_average = (
                    candidate["weight_utilization_pct"] + candidate["volume_utilization_pct"]
                ) / 2
                assert abs(candidate["capacity_utilization_pct"] - displayed_average) <= 0.1
                assert all(item["origin_distance_km"] <= 20 for item in candidate["pairwise_checks"])
                assert all(item["destination_distance_km"] <= 25 for item in candidate["pairwise_checks"])
                assert "mileage_benefit_estimated_km" in candidate
                assert "detour_estimated_km" in candidate
                assert candidate["route_points"]
            assert result["candidates"] == sorted(
                result["candidates"],
                key=lambda item: (
                    -item["enterprise_count"],
                    -len(item["order_ids"]),
                    -item["capacity_utilization_pct"],
                    -item["mileage_benefit_estimated_km"],
                    item["detour_estimated_km"],
                    item["vehicle_id"],
                    tuple(item["order_ids"]),
                ),
            )

        warehouse_result = run_warehouse_pool(
            list(db.scalars(select(TransportOrder))),
            list(db.scalars(select(Warehouse))),
        )
        assert warehouse_result["candidates"]
        assert warehouse_result["rules"]["distance_max_km"] == 30.0
        assert all(
            candidate["candidate_warehouses"] == sorted(
                candidate["candidate_warehouses"],
                key=lambda item: (
                    not item["eligible"],
                    item["estimated_distance_km"],
                    -item["remaining_volume_m3"],
                    item["warehouse_id"],
                ),
            )
            for candidate in warehouse_result["candidates"]
        )
        assert all("order_versions" in candidate for candidate in warehouse_result["candidates"])


def test_procurement_and_forecast_explain_methods(seeded_database):
    with SessionLocal() as db:
        product = db.scalar(select(Product).limit(1))
        enterprise = db.scalar(select(Enterprise).limit(1))
        procurement = run_procurement(db, product.id, 760, date.today())
        assert procurement["recommendation"]
        assert procurement["weights"] == {"price": 0.6, "delivery": 0.2, "quality": 0.2}
        forecast = forecast_demand(db, enterprise.id, product.id, date.today())
        assert forecast["method"] == "SEASONAL_EXPONENTIAL_SMOOTHING"
        assert forecast["lower_bound"] <= forecast["forecast_quantity"] <= forecast["upper_bound"]
        assert forecast["forecast_start"] <= forecast["forecast_end"]
        assert forecast["mae"] >= 0
        assert forecast["smape"] >= 0
