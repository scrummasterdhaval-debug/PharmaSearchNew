from __future__ import annotations

from typing import Any

from sources.source_registry import connector_metadata


CORE_CONNECTOR_STATUS: dict[str, dict[str, str]] = {
    "FDA": {
        "tier": "core",
        "mode": "live_api",
        "status": "ready",
        "notes": "OpenFDA label/NDC APIs. Returns product records when OpenFDA has matching substance metadata.",
    },
    "MHRA": {
        "tier": "core",
        "mode": "live_api_pdf",
        "status": "ready",
        "notes": "MHRA API plus SmPC/PIL/PAR URL merge and PDF metadata extraction.",
    },
    "EMA": {
        "tier": "core",
        "mode": "dataset_html_pdf",
        "status": "ready",
        "notes": "EMA medicine data plus product page/document enrichment.",
    },
    "EU MRI Product Index": {
        "tier": "eu_national",
        "mode": "live_html_or_registry",
        "status": "partial",
        "notes": "HMA MRI Product Index for MRP/DCP products. Current official service may return 503; connector parses table rows when available and otherwise provides official registry handoff.",
    },
    "Health Canada": {
        "tier": "core",
        "mode": "live_api",
        "status": "ready",
        "notes": "Health Canada DPD API. Some document fields may require later deep extraction.",
    },
    "TGA Australia": {
        "tier": "core",
        "mode": "live_html_or_registry",
        "status": "partial",
        "notes": "ARTG page is slow/intermittent from this environment. No hardcoded product fallback; manual registry row when blocked.",
    },
    "Medsafe New Zealand": {
        "tier": "core",
        "mode": "form_or_registry",
        "status": "partial",
        "notes": "Medsafe app shell is reachable, but direct result extraction is unstable. Manual registry row when no product rows are parsed.",
    },
    "Hong Kong Drug Office": {
        "tier": "core",
        "mode": "live_form_detail",
        "status": "ready",
        "notes": "Official form search and product detail extraction for product, holder, registration number/date.",
    },
    "NMPA China": {
        "tier": "asia_national",
        "mode": "open_register",
        "status": "ready",
        "notes": "NMPA's quarterly National Drug Code list, indexed locally: approval number, generic name, form, strength, holder and manufacturer. Generic names only (NMPA lists no brands); the product database itself blocks scripts and is not used.",
    },
    "GRLS Russia": {
        "tier": "core",
        "mode": "open_register",
        "status": "ready",
        "notes": "GRLS's published register file, indexed locally; no browser needed.",
    },
    "Belgium FAMHP": {
        "tier": "eu_national",
        "mode": "live_html",
        "status": "partial",
        "notes": "EU national connector; coverage depends on regulator page search behavior.",
    },
    "France BDPM": {
        "tier": "eu_national",
        "mode": "live_html",
        "status": "partial",
        "notes": "EU national connector; coverage depends on regulator page search behavior.",
    },
    "Ireland medicines.ie": {
        "tier": "eu_national",
        "mode": "live_html_documents",
        "status": "partial",
        "notes": "EU national connector with document links where available.",
    },
    "Spain CIMA": {
        "tier": "eu_national",
        "mode": "live_api",
        "status": "ready",
        "notes": "CIMA API connector for Spain.",
    },
    "BfArM Germany": {
        "tier": "eu_national",
        "mode": "live_view_only",
        "status": "ready_view_only",
        "notes": (
            "AMIce public database: holder and batch-release sites with addresses, live per search. "
            "BfArM's terms allow its results to be shown and printed, not stored or passed on, so rows "
            "are not saved and are left out of exports until BfArM grants permission to reuse the data."
        ),
    },
}


def _register_status(name: str) -> dict[str, str] | None:
    """What a downloaded register holds, read from its own index; None if it is not one."""
    import time

    from sources import open_registers

    register = open_registers.REGISTERS.get(name)
    if register is None:
        return None
    info = open_registers.index_info(register) or {}
    if not info and open_registers._recently_failed(register):
        status = "download_failed"
        notes = "The last download failed; it is tried again within six hours."
    elif not info:
        status = "not_downloaded_yet"
        notes = "Downloads on first use, or at the next daily refresh."
    else:
        status = "ready"
        try:
            age_days = (time.time() - float(info.get("fetched_epoch", 0))) / 86400
            age = f"{age_days:.0f} days old" if age_days >= 1 else "less than a day old"
        except (TypeError, ValueError):
            age = "age unknown"
        notes = f"{int(info.get('rows', 0) or 0):,} rows from the regulator's published file, {age}."
        if info and open_registers._recently_failed(register):
            notes += " The latest refresh failed; this copy is still served."
    return {"tier": "register", "mode": "open_register", "status": status, "notes": notes}


def connector_status_rows() -> list[dict[str, Any]]:
    """Every connector the platform searches, not only the ones described by hand.

    A hand-written entry wins where there is one; a downloaded register reports
    from its own index; anything else is a live search.
    """
    rows: list[dict[str, Any]] = []
    for metadata in connector_metadata():
        name = metadata["name"]
        status = CORE_CONNECTOR_STATUS.get(name) or _register_status(name) or {
            "tier": "live",
            "mode": "live_search",
            "status": "live",
            "notes": "Asked at search time; see Connector Health for how its recent searches went.",
        }
        if name in CORE_CONNECTOR_STATUS:
            register = _register_status(name)
            if register:
                status = {**status, "status": register["status"], "notes": f"{status.get('notes', '')} {register['notes']}".strip()}
        rows.append(
            {
                "name": name,
                "region": metadata.get("region", ""),
                "countries": metadata.get("countries", []),
                "enabled": metadata.get("enabled", False),
                "supports_live_search": metadata.get("supports_live_search", False),
                "supports_documents": metadata.get("supports_documents", False),
                **status,
            }
        )
    return rows
