from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    manifest = load(ROOT / "contracts" / "compatibility-manifest.json")
    specs = {
        "b01": load(ROOT / "contracts" / "b01-openapi.json"),
        "b02": load(ROOT / "contracts" / "miniapp" / "b02-openapi.json"),
    }
    errors: list[str] = []
    for service, requirements in manifest["required_operations"].items():
        paths = specs[service]["paths"]
        for path, methods in requirements.items():
            if path not in paths:
                errors.append(f"{service} 缺少路径 {path}")
                continue
            for method in methods:
                if method.lower() not in paths[path]:
                    errors.append(f"{service} 缺少操作 {method.upper()} {path}")
    for schema_name in manifest["required_b02_schemas"]:
        if schema_name not in specs["b02"].get("components", {}).get("schemas", {}):
            errors.append(f"b02 缺少 Schema {schema_name}")
    if errors:
        raise SystemExit("\n".join(errors))
    print("契约兼容性检查通过")


if __name__ == "__main__":
    main()
