from __future__ import annotations

import hashlib
from typing import Literal

from app.domain.dashboard_catalogs import catalog_sha256, load_information_catalog


def public_information(
    kind: Literal["all", "news", "policy"] = "all",
    limit: int = 8,
) -> dict:
    catalog = load_information_catalog()
    requested_kind = kind.upper()
    enabled = [item for item in catalog.items if item.enabled and (kind == "all" or item.kind == requested_kind)]
    enabled.sort(key=lambda item: (item.display_order, -item.published_at.timestamp(), item.slug))
    selected = enabled[:limit]
    items = [
        {
            "slug": item.slug,
            "kind": item.kind,
            "kind_label": "园区动态" if item.kind == "NEWS" else "政策资讯",
            "category": item.category,
            "title": item.title,
            "summary": item.summary,
            "published_at": item.published_at.isoformat(),
            "source_type": item.source_type,
            "source_name": item.source_name,
            "source_url": str(item.source_url) if item.source_url else None,
            "source_verified_at": item.source_verified_at.isoformat(),
            "service_scope": catalog.service_scope,
            "tone": item.tone,
        }
        for item in selected
    ]
    cutoff = max((item.published_at for item in enabled), default=None)
    etag_source = f"{catalog_sha256(catalog)}:{kind}:{limit}".encode()
    return {
        "items": items,
        "total": len(enabled),
        "kind": kind,
        "catalog_version": catalog.catalog_version,
        "service_scope": catalog.service_scope,
        "data_cutoff": cutoff,
        "etag": f'"public-information-{hashlib.sha256(etag_source).hexdigest()}"',
    }
