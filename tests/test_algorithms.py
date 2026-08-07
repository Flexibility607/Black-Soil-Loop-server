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
            assert all(item["route_label"] == "经纬度估算路线" for item in result["candidates"])

        warehouse_result = run_warehouse_pool(
            list(db.scalars(select(TransportOrder))),
            list(db.scalars(select(Warehouse))),
        )
        assert warehouse_result["candidates"]
        assert warehouse_result["rules"]["distance_max_km"] == 30.0


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
        assert forecast["mae"] >= 0
        assert forecast["smape"] >= 0
