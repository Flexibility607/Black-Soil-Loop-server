from __future__ import annotations

import argparse
import json

from sqlalchemy import select

from app.domain.dashboard_catalogs import catalog_sha256, load_showcase_catalog
from app.domain.services import add_audit, add_outbox
from app.shared.database import SessionLocal
from app.shared.models import AlgorithmRun
from scripts.showcase_support import calculate_scenario


def generate(*, apply: bool, batch_key: str) -> dict:
    catalog = load_showcase_catalog()
    catalog_signature = catalog_sha256(catalog)
    calculated = []
    if not apply:
        for scenario in catalog.scenarios:
            signature = catalog_sha256(scenario)
            carpool, warehouse, _ = calculate_scenario(scenario)
            for algorithm_type, result in (("CARPOOL", carpool), ("WAREHOUSE", warehouse)):
                calculated.append(
                    {
                        "key": scenario.public_key,
                        "algorithm_type": algorithm_type,
                        "candidate_count": len(result.get("candidates", [])),
                        "input_signature": signature,
                        "existing": False,
                    }
                )
        return {
            "mode": "check",
            "batch_key": batch_key,
            "catalog_signature": catalog_signature,
            "runs": calculated,
        }
    with SessionLocal() as db:
        for scenario in catalog.scenarios:
            signature = catalog_sha256(scenario)
            carpool, warehouse, mappings = calculate_scenario(scenario)
            for algorithm_type, result in (("CARPOOL", carpool), ("WAREHOUSE", warehouse)):
                existing = db.scalar(
                    select(AlgorithmRun).where(
                        AlgorithmRun.algorithm_type == algorithm_type,
                        AlgorithmRun.showcase_key == scenario.public_key,
                        AlgorithmRun.showcase_batch_key == batch_key,
                        AlgorithmRun.input_signature == signature,
                    )
                )
                if existing is None and apply:
                    run = AlgorithmRun(
                        algorithm_type=algorithm_type,
                        scenario_code=scenario.scenario_code,
                        rules_version=result["rules_version"],
                        input_snapshot={
                            "catalog_version": catalog.catalog_version,
                            "public_key": scenario.public_key,
                            **mappings,
                        },
                        output_snapshot=result,
                        showcase_key=scenario.public_key,
                        showcase_batch_key=batch_key,
                        input_signature=signature,
                        input_data_cutoff=None,
                        showcase_enabled=True,
                        showcase_source_mode="PRESET_SIMULATION",
                    )
                    db.add(run)
                    db.flush()
                    add_outbox(
                        db,
                        "algorithm.run.completed",
                        "algorithm_run",
                        run.id,
                        1,
                        {"algorithm_type": algorithm_type, "showcase_key": scenario.public_key},
                    )
                    add_audit(
                        db,
                        run.id,
                        None,
                        "GENERATE_ALGORITHM_SHOWCASE",
                        "algorithm_run",
                        run.id,
                        None,
                        {
                            "algorithm_type": algorithm_type,
                            "showcase_key": scenario.public_key,
                            "source_mode": "PRESET_SIMULATION",
                        },
                    )
                calculated.append(
                    {
                        "key": scenario.public_key,
                        "algorithm_type": algorithm_type,
                        "candidate_count": len(result.get("candidates", [])),
                        "input_signature": signature,
                        "existing": existing is not None,
                    }
                )
        db.commit()
    return {
        "mode": "apply" if apply else "check",
        "batch_key": batch_key,
        "catalog_signature": catalog_signature,
        "runs": calculated,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="校验或生成 E02 预设算法展示运行")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--batch-key", required=True)
    args = parser.parse_args()
    print(json.dumps(generate(apply=args.apply, batch_key=args.batch_key), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
