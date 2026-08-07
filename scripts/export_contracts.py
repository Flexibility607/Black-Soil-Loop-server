from __future__ import annotations

import json
from pathlib import Path

from app.b01.main import app as b01_app
from app.b02.main import app as b02_app

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"
MINIAPP = CONTRACTS / "miniapp"
SCHEMAS = MINIAPP / "schemas"


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


COMMON_PROPERTIES = {
    "trace_id": {"type": "string"},
    "schema_version": {"type": "string", "const": "1.0"},
    "generated_at": {"type": "string", "format": "date-time"},
}


def main() -> None:
    write_json(CONTRACTS / "b01-openapi.json", b01_app.openapi())
    write_json(MINIAPP / "b02-openapi.json", b02_app.openapi())
    write_json(
        SCHEMAS / "error.schema.json",
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://api.flexibility607.cn/schemas/error.schema.json",
            "title": "统一错误响应",
            "type": "object",
            "required": ["code", "message", "details", "trace_id"],
            "properties": {
                "code": {"type": "string"},
                "message": {"type": "string"},
                "details": {"type": "object"},
                "trace_id": {"type": "string"},
            },
            "additionalProperties": False,
        },
    )
    write_json(
        SCHEMAS / "mobile-task.schema.json",
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://api.flexibility607.cn/schemas/mobile-task.schema.json",
            "title": "移动端运输任务",
            "type": "object",
            "required": [
                "id",
                "task_no",
                "status",
                "temperature_zone",
                "object_version",
                "stops",
                "route_label",
            ],
            "properties": {
                "id": {"type": "string", "format": "uuid"},
                "task_no": {"type": "string"},
                "source_order_ids": {"type": "array", "items": {"type": "string", "format": "uuid"}},
                "vehicle_id": {"type": "string", "format": "uuid"},
                "driver_id": {"type": "string", "format": "uuid"},
                "status": {"$ref": "status.schema.json#/$defs/transport_status"},
                "temperature_zone": {"enum": ["AMBIENT", "CHILLED", "FROZEN"]},
                "total_weight_kg": {"type": "number", "minimum": 0},
                "total_volume_m3": {"type": "number", "minimum": 0},
                "planned_departure_at": {"type": "string", "format": "date-time"},
                "object_version": {"type": "integer", "minimum": 1},
                "route_label": {"const": "经纬度估算路线"},
                "stops": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": [
                            "id",
                            "store_id",
                            "sequence_no",
                            "status",
                            "object_version",
                            "delivery_lines",
                        ],
                        "properties": {
                            "id": {"type": "string", "format": "uuid"},
                            "store_id": {"type": "string", "format": "uuid"},
                            "sequence_no": {"type": "integer", "minimum": 1},
                            "latitude": {"type": "number"},
                            "longitude": {"type": "number"},
                            "status": {"enum": ["PLANNED", "DELIVERED", "SIGNED"]},
                            "object_version": {"type": "integer", "minimum": 1},
                            "delivery_lines": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "required": ["order_ids", "product_id", "expected_quantity", "unit"],
                                    "properties": {
                                        "order_ids": {"type": "array", "items": {"type": "string"}},
                                        "product_id": {"type": "string"},
                                        "expected_quantity": {"type": "number", "minimum": 0},
                                        "unit": {"type": "string"},
                                    },
                                },
                            },
                        },
                    },
                },
            },
        },
    )
    write_json(
        SCHEMAS / "status.schema.json",
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://api.flexibility607.cn/schemas/status.schema.json",
            "$defs": {
                "transport_status": {
                    "enum": [
                        "DRAFT",
                        "MATCHED",
                        "CONFIRMED",
                        "PUBLISHED",
                        "DRIVER_ACCEPTED",
                        "PICKED_UP",
                        "IN_TRANSIT",
                        "DELIVERED",
                        "STORE_SIGNED",
                        "COMPLETED",
                        "CANCELLED",
                    ]
                },
                "receipt_status": {"enum": ["FULL", "PARTIAL", "REJECTED"]},
            },
        },
    )
    write_json(
        SCHEMAS / "realtime-event.schema.json",
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://api.flexibility607.cn/schemas/realtime-event.schema.json",
            "title": "WSS 实时事件",
            "type": "object",
            "required": [
                "cursor",
                "event_id",
                "topic",
                "object_type",
                "object_id",
                "object_version",
                "payload",
                "created_at",
            ],
            "properties": {
                "cursor": {"type": "integer", "minimum": 1},
                "event_id": {"type": "string", "format": "uuid"},
                "topic": {"type": "string"},
                "object_type": {"type": "string"},
                "object_id": {"type": "string"},
                "object_version": {"type": "integer", "minimum": 1},
                "payload": {"type": "object"},
                "created_at": {"type": "string", "format": "date-time"},
            },
        },
    )
    write_json(
        SCHEMAS / "list-envelope.schema.json",
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://api.flexibility607.cn/schemas/list-envelope.schema.json",
            "type": "object",
            "required": ["items", "page", "page_size", "total", *COMMON_PROPERTIES],
            "properties": {
                "items": {"type": "array"},
                "page": {"type": "integer", "minimum": 1},
                "page_size": {"type": "integer", "minimum": 0},
                "total": {"type": "integer", "minimum": 0},
                **COMMON_PROPERTIES,
            },
        },
    )


if __name__ == "__main__":
    main()
