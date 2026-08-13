from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from app.shared.models import utcnow

CONTENT_ROOT = Path(__file__).resolve().parents[1] / "content"
MAP_CATALOG_PATH = CONTENT_ROOT / "e02-map-points.v1.json"
INFORMATION_CATALOG_PATH = CONTENT_ROOT / "e02-public-information.v1.json"
SHOWCASE_CATALOG_PATH = CONTENT_ROOT / "e02-showcase-scenarios.v1.json"

CATALOG_VERSION_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}\.\d+$")
SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{2,79}$")
CONTROL_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
HTML_PATTERN = re.compile(r"<[^>]+>|\[[^\]]+\]\([^\)]+\)")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class MapBounds(StrictModel):
    min_longitude: float
    min_latitude: float
    max_longitude: float
    max_latitude: float

    @model_validator(mode="after")
    def validate_order(self):
        values = (self.min_longitude, self.min_latitude, self.max_longitude, self.max_latitude)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("地图范围必须是有限数值")
        if not (-180 <= self.min_longitude < self.max_longitude <= 180):
            raise ValueError("地图经度范围无效")
        if not (-90 <= self.min_latitude < self.max_latitude <= 90):
            raise ValueError("地图纬度范围无效")
        return self

    def contains(self, longitude: float, latitude: float) -> bool:
        return (
            math.isfinite(longitude)
            and math.isfinite(latitude)
            and self.min_longitude <= longitude <= self.max_longitude
            and self.min_latitude <= latitude <= self.max_latitude
        )

    def as_list(self) -> list[float]:
        return [self.min_longitude, self.min_latitude, self.max_longitude, self.max_latitude]


class MapPointItem(StrictModel):
    point_code: str = Field(pattern=r"^[A-Z0-9][A-Z0-9-]{2,63}$")
    store_code: str | None = None
    display_name: str = Field(min_length=2, max_length=120)
    point_type: Literal["THIRD_SPACE", "TRADITIONAL_STORE"]
    brand_name: str = Field(min_length=2, max_length=80)
    address: str = Field(min_length=4, max_length=240)
    longitude: float
    latitude: float
    coordinate_crs: Literal["EPSG:4326"] = "EPSG:4326"
    coordinate_accuracy: Literal["ENTRANCE", "ROOFTOP", "ADDRESS", "DEMO_APPROXIMATE"]
    source_longitude: float | None = None
    source_latitude: float | None = None
    source_crs: Literal["WGS84", "GCJ02", "BD09"]
    conversion_method: str | None = Field(default=None, max_length=120)
    featured: bool = True
    display_order: int = Field(ge=0, le=9999)
    source_type: Literal["VERIFIED_MAP", "OFFICIAL_SITE", "DEMO_SIMULATION"]
    source_name: str = Field(min_length=2, max_length=120)
    source_url: HttpUrl | None = None
    source_accessed_at: datetime
    verified_at: datetime
    enabled: bool = True

    @model_validator(mode="after")
    def validate_source(self):
        if not (math.isfinite(self.longitude) and math.isfinite(self.latitude)):
            raise ValueError("地图点坐标必须是有限数值")
        if not (-180 <= self.longitude <= 180 and -90 <= self.latitude <= 90):
            raise ValueError("地图点坐标超出合法经纬度范围")
        if self.source_type == "DEMO_SIMULATION" and self.coordinate_accuracy != "DEMO_APPROXIMATE":
            raise ValueError("演示点必须明确使用 DEMO_APPROXIMATE 精度")
        if self.source_type != "DEMO_SIMULATION" and self.source_url is None:
            raise ValueError("核验点必须提供来源 URL")
        if self.source_url is not None and urlparse(str(self.source_url)).scheme != "https":
            raise ValueError("地图点来源 URL 必须使用 HTTPS")
        return self


