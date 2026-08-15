from __future__ import annotations

from uuid import UUID, uuid5

from sqlalchemy import select

from app.domain.fixed_demo import today_shanghai
from app.shared.config import get_settings
from app.shared.database import SessionLocal
from app.shared.models import DemoCaseInstallation, OutboxEvent


def run_showcase_reset() -> dict[str, object]:
    with SessionLocal() as db:
        case_key = get_settings().showcase_case_key
        installation = db.scalar(
            select(DemoCaseInstallation)
            .where(DemoCaseInstallation.case_key == case_key)
            .with_for_update()
        )
        if installation is None:
            raise RuntimeError("fixed demo case has not been installed")
        anchor_date = today_shanghai()
        event_id = str(
            uuid5(UUID(installation.id), f"demo.case.reset_requested:scheduled:{anchor_date.isoformat()}")
        )
        existing = db.scalar(select(OutboxEvent).where(OutboxEvent.event_id == event_id))
        if existing is None:
            if installation.state != "READY":
                raise RuntimeError(f"fixed demo case is not ready: {installation.state}")
            db.add(
                OutboxEvent(
                    event_id=event_id,
                    topic="demo.case.reset_requested",
                    object_type="demo_case_installation",
                    object_id=installation.id,
                    object_version=installation.object_version,
                    payload={
                        "case_key": installation.case_key,
                        "expected_case_revision": installation.case_revision,
                        "reset_reason": "SCHEDULED_03_05_ASIA_SHANGHAI",
                    },
                )
            )
            installation.state = "REFRESHING"
        db.commit()
        return {
            "result": "queued" if existing is None else existing.status.lower(),
            "catalog_version": installation.catalog_version,
            "case_revision": installation.case_revision,
            "anchor_date": anchor_date.isoformat(),
            "refreshed_at": installation.refreshed_at.isoformat(),
        }


def main() -> None:
    result = run_showcase_reset()
    print(
        {
            "result": result["result"],
            "catalog_version": result["catalog_version"],
            "case_revision": result["case_revision"],
            "anchor_date": result["anchor_date"],
            "refreshed_at": result["refreshed_at"],
        }
    )


if __name__ == "__main__":
    main()
