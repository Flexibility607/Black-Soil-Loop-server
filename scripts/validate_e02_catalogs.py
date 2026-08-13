from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

from app.domain.dashboard_catalogs import load_map_catalog, load_showcase_catalog, validate_all_catalogs
from scripts.showcase_support import calculate_scenario

WEB_MAP_PATH = (
    Path(__file__).resolve().parents[2]
    / "web"
    / "frontdesign-v1"
    / "assets"
    / "maps"
    / "changchun-service-area.geojson"
)


def _rings(geometry: dict):
    if geometry["type"] == "Polygon":
        yield from geometry["coordinates"]
    elif geometry["type"] == "MultiPolygon":
        for polygon in geometry["coordinates"]:
            yield from polygon
    else:
        raise ValueError("GeoJSON 只允许 Polygon 或 MultiPolygon")


def _orientation(left, middle, right) -> float:
    return (middle[0] - left[0]) * (right[1] - left[1]) - (middle[1] - left[1]) * (
        right[0] - left[0]
    )


def _segments_cross(left_start, left_end, right_start, right_end) -> bool:
    values = (
        _orientation(left_start, left_end, right_start),
        _orientation(left_start, left_end, right_end),
        _orientation(right_start, right_end, left_start),
        _orientation(right_start, right_end, left_end),
    )
    return values[0] * values[1] < 0 and values[2] * values[3] < 0


def _validate_simple_ring(ring: list[list[float]]) -> None:
    segments = list(zip(ring[:-1], ring[1:], strict=True))
    for left_index, (left_start, left_end) in enumerate(segments):
        for right_index, (right_start, right_end) in enumerate(segments):
            if right_index <= left_index + 1:
                continue
            if left_index == 0 and right_index == len(segments) - 1:
                continue
            if _segments_cross(left_start, left_end, right_start, right_end):
                raise ValueError("GeoJSON 环存在自交")


def validate_geojson(path: Path = WEB_MAP_PATH) -> dict[str, object]:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError("GeoJSON 不允许 UTF-8 BOM")
    payload = json.loads(raw)
    if payload.get("type") != "FeatureCollection" or not payload.get("features"):
        raise ValueError("GeoJSON 必须是非空 FeatureCollection")
    coordinates: list[tuple[float, float]] = []
    for feature in payload["features"]:
        geometry = feature.get("geometry") or {}
        for ring in _rings(geometry):
            if len(ring) < 4 or ring[0] != ring[-1]:
                raise ValueError("GeoJSON 环必须闭合且至少包含四个坐标")
            _validate_simple_ring(ring)
            for coordinate in ring:
                if len(coordinate) < 2 or not all(math.isfinite(float(value)) for value in coordinate[:2]):
                    raise ValueError("GeoJSON 坐标必须是有限经纬度")
                coordinates.append((float(coordinate[0]), float(coordinate[1])))
    derived = [
        min(item[0] for item in coordinates),
        min(item[1] for item in coordinates),
        max(item[0] for item in coordinates),
        max(item[1] for item in coordinates),
    ]
    expected = load_map_catalog().bounds.as_list()
    if any(abs(left - right) > 0.03 for left, right in zip(derived, expected, strict=True)):
        raise ValueError(f"GeoJSON 边界 {derived} 与目录服务范围 {expected} 不一致")
    return {"sha256": hashlib.sha256(raw).hexdigest(), "bounds": derived, "feature_count": len(payload["features"])}


def validate_algorithms() -> dict[str, object]:
    results = []
    for scenario in load_showcase_catalog().scenarios:
        first = calculate_scenario(scenario)
        second = calculate_scenario(scenario)
        if first != second:
            raise ValueError(f"场景 {scenario.public_key} 的算法结果不确定")
        carpool, warehouse, _ = first
        if not carpool["candidates"]:
            raise ValueError(f"场景 {scenario.public_key} 未形成拼车候选")
        if not warehouse["candidates"]:
            raise ValueError(f"场景 {scenario.public_key} 未形成满足条件的拼仓候选")
        results.append(
            {
                "key": scenario.public_key,
                "carpool_groups": len(carpool["candidates"]),
                "warehouse_groups": len(warehouse["candidates"]),
            }
        )
    return {"scenarios": results}


def main() -> None:
    parser = argparse.ArgumentParser(description="校验 E02 正式目录、地图和预设算法输入")
    parser.add_argument(
        "--geojson",
        type=Path,
        default=WEB_MAP_PATH,
        help="长春服务范围 GeoJSON；独立服务器 release 必须显式传入网页制品中的文件",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            {
                "catalogs": validate_all_catalogs(),
                "geojson": validate_geojson(args.geojson),
                "algorithms": validate_algorithms(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
