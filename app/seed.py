from __future__ import annotations

import argparse
import hashlib
import math
import random
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import inspect, select

from app.domain.dashboard import refresh_dashboard_projection
from app.shared.config import get_settings
from app.shared.database import Base, SessionLocal, create_schemas, engine, init_database
from app.shared.models import (
    Alert,
    DemandHistory,
    Driver,
    Enterprise,
    InventoryBalance,
    Product,
    ProductionPlan,
    Store,
    StoreDailyReport,
    Supplier,
    SupplierPriceTier,
    TaskStop,
    TelemetryPoint,
    TransportOrder,
    TransportTask,
    User,
    Vehicle,
    Warehouse,
    utcnow,
)
from app.shared.security import hash_password

SHANGHAI = ZoneInfo("Asia/Shanghai")


def sid(kind: str, code: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"black-soil-loop:{kind}:{code}"))


def _month_start(year: int, month: int) -> date:
    return date(year, month, 1)


def seed(reset: bool = False) -> dict[str, object]:
    if engine.dialect.name == "postgresql":
        existing_schemas = set(inspect(engine).get_schema_names())
        required_schemas = {table.schema for table in Base.metadata.sorted_tables if table.schema}
        missing_schemas = required_schemas - existing_schemas
        if missing_schemas:
            missing = ", ".join(sorted(schema for schema in missing_schemas if schema))
            raise RuntimeError(f"数据库迁移尚未完成，缺少 schema：{missing}")
    else:
        create_schemas(engine)
        if reset:
            Base.metadata.drop_all(engine)
        init_database(engine)
    if reset and engine.dialect.name == "postgresql":
        with engine.begin() as connection:
            for table in reversed(Base.metadata.sorted_tables):
                connection.execute(table.delete())
    today = datetime.now(SHANGHAI).date()
    rng = random.Random(20260806)
    settings = get_settings()

    with SessionLocal() as db:
        if db.scalar(select(Enterprise.id).limit(1)):
            return {"status": "skipped", "reason": "数据库已有演示数据；如需重建请使用 --reset"}

        enterprises = []
        for index, name in enumerate(["吉品食品", "北沃农业", "松江餐饮", "稻香食品", "黑土优选", "寒地供应链"], 1):
            item = Enterprise(id=sid("enterprise", str(index)), code=f"ENT{index:02d}", name=name)
            enterprises.append(item)
            db.add(item)

        product_specs = [
            ("P001", "速冻玉米", "玉米制品", "kg", "FROZEN"),
            ("P002", "鲜食玉米", "玉米制品", "kg", "CHILLED"),
            ("P003", "冷鲜猪肉", "肉制品", "kg", "CHILLED"),
            ("P004", "速冻水饺", "面点", "箱", "FROZEN"),
            ("P005", "东北大米", "粮食", "袋", "AMBIENT"),
            ("P006", "山野菜", "蔬菜", "kg", "CHILLED"),
            ("P007", "豆制品", "豆制品", "箱", "CHILLED"),
            ("P008", "玉米饮品", "饮品", "箱", "AMBIENT"),
        ]
        products = []
        for code, name, category, unit, temperature_zone in product_specs:
            product = Product(
                id=sid("product", code),
                code=code,
                name=name,
                category=category,
                unit=unit,
                temperature_zone=temperature_zone,
            )
            products.append(product)
            db.add(product)

        store_specs = [
            ("S001", "吉品中央门店", "TRADITIONAL", 45.7560, 126.6420),
            ("S002", "松北社区店", "TRADITIONAL", 45.8030, 126.5360),
            ("S003", "道里生鲜店", "TRADITIONAL", 45.7440, 126.6150),
            ("S004", "香坊直营网点", "TRADITIONAL", 45.7200, 126.6900),
            ("T001", "冰雪大世界第三空间", "THIRD_SPACE", 45.8120, 126.5680),
            ("T002", "中央大街第三空间", "THIRD_SPACE", 45.7690, 126.6140),
            ("T003", "哈西高铁第三空间", "THIRD_SPACE", 45.7040, 126.5850),
            ("T004", "太阳岛第三空间", "THIRD_SPACE", 45.7940, 126.5900),
            ("T005", "伏尔加庄园第三空间", "THIRD_SPACE", 45.6260, 126.8560),
            ("T006", "机场第三空间", "THIRD_SPACE", 45.6230, 126.2500),
        ]
        stores = []
        for index, (code, name, channel, latitude, longitude) in enumerate(store_specs):
            store = Store(
                id=sid("store", code),
                enterprise_id=enterprises[index % len(enterprises)].id,
                code=code,
                name=name,
                channel=channel,
                latitude=latitude,
                longitude=longitude,
            )
            stores.append(store)
            db.add(store)

        zones = ["AMBIENT", "CHILLED", "FROZEN", "CHILLED", "AMBIENT", "FROZEN", "CHILLED", "AMBIENT"]
        drivers = []
        vehicles = []
        for index, zone in enumerate(zones, 1):
            driver = Driver(id=sid("driver", str(index)), code=f"D{index:03d}", name=f"演示司机{index}")
            vehicle = Vehicle(
                id=sid("vehicle", str(index)),
                driver_id=driver.id,
                plate_no=f"黑A·{3600 + index}",
                temperature_zone=zone,
                max_weight_kg=6000 + index * 500,
                max_volume_m3=24 + index * 2,
            )
            drivers.append(driver)
            vehicles.append(vehicle)
            db.add_all([driver, vehicle])

        warehouse_specs = [
            ("W001", "平房常温共享仓", "AMBIENT", 45.603, 126.639, 850.0, 260.0),
            ("W002", "哈南冷藏共享仓", "CHILLED", 45.591, 126.620, 620.0, 190.0),
            ("W003", "经开冷冻共享仓", "FROZEN", 45.682, 126.671, 480.0, 155.0),
            ("W004", "松北冷藏周转仓", "CHILLED", 45.820, 126.510, 360.0, 80.0),
        ]
        warehouses = []
        for index, (code, name, zone, latitude, longitude, capacity, used) in enumerate(warehouse_specs):
            warehouse = Warehouse(
                id=sid("warehouse", code),
                enterprise_id=enterprises[index % len(enterprises)].id,
                code=code,
                name=name,
                temperature_zone=zone,
                latitude=latitude,
                longitude=longitude,
                capacity_m3=capacity,
                used_m3=used,
            )
            warehouses.append(warehouse)
            db.add(warehouse)

        suppliers = []
        for index, name in enumerate(["北大荒供应", "龙江丰农", "松嫩优选", "寒地粮仓", "兴凯湖供应", "黑土源头"], 1):
            supplier = Supplier(
                id=sid("supplier", str(index)),
                code=f"SUP{index:02d}",
                name=name,
                delivery_score=82 + index * 2,
                quality_score=84 + ((index * 3) % 14),
            )
            suppliers.append(supplier)
            db.add(supplier)
            for product_index, product in enumerate(products, 1):
                base_price = (
                    Decimal("5.20") + Decimal(product_index) * Decimal("0.73") + Decimal(index) * Decimal("0.11")
                )
                for tier_index, (minimum, maximum, discount) in enumerate(
                    [(50.0, 499.99, Decimal("1.00")), (500.0, None, Decimal("0.91"))], 1
                ):
                    db.add(
                        SupplierPriceTier(
                            id=sid("tier", f"{index}-{product_index}-{tier_index}"),
                            supplier_id=supplier.id,
                            product_id=product.id,
                            min_quantity=minimum,
                            max_quantity=maximum,
                            supply_capacity=5000 + index * 800,
                            unit_price=(base_price * discount).quantize(Decimal("0.0001")),
                            valid_from=date(2023, 1, 1),
                            valid_to=date(2030, 12, 31),
                        )
                    )

        db.flush()
        departure_day = today + timedelta(days=1)
        local_midnight = datetime.combine(departure_day, time(0, 0), tzinfo=SHANGHAI)
        orders = []
        scenario_origins = [(45.603, 126.639), (45.681, 126.671), (45.806, 126.520)]
        scenario_stores = [stores[:5], stores[2:8], stores[4:10]]
        for scenario_index in range(3):
            scenario_code = f"SCENARIO_{scenario_index + 1}"
            origin_lat, origin_lon = scenario_origins[scenario_index]
            chosen_stores = scenario_stores[scenario_index]
            for order_index in range(15):
                product = products[order_index % 5]
                store = chosen_stores[order_index % len(chosen_stores)]
                enterprise = enterprises[order_index % len(enterprises)]
                jitter = (order_index % 3) * 0.008
                departure_at = (
                    local_midnight + timedelta(hours=7 + scenario_index, minutes=(order_index % 4) * 15)
                ).astimezone(UTC)
                order = TransportOrder(
                    id=sid("order", f"{scenario_index + 1}-{order_index + 1}"),
                    order_no=f"Y{today.year % 100:02d}{scenario_index + 1}{order_index + 1:03d}",
                    scenario_code=scenario_code,
                    enterprise_id=enterprise.id,
                    product_id=product.id,
                    store_id=store.id,
                    origin_latitude=origin_lat + jitter,
                    origin_longitude=origin_lon + jitter / 2,
                    destination_latitude=store.latitude,
                    destination_longitude=store.longitude,
                    departure_at=departure_at,
                    warehouse_inbound_start=departure_at - timedelta(hours=6),
                    warehouse_inbound_end=departure_at - timedelta(hours=4),
                    warehouse_outbound_start=departure_at - timedelta(hours=2),
                    warehouse_outbound_end=departure_at,
                    quantity=24 + (order_index % 5) * 6,
                    unit=product.unit,
                    weight_kg=180 + (order_index % 5) * 55,
                    volume_m3=0.9 + (order_index % 4) * 0.32,
                    temperature_zone=product.temperature_zone,
                )
                orders.append(order)
                db.add(order)

        for enterprise_index, enterprise in enumerate(enterprises):
            for product_index, product in enumerate(products):
                for year in (2023, 2024, 2025):
                    for month in range(1, 13):
                        season = 1 + 0.22 * math.sin((month - 1) / 12 * 2 * math.pi)
                        trend = 1 + (year - 2023) * 0.06
                        quantity = (90 + enterprise_index * 11 + product_index * 8) * season * trend
                        quantity += rng.uniform(-4, 4)
                        db.add(
                            DemandHistory(
                                id=sid("demand", f"{enterprise_index}-{product_index}-{year}-{month}"),
                                enterprise_id=enterprise.id,
                                product_id=product.id,
                                period_start=_month_start(year, month),
                                quantity=round(quantity, 2),
                            )
                        )
                db.add(
                    ProductionPlan(
                        id=sid("plan", f"{enterprise_index}-{product_index}-2026"),
                        enterprise_id=enterprise.id,
                        product_id=product.id,
                        plan_date=today + timedelta(days=7),
                        planned_quantity=125 + enterprise_index * 12 + product_index * 7,
                    )
                )

        for store_index, store in enumerate(stores):
            for product_index, product in enumerate(products):
                db.add(
                    InventoryBalance(
                        id=sid("inventory", f"{store_index}-{product_index}"),
                        store_id=store.id,
                        product_id=product.id,
                        quantity=18 + store_index * 3 + product_index * 4,
                        low_stock_threshold=24,
                    )
                )
            for offset in range(14):
                report_date = today - timedelta(days=offset)
                channel_factor = 1.38 if store.channel == "THIRD_SPACE" else 1.0
                sales = Decimal(str(round((3100 + store_index * 245 + (13 - offset) * 38) * channel_factor, 2)))
                db.add(
                    StoreDailyReport(
                        id=sid("daily", f"{store.code}-{report_date.isoformat()}"),
                        store_id=store.id,
                        report_date=report_date,
                        sales_amount=sales,
                        order_count=int((42 + store_index * 3 + offset % 5) * channel_factor),
                        authorized_for_dashboard=True,
                        summary={"source": "reproducible-seed", "channel": store.channel},
                        idempotency_key=f"seed-daily-{store.code}-{report_date.isoformat()}",
                    )
                )

        demo_qr = "BSL:DEMO-TASK-001:DEMO-QR-2026"
        demo_task = TransportTask(
            id=sid("task", "demo-001"),
            task_no="DEMO-TASK-001",
            source_order_ids=[order.id for order in orders[:3]],
            vehicle_id=vehicles[1].id,
            driver_id=drivers[1].id,
            store_id=stores[4].id,
            status="IN_TRANSIT",
            temperature_zone="CHILLED",
            total_weight_kg=sum(order.weight_kg for order in orders[:3]),
            total_volume_m3=sum(order.volume_m3 for order in orders[:3]),
            origin_latitude=orders[0].origin_latitude,
            origin_longitude=orders[0].origin_longitude,
            destination_latitude=stores[4].latitude,
            destination_longitude=stores[4].longitude,
            planned_departure_at=local_midnight.astimezone(UTC),
            qr_token_hash=hashlib.sha256(demo_qr.encode("utf-8")).hexdigest(),
            qr_expires_at=utcnow() + timedelta(days=30),
        )
        db.add(demo_task)
        db.flush()
        for sequence, store in enumerate(stores[4:7], 1):
            db.add(
                TaskStop(
                    id=sid("stop", f"demo-{sequence}"),
                    task_id=demo_task.id,
                    store_id=store.id,
                    sequence_no=sequence,
                    latitude=store.latitude,
                    longitude=store.longitude,
                    delivery_lines=[
                        {
                            "order_ids": [orders[sequence - 1].id],
                            "product_id": orders[sequence - 1].product_id,
                            "expected_quantity": orders[sequence - 1].quantity,
                            "unit": orders[sequence - 1].unit,
                        }
                    ],
                )
            )
        anomaly_time = utcnow() - timedelta(minutes=3)
        db.add(
            TelemetryPoint(
                id=sid("telemetry", "demo-anomaly"),
                task_id=demo_task.id,
                vehicle_id=demo_task.vehicle_id,
                sampled_at=anomaly_time,
                temperature_c=10.6,
                humidity_pct=91.0,
                latitude=45.741,
                longitude=126.612,
                anomaly_code="TEMPERATURE+HUMIDITY",
            )
        )
        db.add_all(
            [
                Alert(
                    id=sid("alert", "temperature"),
                    task_id=demo_task.id,
                    alert_type="TEMPERATURE",
                    message="冷藏运输温度超出 0–8℃ 阈值",
                ),
                Alert(
                    id=sid("alert", "humidity"),
                    task_id=demo_task.id,
                    alert_type="HUMIDITY",
                    message="冷藏运输湿度超出 30%–85% 阈值",
                ),
            ]
        )

        password_hash = hash_password(settings.demo_seed_password)
        users = [
            User(
                id=sid("user", "admin"),
                username="admin",
                display_name="园区管理员",
                password_hash=password_hash,
                role="park_admin",
            ),
            User(
                id=sid("user", "enterprise"),
                username="enterprise_demo",
                display_name="企业管理员",
                password_hash=password_hash,
                role="enterprise_admin",
                enterprise_id=enterprises[0].id,
            ),
            User(
                id=sid("user", "driver"),
                username="driver_demo",
                display_name="演示司机2",
                password_hash=password_hash,
                role="driver",
                driver_id=drivers[1].id,
            ),
            User(
                id=sid("user", "store"),
                username="store_demo",
                display_name="传统门店店长",
                password_hash=password_hash,
                role="store_manager",
                store_id=stores[0].id,
            ),
            User(
                id=sid("user", "third-space"),
                username="third_space_demo",
                display_name="第三空间店长",
                password_hash=password_hash,
                role="third_space_manager",
                store_id=stores[4].id,
            ),
        ]
        for store in stores:
            users.append(
                User(
                    id=sid("user", f"manager-{store.code}"),
                    username=f"manager_{store.code.lower()}",
                    display_name=f"{store.name}负责人",
                    password_hash=password_hash,
                    role="third_space_manager" if store.channel == "THIRD_SPACE" else "store_manager",
                    store_id=store.id,
                )
            )
        db.add_all(users)
        db.commit()
        refresh_dashboard_projection(db)

    return {
        "status": "created",
        "anchor_date": today.isoformat(),
        "counts": {"enterprises": 6, "products": 8, "vehicles": 8, "warehouses": 4, "stores": 10, "orders": 45},
        "demo_users": [
            "admin",
            "enterprise_demo",
            "driver_demo",
            "store_demo",
            "third_space_demo",
            "manager_s001 … manager_t006",
        ],
        "demo_password_configured": True,
        "demo_qr": demo_qr,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="建立黑土循环可重复演示数据")
    parser.add_argument("--reset", action="store_true", help="删除当前库中的所有业务表后重建")
    args = parser.parse_args()
    print(seed(reset=args.reset))


if __name__ == "__main__":
    main()