class MapCatalog(StrictModel):
    catalog_version: str
    service_scope: Literal["长春市及市郊"]
    crs: Literal["EPSG:4326"]
    coordinate_order: Literal["longitude,latitude"]
    bounds: MapBounds
    geojson_asset: Literal["/assets/maps/changchun-service-area.geojson"]
    geojson_source_url: HttpUrl
    geojson_source_name: str = Field(min_length=2, max_length=120)
    geojson_source_accessed_at: datetime
    geojson_license: str = Field(min_length=2, max_length=120)
    points: list[MapPointItem] = Field(min_length=8, max_length=100)

    @field_validator("catalog_version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        if not CATALOG_VERSION_PATTERN.fullmatch(value):
            raise ValueError("目录版本必须匹配 YYYY-MM-DD.N")
        return value

    @model_validator(mode="after")
    def validate_points(self):
        codes = [item.point_code for item in self.points]
        if len(codes) != len(set(codes)):
            raise ValueError("地图点编码必须唯一")
        enabled = [item for item in self.points if item.enabled and item.featured]
        third_spaces = [item for item in enabled if item.point_type == "THIRD_SPACE"]
        traditional = [item for item in enabled if item.point_type == "TRADITIONAL_STORE"]
        if len(third_spaces) != 3 or len(traditional) < 5:
            raise ValueError("精选地图必须包含 3 个第三空间和至少 5 个普通销售点")
        for item in enabled:
            if not self.bounds.contains(item.longitude, item.latitude):
                raise ValueError(f"地图点 {item.point_code} 超出长春服务范围")
        return self


class InformationItem(StrictModel):
    slug: str
    kind: Literal["NEWS", "POLICY"]
    category: str = Field(min_length=2, max_length=20)
    title: str = Field(min_length=2, max_length=80)
    summary: str = Field(min_length=10, max_length=240)
    published_at: datetime
    source_type: Literal["INTERNAL_RELEASE", "OFFICIAL_POLICY"]
    source_name: str = Field(min_length=2, max_length=80)
    source_url: HttpUrl | None = None
    source_verified_at: datetime
    tone: Literal["INFO", "SUCCESS", "WARNING", "ALERT"]
    display_order: int = Field(ge=0, le=9999)
    enabled: bool = True

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, value: str) -> str:
        if not SLUG_PATTERN.fullmatch(value):
            raise ValueError("资讯 slug 格式无效")
        return value

    @field_validator("category", "title", "summary", "source_name")
    @classmethod
    def validate_plain_text(cls, value: str) -> str:
        if CONTROL_PATTERN.search(value) or HTML_PATTERN.search(value):
            raise ValueError("资讯内容不允许 HTML、Markdown 链接或控制字符")
        return value

    @model_validator(mode="after")
    def validate_source(self):
        if self.published_at > utcnow() + timedelta(minutes=5):
            raise ValueError("资讯发布时间不能晚于当前时间五分钟以上")
        if self.published_at.tzinfo is None or self.source_verified_at.tzinfo is None:
            raise ValueError("资讯时间必须带时区")
        if self.source_url is not None and urlparse(str(self.source_url)).scheme != "https":
            raise ValueError("资讯来源 URL 必须使用 HTTPS")
        if self.source_type == "OFFICIAL_POLICY":
            if self.kind != "POLICY" or self.source_url is None:
                raise ValueError("正式政策必须提供政府来源 URL")
            host = (urlparse(str(self.source_url)).hostname or "").lower()
            if not (host == "gov.cn" or host.endswith(".gov.cn")):
                raise ValueError("政策来源域名必须属于 gov.cn")
        elif self.source_url is not None:
            host = (urlparse(str(self.source_url)).hostname or "").lower()
            if not (host == "flexibility607.cn" or host.endswith(".flexibility607.cn")):
                raise ValueError("内部资讯链接只能使用 flexibility607.cn")
        return self


class InformationCatalog(StrictModel):
    catalog_version: str
    service_scope: Literal["长春市及市郊"]
    items: list[InformationItem] = Field(min_length=1, max_length=100)

    @field_validator("catalog_version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        if not CATALOG_VERSION_PATTERN.fullmatch(value):
            raise ValueError("目录版本必须匹配 YYYY-MM-DD.N")
        return value

    @model_validator(mode="after")
    def validate_items(self):
        slugs = [item.slug for item in self.items]
        if len(slugs) != len(set(slugs)):
            raise ValueError("资讯 slug 必须唯一")
        return self


class ScenarioOrder(StrictModel):
    order_key: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,47}$")
    enterprise_name: str = Field(min_length=2, max_length=80)
    product_name: str = Field(min_length=2, max_length=80)
    store_name: str = Field(min_length=2, max_length=120)
    origin_latitude: float
    origin_longitude: float
    destination_latitude: float
    destination_longitude: float
    departure_at: datetime
    warehouse_inbound_start: datetime
    warehouse_inbound_end: datetime
    warehouse_outbound_start: datetime
    warehouse_outbound_end: datetime
    quantity: float = Field(gt=0)
    unit: str = Field(min_length=1, max_length=20)
    weight_kg: float = Field(gt=0)
    volume_m3: float = Field(gt=0)
    temperature_zone: Literal["AMBIENT", "CHILLED", "FROZEN"]


class ScenarioVehicle(StrictModel):
    vehicle_key: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,47}$")
    temperature_zone: Literal["AMBIENT", "CHILLED", "FROZEN"]
    max_weight_kg: float = Field(gt=0)
    max_volume_m3: float = Field(gt=0)


class ScenarioWarehouse(StrictModel):
    warehouse_key: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,47}$")
    name: str = Field(min_length=2, max_length=120)
    temperature_zone: Literal["AMBIENT", "CHILLED", "FROZEN"]
    latitude: float
    longitude: float
    capacity_m3: float = Field(gt=0)
    used_m3: float = Field(ge=0)
    reserved_m3: float = Field(ge=0)


