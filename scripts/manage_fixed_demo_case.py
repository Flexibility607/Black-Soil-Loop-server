from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from pydantic import SecretStr

from app.domain.fixed_demo import (
    FIXED_DEMO_CATALOG_PATH,
    FixedDemoCatalog,
    guard_showcase_database,
    load_fixed_demo_catalog,
    materialize_fixed_demo_case,
    parse_anchor_date,
    read_fixed_demo_status,
    validate_fixed_demo_catalog,
)
from app.shared.config import get_settings
from app.shared.database import SessionLocal


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate, install, reset, or inspect the fixed showcase case")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--reset", action="store_true")
    mode.add_argument("--status", action="store_true")
    parser.add_argument("--catalog", type=Path, default=FIXED_DEMO_CATALOG_PATH)
    parser.add_argument("--case-key", default="changchun-fixed-showcase-v1")
    parser.add_argument("--expected-catalog-version")
    parser.add_argument("--expected-case-revision", type=int)
    parser.add_argument("--anchor-date", default="today")
    parser.add_argument("--password-file", type=Path)
    return parser


def _catalog(path: Path) -> FixedDemoCatalog:
    return load_fixed_demo_catalog(path.resolve())


def _password(path: Path | None) -> SecretStr | None:
    if path is None:
        return None
    resolved = path.resolve(strict=True)
    if os.name != "nt":
        stat = resolved.stat()
        if stat.st_uid != 0 or stat.st_mode & 0o077:
            raise RuntimeError("showcase password file must be owned by root with mode 0600")
    values = {}
    for line in resolved.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            raise RuntimeError("showcase password file has an invalid line")
        values[key.strip()] = value.strip()
    value = values.get("showcase_accounts_password")
    if value is None or len(value) < 12:
        raise RuntimeError("showcase password file is missing a valid account password")
    return SecretStr(value)


def main() -> None:
    args = _parser().parse_args()
    catalog = _catalog(args.catalog)
    if args.case_key != catalog.case_key:
        raise RuntimeError("--case-key does not match the controlled catalog")
    if args.expected_catalog_version and args.expected_catalog_version != catalog.catalog_version:
        raise RuntimeError("catalog version does not match --expected-catalog-version")
    settings = get_settings()
    expected_accounts = {item.username for item in catalog.accounts}
    if settings.showcase_account_username_set and settings.showcase_account_username_set != expected_accounts:
        raise RuntimeError("SHOWCASE_ACCOUNT_USERNAMES does not match the controlled catalog")

    if args.check:
        print(json.dumps({"mode": "check", **validate_fixed_demo_catalog(catalog)}, ensure_ascii=False))
        return

    with SessionLocal() as db:
        guard_showcase_database(db, require_process_role=True)
        if args.status:
            status = read_fixed_demo_status(db, args.case_key)
            print(json.dumps({"mode": "status", "status": status}, ensure_ascii=False))
            return
        result = materialize_fixed_demo_case(
            db,
            catalog=catalog,
            anchor_date=parse_anchor_date(args.anchor_date),
            password=_password(args.password_file),
            reset_reason="MANUAL_COMMAND" if args.reset else "INITIAL_APPLY",
            expected_case_revision=args.expected_case_revision,
            force_reset=args.reset,
            require_process_role=True,
        )
        db.commit()
        print(json.dumps({"mode": "reset" if args.reset else "apply", **result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