class ShowcaseScenario(StrictModel):
    scenario_code: Literal["SCENARIO_1", "SCENARIO_2", "SCENARIO_3"]
    public_key: Literal["city-morning-delivery", "jingyue-cold-chain", "suburban-third-space"]
    title: str = Field(min_length=2, max_length=60)
    description: str = Field(min_length=10, max_length=240)
    service_area: Literal["长春市及近郊"]
    orders: list[ScenarioOrder] = Field(min_length=5, max_length=30)
    vehicles: list[ScenarioVehicle] = Field(min_length=1, max_length=10)
    warehouses: list[ScenarioWarehouse] = Field(min_length=1, max_length=10)


class ShowcaseCatalog(StrictModel):
    catalog_version: str
    source_mode: Literal["PRESET_SIMULATION"]
    scenarios: list[ShowcaseScenario] = Field(min_length=3, max_length=3)

    @field_validator("catalog_version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        if not CATALOG_VERSION_PATTERN.fullmatch(value):
            raise ValueError("目录版本必须匹配 YYYY-MM-DD.N")
        return value

    @model_validator(mode="after")
    def validate_scenarios(self):
        codes = {item.scenario_code for item in self.scenarios}
        keys = {item.public_key for item in self.scenarios}
        if codes != {"SCENARIO_1", "SCENARIO_2", "SCENARIO_3"} or len(keys) != 3:
            raise ValueError("场景目录必须完整定义三个固定场景")
        map_bounds = load_map_catalog().bounds
        for scenario in self.scenarios:
            if len({order.enterprise_name for order in scenario.orders}) < 3:
                raise ValueError(f"场景 {scenario.public_key} 至少需要三个模拟企业")
            if len({order.product_name for order in scenario.orders}) < 5:
                raise ValueError(f"场景 {scenario.public_key} 至少需要五个商品")
            order_keys = [order.order_key for order in scenario.orders]
            if len(order_keys) != len(set(order_keys)):
                raise ValueError(f"场景 {scenario.public_key} 的订单键必须唯一")
            for order in scenario.orders:
                if not map_bounds.contains(order.origin_longitude, order.origin_latitude):
                    raise ValueError(f"场景订单 {order.order_key} 的始发地超出服务范围")
                if not map_bounds.contains(order.destination_longitude, order.destination_latitude):
                    raise ValueError(f"场景订单 {order.order_key} 的目的地超出服务范围")
                if not (
                    order.warehouse_inbound_start < order.warehouse_inbound_end
                    and order.warehouse_outbound_start < order.warehouse_outbound_end
                ):
                    raise ValueError(f"场景订单 {order.order_key} 的入仓或出仓时间窗无效")
            for warehouse in scenario.warehouses:
                if not map_bounds.contains(warehouse.longitude, warehouse.latitude):
                    raise ValueError(f"场景仓库 {warehouse.warehouse_key} 超出服务范围")
                if warehouse.used_m3 + warehouse.reserved_m3 > warehouse.capacity_m3:
                    raise ValueError(f"场景仓库 {warehouse.warehouse_key} 已用与预留容量无效")
        return self


def canonical_json_bytes(data: object) -> bytes:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def catalog_sha256(model: BaseModel) -> str:
    return hashlib.sha256(canonical_json_bytes(model.model_dump(mode="json"))).hexdigest()


def _load(path: Path, model_type: type[BaseModel], *, maximum_bytes: int = 256 * 1024):
    content = path.read_bytes()
    if len(content) > maximum_bytes:
        raise ValueError(f"目录 {path.name} 超过 {maximum_bytes} 字节")
    if content.startswith(b"\xef\xbb\xbf"):
        raise ValueError(f"目录 {path.name} 不允许 UTF-8 BOM")
    return model_type.model_validate_json(content)


@lru_cache(maxsize=1)
def load_map_catalog() -> MapCatalog:
    return _load(MAP_CATALOG_PATH, MapCatalog)


@lru_cache(maxsize=1)
def load_information_catalog() -> InformationCatalog:
    return _load(INFORMATION_CATALOG_PATH, InformationCatalog)


@lru_cache(maxsize=1)
def load_showcase_catalog() -> ShowcaseCatalog:
    return _load(SHOWCASE_CATALOG_PATH, ShowcaseCatalog)


def validate_all_catalogs() -> dict[str, dict[str, str | int]]:
    catalogs = {
        "map": load_map_catalog(),
        "information": load_information_catalog(),
        "showcase": load_showcase_catalog(),
    }
    counts = {
        "map": len(catalogs["map"].points),
        "information": len(catalogs["information"].items),
        "showcase": len(catalogs["showcase"].scenarios),
    }
    return {
        name: {
            "catalog_version": model.catalog_version,
            "sha256": catalog_sha256(model),
            "item_count": counts[name],
        }
        for name, model in catalogs.items()
    }
