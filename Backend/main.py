from html import escape
from pathlib import Path
from collections import Counter
from math import ceil
import re
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from admin_routes import router as admin_router
from services.admin_auth import SESSION_COOKIE_NAME, is_admin, read_session
from services.result_cache import BoundedResultCache

from config import BASE_DIR, DB_PATH, EXPORT_DIR
from core.logging_config import configure_logging, get_logger
from export_service import write_excel_export
from repository import (
    PRODUCT_DETAILS_SEED_PATH,
    initialize_database,
    list_search_jobs,
    list_product_details,
    save_product_detail,
    seed_product_details_if_needed,
)
from sources.ema import EU_COUNTRIES, find_product_url, run_ema_search
from sources.ema_product_parser import NotAnEmaPage, extract_product_page
from sources.open_registers import warm_open_registers
from sources.mhra import run_mhra_search
from sources.mhra_product_parser import extract_mhra_product_page
from sources.search_engine import COMPLETE_SEARCH_TIMEOUT_SECONDS, search_substance
from sources.source_registry import connector_metadata
from services.search_pipeline import (
    COUNTRY_SOURCE_DEFAULTS,
    COUNTRY_OPTIONS,
    DEFAULT_SOURCES,
    REGION_OPTIONS,
    combined_search,
    enriched_cached_results,
    filtered_search_results,
    parse_sources,
    prepared_cached_results,
    sources_for_scope,
)
from services.ai_client import ai_status as current_ai_status
from services.connector_health import connector_health_rows
from services.connector_status import connector_status_rows
from services.english_normalizer import english_text
from services.field_availability import missing_field_value
from services.harvest import harvest_coverage
from services.harvest_vocabulary import load_vocabulary
from services.result_formatter import formatted_result_row
from services.web_security import add_security, check_production_configuration
from services.search_jobs import FAST_BACKGROUND_SOURCES, create_search_job, get_search_job, get_search_job_results


configure_logging()
logger = get_logger(__name__)
app = FastAPI(title="PharmaSearch", version="0.2.0")
# A deployment that says it is production must have its secrets; this raises
# before the first request rather than serving an open site.
check_production_configuration()
add_security(app)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.include_router(admin_router)

# The sign-in page is the only way in. Everything below it needs a session:
# the search pages, the exports and the JSON endpoints alike.
PUBLIC_PATHS = {"/admin/login", "/admin/register", "/admin/verify-email", "/admin/forgot-password", "/admin/reset-password", "/favicon.ico", "/healthz"}
PUBLIC_PREFIXES = ("/static",)

# What a registered account may not reach. Signing up opens the search, the
# results and the exports. The admin area, and the routes that write to the
# database or describe the server, are for admins.
ADMIN_ONLY_PREFIXES = (
    "/admin", "/crawl", "/mhra_full", "/reset_db", "/api/data_health", "/api/export_directory",
    # Everything but the search itself: the stored catalogue, the connectors
    # and how they are doing, the coverage reports and the heavy exports.
    "/catalogue", "/products", "/connector_", "/ai_status", "/harvest_coverage", "/deep_export",
)
# Exact paths only: /search_jobs lists every user's searches, while
# /search_jobs/<id> is one person's own search and stays theirs.
ADMIN_ONLY_PATHS = {"/search_jobs"}
# Under /admin, and still everyone's: leaving has to work for any account.
SIGNED_IN_PATHS = {"/admin/logout"}


def _wants_json(request: Request) -> bool:
    path = request.url.path
    return path.startswith("/api") or path.startswith("/admin/api") or "application/json" in (
        request.headers.get("accept") or ""
    )


def _admins_only(request: Request):
    if _wants_json(request):
        return JSONResponse({"detail": "Administrators only"}, status_code=403)
    return templates.TemplateResponse(request=request, name="admin/forbidden.html", context={}, status_code=403)


@app.middleware("http")
async def require_sign_in(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
        return await call_next(request)
    user = read_session(request.cookies.get(SESSION_COOKIE_NAME))
    if user:
        admin_only = path.startswith(ADMIN_ONLY_PREFIXES) or path.rstrip("/") in ADMIN_ONLY_PATHS
        if admin_only and path not in SIGNED_IN_PATHS and not is_admin(user):
            return _admins_only(request)
        return await call_next(request)
    # A browser gets sent to the door and returned to where it was heading.
    # Anything programmatic gets told plainly, rather than handed a login page
    # it would try to parse as data.
    if _wants_json(request):
        return JSONResponse({"detail": "Sign in required"}, status_code=401)
    destination = request.url.path
    if request.url.query:
        destination = f"{destination}?{request.url.query}"
    return RedirectResponse(
        f"/admin/login?next={quote(destination, safe='')}", status_code=303
    )
# Sixteen recent result lists, each for half an hour -- enough for a person to
# search, sort, filter and then export, without memory growing with every click.
SEARCH_RESULT_CACHE = BoundedResultCache(max_entries=16, ttl_seconds=30 * 60)
APP_BUILD = "UAT-2026-08-01-platform-core"


def result_cache_key(
    substance: str,
    live: bool,
    sources: list[str],
    country: str = "",
    region: str = "",
    source: str = "",
    status: str = "",
    table_search: str = "",
    substance_filter: str = "",
    product_filter: str = "",
    company_filter: str = "",
    strength_filter: str = "",
    dosage_form_filter: str = "",
    pack_size_filter: str = "",
    atc_code_filter: str = "",
    therapeutic_category_filter: str = "",
    ma_holder_filter: str = "",
    manufacturer_name_filter: str = "",
    manufacturer_country_filter: str = "",
    registration_number_filter: str = "",
    registration_date_filter: str = "",
    sort_by: str = "",
    sort_dir: str = "asc",
) -> tuple[Any, ...]:
    return (
        substance.strip().lower(),
        bool(live),
        tuple(sources),
        country,
        region,
        source,
        status,
        table_search,
        substance_filter,
        product_filter,
        company_filter,
        strength_filter,
        dosage_form_filter,
        pack_size_filter,
        atc_code_filter,
        therapeutic_category_filter,
        ma_holder_filter,
        manufacturer_name_filter,
        manufacturer_country_filter,
        registration_number_filter,
        registration_date_filter,
        sort_by,
        "desc" if sort_dir == "desc" else "asc",
    )


def h(value: object) -> str:
    return escape("" if value is None else str(value))


def link_or_unavailable(url: str, label: str, missing_text: str = "Not available") -> str:
    if not url:
        return f'<span class="text-muted">{h(missing_text)}</span>'
    return f'<a href="{h(url)}" target="_blank" rel="noopener noreferrer">{h(label)}</a>'


def unique_values(rows: list[dict[str, Any]], field: str) -> list[str]:
    values = {
        str(row.get(field) or "").strip()
        for row in rows
        if str(row.get(field) or "").strip()
    }
    return sorted(values, key=str.lower)


def option_tags(values: list[str], selected_value: str, all_label: str = "All") -> str:
    tags = [f'<option value="">{h(all_label)}</option>']
    for value in values:
        selected = " selected" if value == selected_value else ""
        tags.append(f'<option value="{h(value)}"{selected}>{h(value)}</option>')
    return "".join(tags)


def merged_options(primary_values: list[str], fixed_values: list[str]) -> list[str]:
    seen = set()
    merged = []
    for value in list(fixed_values) + list(primary_values):
        clean_value = str(value or "").strip()
        if not clean_value:
            continue
        key = clean_value.lower()
        if key in seen:
            continue
        seen.add(key)
        merged.append(clean_value)
    return merged


@app.on_event("startup")
def startup() -> None:
    logger.info("Starting PharmaSearch backend")
    from services.admin_auth import describe_admin_configuration

    logger.info(describe_admin_configuration())
    initialize_database()
    # Download any published register that is missing or stale, on a background
    # thread, so the first Italian or Brazilian search finds it ready.
    warm_open_registers()


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "sources": connector_metadata(),
            "fast_background_sources": FAST_BACKGROUND_SOURCES,
            "country_options": COUNTRY_OPTIONS,
            "region_options": REGION_OPTIONS,
            "user": (user := read_session(request.cookies.get(SESSION_COOKIE_NAME))),
            "user_is_admin": is_admin(user),
        },
    )


@app.get("/global_search/{substance:path}")
def global_search(
    substance: str,
    sources: list[str] | None = Query(default=None),
):
    results = search_substance(substance, source_names=parse_sources(sources))
    return {"substance": substance, "count": len(results), "results": results}


@app.get("/healthz")
def healthz():
    """Whether the process is up, for a load balancer or container check.

    Public on purpose, and it says nothing about the data: a monitor must be
    able to reach it without a session.
    """
    return {"status": "ok", "build": APP_BUILD}


@app.get("/api/sources")
def api_sources():
    return {"sources": connector_metadata()}


@app.get("/api/version")
def api_version():
    return {"app": "PharmaSearch", "build": APP_BUILD}


@app.get("/api/data_health")
def api_data_health():
    inserted = seed_product_details_if_needed()
    rows = list_product_details()
    sample_substances = [
        "metformin",
        "paracetamol",
        "dapagliflozin",
        "quetiapine",
        "mirabegron",
    ]
    sample_counts = {
        substance: sum(
            1
            for row in rows
            if substance in str(row.get("substance") or "").lower()
            or substance in str(row.get("product") or "").lower()
        )
        for substance in sample_substances
    }
    return {
        "app": "PharmaSearch",
        "build": APP_BUILD,
        "database_path": str(DB_PATH),
        "seed_file": str(PRODUCT_DETAILS_SEED_PATH),
        "seed_file_exists": PRODUCT_DETAILS_SEED_PATH.exists(),
        "seed_file_size": PRODUCT_DETAILS_SEED_PATH.stat().st_size
        if PRODUCT_DETAILS_SEED_PATH.exists()
        else 0,
        "seed_rows_inserted_now": inserted,
        "product_details_count": len(rows),
        "sample_counts": sample_counts,
    }


@app.get("/api/export_directory")
def api_export_directory():
    return {"export_directory": str(EXPORT_DIR)}


@app.get("/api/search")
def api_search(
    substance: str,
    live: bool = True,
    sources: list[str] | None = Query(default=None),
    country: str = "",
    region: str = "",
    source: str = "",
    refresh: bool = False,
):
    if not substance.strip():
        return {
            "substance": substance,
            "count": 0,
            "results": [],
            "message": "Please enter the active substance name.",
        }
    freshness = []
    results, _, _ = filtered_search_results(
        substance,
        live=live,
        sources=sources,
        country=country,
        region=region,
        source=source,
        force_live=refresh,
        freshness_report=freshness,
    )
    return {
        "substance": substance,
        "count": len(results),
        "results": results,
        "answered_from_store": [
            {"source": item.source, "checked_at": item.checked_at}
            for item in freshness if not item.ask_live
        ],
    }


@app.get("/search/{substance:path}")
def search_saved(
    substance: str,
    live: bool = True,
    sources: list[str] = Query(default=DEFAULT_SOURCES),
    refresh: bool = False,
):
    return combined_search(substance, include_live=live, sources=sources, force_live=refresh)


@app.get("/search_jobs/start")
def start_search_job(
    substance: str,
    sources: list[str] = Query(default=DEFAULT_SOURCES),
    mode: str = "fast",
    refresh: bool = False,
):
    if not substance.strip():
        return RedirectResponse(url="/", status_code=303)
    job_id = create_search_job(substance, sources, mode=mode, refresh=refresh)
    return RedirectResponse(url=f"/search_jobs/{job_id}", status_code=303)


@app.get("/api/search_jobs/{job_id}")
def search_job_status_api(job_id: str):
    job = get_search_job(job_id)
    if not job:
        return {"error": "Search job not found"}
    return job


@app.get("/search_jobs", response_class=HTMLResponse)
def recent_search_jobs_page():
    jobs = list_search_jobs(limit=50)
    rows = []
    for job in jobs:
        rows.append(
            f"""
            <tr>
                <td>{h(job["created_at"])}</td>
                <td>{h(job["substance"])}</td>
                <td>{h(job["mode"])}</td>
                <td>{h(job["status"])}</td>
                <td>{h(job["record_count"])}</td>
                <td>
                    <a class="btn btn-sm btn-outline-primary" href="/search_jobs/{h(job["job_id"])}">Progress</a>
                    <a class="btn btn-sm btn-outline-success" href="/search_jobs/{h(job["job_id"])}/results">Results</a>
                    <a class="btn btn-sm btn-success" href="/search_jobs/{h(job["job_id"])}/export">Export</a>
                </td>
            </tr>
            """
        )
    if not rows:
        rows.append('<tr><td colspan="6" class="text-center text-muted">No saved search jobs yet.</td></tr>')
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Recent Searches</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    </head>
    <body>
    <div class="container-fluid px-4 mt-4">
        <div class="d-flex justify-content-between align-items-center mb-3">
            <h2>Recent Searches</h2>
            <a class="btn btn-secondary" href="/">Back</a>
        </div>
        <table class="table table-striped table-bordered align-middle">
            <thead class="table-dark">
                <tr>
                    <th>Created</th>
                    <th>Substance</th>
                    <th>Mode</th>
                    <th>Status</th>
                    <th>Records</th>
                    <th>Actions</th>
                </tr>
            </thead>
            <tbody>{"".join(rows)}</tbody>
        </table>
    </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


def records_with_total(records: object, available: object) -> str:
    """ "100 of 3,276" where a registry holds more than it returned."""
    shown = int(records or 0)
    total = int(available or 0)
    return f"{shown:,} of {total:,}" if total > shown else f"{shown:,}"


def _capped_counts(pairs: list[tuple[str, int, int]]) -> list[str]:
    return [f"{source} returned {shown:,} of {total:,}" for source, shown, total in pairs if total > shown]


def capped_job_note(progress: list[dict[str, Any]]) -> str:
    capped = _capped_counts(
        [(item["source"], int(item.get("records") or 0), int(item.get("available") or 0)) for item in progress]
    )
    if not capped:
        return ""
    return (
        '<p class="alert alert-warning py-2">'
        f'Not every product was fetched: {h("; ".join(capped))}. '
        "These registries stop at a fixed number per search for now, so their counts are partial."
        "</p>"
    )


def capped_sources_note(rows: list[dict[str, Any]]) -> str:
    """Name the registries whose results stopped short of what they hold.

    FDA returns its first 100 labels and Health Canada its first 50 products;
    acetaminophen has 3,276 labels and 1,348 Canadian products. A results page
    that says nothing reads as the complete list.
    """
    shown: Counter[str] = Counter()
    totals: dict[str, int] = {}
    for row in rows:
        total = int(row.get("available_total") or 0)
        if not total:
            continue
        source = str(row.get("source") or "")
        shown[source] += 1
        totals[source] = max(totals.get(source, 0), total)
    capped = _capped_counts([(source, shown[source], totals[source]) for source in totals])
    if not capped:
        return ""
    return (
        '<p class="alert alert-warning py-2">'
        f'Partial results: {h("; ".join(capped))}. '
        "These registries stop at a fixed number per search for now."
        "</p>"
    )


@app.get("/search_jobs/{job_id}", response_class=HTMLResponse)
def search_job_page(job_id: str):
    job = get_search_job(job_id)
    if not job:
        return HTMLResponse(
            content="""
            <!DOCTYPE html>
            <html><head><title>Search Job</title>
            <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
            </head><body><div class="container mt-5">
            <div class="alert alert-warning">Search job not found.</div>
            <a class="btn btn-secondary" href="/">Back</a>
            </div></body></html>
            """,
            status_code=404,
        )
    rows = []
    for progress in job["progress"]:
        badge = {
            "done": "success",
            "running": "primary",
            "failed": "danger",
            "timeout": "warning",
            "queued": "secondary",
            "skipped": "secondary",
        }.get(progress["status"], "secondary")
        rows.append(
            f"""
            <tr>
                <td>{h(progress["source"])}</td>
                <td><span class="badge text-bg-{badge}">{h(progress["status"])}</span></td>
                <td>{h(records_with_total(progress.get("records"), progress.get("available")))}</td>
                <td>{h(progress["error"])}</td>
            </tr>
            """
        )
    refresh_meta = '<meta http-equiv="refresh" content="5">' if job["status"] in {"queued", "running"} else ""
    results_button = (
        f'<a class="btn btn-success" href="/search_jobs/{h(job_id)}/results">Open Results</a>'
        if job["record_count"]
        else '<button class="btn btn-outline-secondary" disabled>No results yet</button>'
    )
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Search Job</title>
        {refresh_meta}
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    </head>
    <body>
    <div class="container-fluid px-4 mt-4">
        <div class="d-flex justify-content-between align-items-center mb-3">
            <div>
                <h2>Search Job Progress</h2>
                <p class="mb-0">Active Substance: <strong>{h(job["substance"])}</strong></p>
                <p class="mb-0">Mode: <strong>{h(job["mode"])}</strong></p>
                <p class="text-muted small mb-0">Job ID: {h(job_id)}</p>
            </div>
            <div class="d-flex gap-2">
                <a class="btn btn-secondary" href="/">Back</a>
                {results_button}
            </div>
        </div>
        <div class="alert alert-info">
            Status: <strong>{h(job["status"])}</strong>. Records found so far: <strong>{h(job["record_count"])}</strong>.
            This page refreshes every 5 seconds while the job is running.
            Fast mode skips heavy sources; use Full Background Search when you need every selected source.
        </div>
        <table class="table table-striped table-bordered align-middle">
            <thead class="table-dark">
                <tr>
                    <th>Source</th>
                    <th>Status</th>
                    <th>Records</th>
                    <th>Error</th>
                </tr>
            </thead>
            <tbody>{"".join(rows)}</tbody>
        </table>
        {capped_job_note(job["progress"])}
    </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


@app.get("/search_jobs/{job_id}/results", response_class=HTMLResponse)
def search_job_results_page(job_id: str):
    job = get_search_job(job_id)
    if not job:
        return HTMLResponse("<h2>Search job not found</h2>", status_code=404)
    saved_rows = get_search_job_results(job_id)
    rows = prepared_cached_results(saved_rows[:200])
    columns = [
        ("molecule", "Molecule"),
        ("product", "Product Name"),
        ("company", "Company"),
        ("country", "Country"),
        ("region", "Region"),
        ("registration_status", "Registration Status"),
        ("source", "Source"),
        ("strength", "Strength"),
        ("dosage_form", "Dosage Form"),
        ("pack_size", "Pack Size"),
        ("atc_code", "ATC Code"),
        ("therapeutic_category", "Therapeutic Category"),
        ("ma_holder", "MA Holder Name"),
        ("manufacturer_name", "Manufacturer Name"),
        ("manufacturer_country", "Manufacturer Country"),
        ("registration_number", "Registration Number"),
        ("registration_date", "Registration Date"),
        ("product_details_url", "Product Details"),
        ("smpc_url", "SmPC URL"),
        ("pil_url", "PIL URL"),
        ("assessment_report_url", "Assessment Report"),
        ("data_confidence", "Data Confidence"),
        ("enrichment_status", "Enrichment Status"),
        ("missing_fields", "Missing Fields"),
    ]
    link_columns = {
        "product_details_url": "Open Product",
        "smpc_url": "Open SmPC",
        "pil_url": "Open PIL",
        "assessment_report_url": "Open Assessment",
    }
    body_rows = []
    for row in rows[:200]:
        display_row = formatted_result_row(row, searched_substance=job["substance"])
        display_row.update(
            {
                "data_confidence": row.get("data_confidence", ""),
                "enrichment_status": row.get("enrichment_status", ""),
                "missing_fields": row.get("missing_fields", ""),
            }
        )
        cells = []
        for key, _label in columns:
            value = display_row.get(key, "")
            if key in link_columns:
                cells.append(
                    f"<td>{link_or_unavailable(value, link_columns[key], missing_field_value(row, key))}</td>"
                )
            else:
                cells.append(f"<td>{h(value)}</td>")
        body_rows.append(f'<tr class="result-row">{"".join(cells)}</tr>')
    if not body_rows:
        body_rows.append(
            f'<tr><td colspan="{len(columns)}" class="text-center text-muted">No records found yet.</td></tr>'
        )
    header_cells = "".join(f"<th>{h(label)}</th>" for _key, label in columns)
    filter_cells = "".join(
        f'<th><input class="form-control form-control-sm column-filter" '
        f'data-column="{index}" placeholder="Filter" aria-label="Filter {h(label)}"></th>'
        for index, (_key, label) in enumerate(columns)
    )
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Search Job Results</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
        <style>
            .results-scroll {{ overflow: auto; max-height: calc(100vh - 190px); }}
            .results-table {{ min-width: 4200px; font-size: .88rem; }}
            .results-table th {{ min-width: 150px; vertical-align: top; }}
            .results-table th:nth-child(2) {{ min-width: 260px; }}
            .results-table th:nth-child(12), .results-table th:nth-child(23),
            .results-table th:nth-child(24) {{ min-width: 300px; }}
            .results-table thead tr:first-child th {{ position: sticky; top: 0; z-index: 2; }}
            .results-table thead tr.filters th {{ position: sticky; top: 42px; z-index: 2; background: #dee2e6; }}
            .results-table td {{ white-space: normal; overflow-wrap: anywhere; }}
        </style>
    </head>
    <body>
    <div class="container-fluid px-4 mt-4">
        <div class="d-flex justify-content-between align-items-center mb-3">
            <div>
                <h2>Search Job Results</h2>
                <p class="mb-0">Active Substance: <strong>{h(job["substance"])}</strong></p>
                <p class="text-muted small mb-0">Showing {len(rows)} of {len(saved_rows)} records from this background job.</p>
                {capped_sources_note(saved_rows)}
            </div>
            <div class="d-flex gap-2">
                <a class="btn btn-secondary" href="/search_jobs/{h(job_id)}">Progress</a>
                <a class="btn btn-success" href="/search_jobs/{h(job_id)}/export">Export Excel</a>
                <a class="btn btn-secondary" href="/">Back</a>
            </div>
        </div>
        <p id="filter-count" class="mb-2">Visible records: {len(rows)}</p>
        <div class="results-scroll">
            <table class="table table-striped table-bordered align-middle results-table">
                <thead class="table-dark">
                    <tr>{header_cells}</tr>
                    <tr class="filters">{filter_cells}</tr>
                </thead>
                <tbody id="results-body">{"".join(body_rows)}</tbody>
            </table>
        </div>
    </div>
    <script>
        const filters = Array.from(document.querySelectorAll('.column-filter'));
        const resultRows = Array.from(document.querySelectorAll('#results-body .result-row'));
        const filterCount = document.getElementById('filter-count');
        function applyColumnFilters() {{
            let visible = 0;
            resultRows.forEach((row) => {{
                const matches = filters.every((input) => {{
                    const query = input.value.trim().toLowerCase();
                    if (!query) return true;
                    const cell = row.cells[Number(input.dataset.column)];
                    return cell && cell.textContent.toLowerCase().includes(query);
                }});
                row.hidden = !matches;
                if (matches) visible += 1;
            }});
            filterCount.textContent = `Visible records: ${{visible}} of ${{resultRows.length}} displayed`;
        }}
        filters.forEach((input) => input.addEventListener('input', applyColumnFilters));
    </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


@app.get("/search_jobs/{job_id}/export")
def export_search_job_results(job_id: str):
    job = get_search_job(job_id)
    if not job:
        return HTMLResponse("<h2>Search job not found</h2>", status_code=404)
    rows = enriched_cached_results(get_search_job_results(job_id))
    path = write_excel_export(job["substance"], rows, deep=False)
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=Path(path).name,
    )


def document_cell(row, display_row, field, label):
    """The document this regulator published for this authorisation, or nothing.

    Only the row's own document belongs in the column. Another regulator's
    document for the same molecule is not this authorisation's label, and a
    caption naming the lender does not stop a reader taking an MHRA leaflet
    for the FDA one: the US column showed an MHRA leaflet for NASACORT that
    way. Where the regulator publishes nothing the column says so, and the
    reader looks the document up at the source themselves.

    The borrowed link is not lost. It keeps its own Molecule Reference
    columns in the Excel export, where it is never mistaken for this one.
    """
    own_url = str(display_row.get(field) or "").strip()
    if own_url:
        return f'<a href="{h(own_url)}" target="_blank" rel="noopener">Open {label}</a>'
    return f'<span class="text-muted">{h(missing_field_value(row, field))}</span>'


@app.get("/search_page", response_class=HTMLResponse)
def search_page(
    substance: str,
    live: bool = True,
    sources: list[str] | None = Query(default=None),
    page: int = 1,
    page_size: int = 50,
    country: str = "",
    region: str = "",
    source: str = "",
    status: str = "",
    table_search: str = "",
    substance_filter: str = "",
    product_filter: str = "",
    company_filter: str = "",
    strength_filter: str = "",
    dosage_form_filter: str = "",
    pack_size_filter: str = "",
    atc_code_filter: str = "",
    therapeutic_category_filter: str = "",
    ma_holder_filter: str = "",
    manufacturer_name_filter: str = "",
    manufacturer_country_filter: str = "",
    registration_number_filter: str = "",
    registration_date_filter: str = "",
    sort_by: str = "",
    sort_dir: str = "asc",
    search_mode: str = "fast",
    export_all: bool = False,
    refresh: bool = False,
):
    if not substance.strip():
        return HTMLResponse(
            content="""
            <!DOCTYPE html>
            <html>
            <head>
                <title>PharmaSearch Results</title>
                <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
            </head>
            <body>
            <div class="container mt-5">
                <h2>PharmaSearch Results</h2>
                <div class="alert alert-warning">
                    <strong>Please enter the active substance name.</strong>
                </div>
                <a class="btn btn-primary" href="/">Back to search</a>
            </div>
            </body>
            </html>
            """,
            status_code=200,
        )
    sort_dir = "desc" if sort_dir == "desc" else "asc"
    status = english_text(status)
    normalized_search_mode = "full" if search_mode == "full" else "fast"
    requested_sources = sources if isinstance(sources, list) else [sources] if sources else []
    if not requested_sources:
        # A full search with no explicit source list means every source, not the
        # quick-search shortlist.
        default_sources = (
            [item["name"] for item in connector_metadata() if item.get("enabled", True)]
            if normalized_search_mode == "full"
            else sorted(FAST_BACKGROUND_SOURCES)
        )
        sources = default_sources
        requested_sources = list(sources)
    requested_source_names = {str(item).strip().lower() for item in requested_sources if str(item).strip()}
    slow_live_sources = {
        "cdsco india",
        "grls russia",
        "mhra",
        "ema",
        "tga australia",
        "medsafe new zealand",
        "eu mri product index",
        "belgium famhp",
        "ireland medicines.ie",
    }
    country_default_source = COUNTRY_SOURCE_DEFAULTS.get(country, "").strip().lower()
    auto_slow_country_source = (
        normalized_search_mode == "fast"
        and country_default_source in slow_live_sources
        and country_default_source not in requested_source_names
    )
    effective_live = bool(live and not auto_slow_country_source)
    # A full search waited 45s, and the registers answer in about 50: Italy,
    # Brazil and half of Belgium were cut off a moment before they replied, so
    # rosuvastatin + ezetimibe showed 138 products where 233 exist.
    live_timeout = 90 if normalized_search_mode == "full" else 10
    selected_for_live = parse_sources(sources)
    if normalized_search_mode == "full":
        live_sources = selected_for_live
    elif country or region or source:
        live_sources = sources_for_scope(
            selected_for_live,
            country=country,
            region=region,
            source=source,
        )
    else:
        live_sources = [
            item for item in selected_for_live if item in FAST_BACKGROUND_SOURCES
        ]
    freshness = []
    rows, selected_sources, all_rows = filtered_search_results(
        substance,
        live=effective_live,
        sources=sources,
        country=country,
        region=region,
        source=source,
        status=status,
        table_search=table_search,
        substance_filter=substance_filter,
        product_filter=product_filter,
        company_filter=company_filter,
        strength_filter=strength_filter,
        dosage_form_filter=dosage_form_filter,
        pack_size_filter=pack_size_filter,
        atc_code_filter=atc_code_filter,
        therapeutic_category_filter=therapeutic_category_filter,
        ma_holder_filter=ma_holder_filter,
        manufacturer_name_filter=manufacturer_name_filter,
        manufacturer_country_filter=manufacturer_country_filter,
        registration_number_filter=registration_number_filter,
        registration_date_filter=registration_date_filter,
        sort_by=sort_by,
        sort_dir=sort_dir,
        live_timeout=live_timeout,
        live_sources=live_sources,
        include_lookup_rows=True,
        force_live=refresh,
        freshness_report=freshness,
    )
    SEARCH_RESULT_CACHE[
        result_cache_key(
            substance,
            effective_live,
            selected_sources,
            country=country,
            region=region,
            source=source,
            status=status,
            table_search=table_search,
            substance_filter=substance_filter,
            product_filter=product_filter,
            company_filter=company_filter,
            strength_filter=strength_filter,
            dosage_form_filter=dosage_form_filter,
            pack_size_filter=pack_size_filter,
            atc_code_filter=atc_code_filter,
            therapeutic_category_filter=therapeutic_category_filter,
            ma_holder_filter=ma_holder_filter,
            manufacturer_name_filter=manufacturer_name_filter,
            manufacturer_country_filter=manufacturer_country_filter,
            registration_number_filter=registration_number_filter,
            registration_date_filter=registration_date_filter,
            sort_by=sort_by,
            sort_dir=sort_dir,
        )
    ] = [dict(row) for row in rows]
    page = max(page, 1)
    page_size = min(max(page_size, 10), 200)
    total_pages = max(1, ceil(len(rows) / page_size))
    page = min(page, total_pages)
    start = (page - 1) * page_size
    visible_rows = rows[start : start + page_size]
    if effective_live and visible_rows:
        visible_rows = enriched_cached_results(visible_rows)

    def is_manual_registry_row(row):
        return row.get("connector_mode") == "manual_registry"

    product_rows = [row for row in rows if not is_manual_registry_row(row)]
    registry_rows = [row for row in rows if is_manual_registry_row(row)]
    product_all_rows = [row for row in all_rows if not is_manual_registry_row(row)]
    registry_all_rows = [row for row in all_rows if is_manual_registry_row(row)]

    source_counts = {}
    country_counts = {}
    for row in rows:
        source_counts[row.get("source") or "Unknown"] = source_counts.get(row.get("source") or "Unknown", 0) + 1
        country_counts[row.get("country") or "Unknown"] = country_counts.get(row.get("country") or "Unknown", 0) + 1

    common_query = [("substance", substance), ("live", str(live).lower())]
    common_query.extend(("sources", selected_source) for selected_source in selected_sources)
    common_query.extend(
        (key, value)
        for key, value in [
            ("country", country),
            ("region", region),
            ("source", source),
            ("status", status),
            ("table_search", table_search),
            ("substance_filter", substance_filter),
            ("product_filter", product_filter),
            ("company_filter", company_filter),
            ("strength_filter", strength_filter),
            ("dosage_form_filter", dosage_form_filter),
            ("pack_size_filter", pack_size_filter),
            ("atc_code_filter", atc_code_filter),
            ("therapeutic_category_filter", therapeutic_category_filter),
            ("ma_holder_filter", ma_holder_filter),
            ("manufacturer_name_filter", manufacturer_name_filter),
            ("manufacturer_country_filter", manufacturer_country_filter),
            ("registration_number_filter", registration_number_filter),
            ("registration_date_filter", registration_date_filter),
            ("sort_by", sort_by),
            ("sort_dir", sort_dir),
            ("search_mode", normalized_search_mode),
        ]
        if value
    )

    export_query = urlencode(
        [("sources", selected_source) for selected_source in selected_sources]
        + [
            (key, value)
            for key, value in [
                ("live", str(live).lower()),
                ("country", country),
                ("region", region),
                ("source", source),
                ("status", status),
                ("table_search", table_search),
                ("substance_filter", substance_filter),
                ("product_filter", product_filter),
                ("company_filter", company_filter),
                ("strength_filter", strength_filter),
                ("dosage_form_filter", dosage_form_filter),
                ("pack_size_filter", pack_size_filter),
                ("atc_code_filter", atc_code_filter),
                ("therapeutic_category_filter", therapeutic_category_filter),
                ("ma_holder_filter", ma_holder_filter),
                ("manufacturer_name_filter", manufacturer_name_filter),
                ("manufacturer_country_filter", manufacturer_country_filter),
                ("registration_number_filter", registration_number_filter),
                ("registration_date_filter", registration_date_filter),
                ("sort_by", sort_by),
                ("sort_dir", sort_dir),
                ("search_mode", normalized_search_mode),
                ("page", str(page)),
                ("page_size", str(page_size)),
            ]
            if value
        ]
    )
    export_href = f"/export/{quote(substance)}"
    if export_query:
        export_href = f"{export_href}?{export_query}"
    export_all_query = urlencode(
        [("sources", selected_source) for selected_source in selected_sources]
        + [
            (key, value)
            for key, value in [
                ("live", str(live).lower()),
                ("country", country),
                ("region", region),
                ("source", source),
                ("status", status),
                ("table_search", table_search),
                ("substance_filter", substance_filter),
                ("product_filter", product_filter),
                ("company_filter", company_filter),
                ("strength_filter", strength_filter),
                ("dosage_form_filter", dosage_form_filter),
                ("pack_size_filter", pack_size_filter),
                ("atc_code_filter", atc_code_filter),
                ("therapeutic_category_filter", therapeutic_category_filter),
                ("ma_holder_filter", ma_holder_filter),
                ("manufacturer_name_filter", manufacturer_name_filter),
                ("manufacturer_country_filter", manufacturer_country_filter),
                ("registration_number_filter", registration_number_filter),
                ("registration_date_filter", registration_date_filter),
                ("sort_by", sort_by),
                ("sort_dir", sort_dir),
                ("search_mode", normalized_search_mode),
                ("export_all", "true"),
            ]
            if value
        ]
    )
    export_all_href = f"/export/{quote(substance)}"
    if export_all_query:
        export_all_href = f"{export_all_href}?{export_all_query}"

    def page_href(page_number):
        query = list(common_query)
        query.extend([("page", str(page_number)), ("page_size", str(page_size))])
        return f"/search_page?{urlencode(query)}"

    def sort_href(column):
        next_dir = "desc" if sort_by == column and sort_dir == "asc" else "asc"
        query = list(common_query)
        query.extend(
            [
                ("page", "1"),
                ("page_size", str(page_size)),
                ("sort_by", column),
                ("sort_dir", next_dir),
            ]
        )
        return f"/search_page?{urlencode(query)}"

    def sort_label(column, label):
        marker = ""
        if sort_by == column:
            marker = " v" if sort_dir == "asc" else " ^"
        return f'<a class="link-light" href="{h(sort_href(column))}">{h(label)}{marker}</a>'

    visible_product_rows = [row for row in visible_rows if not is_manual_registry_row(row)]
    visible_registry_rows = [row for row in visible_rows if is_manual_registry_row(row)]

    body_rows = []
    for row in visible_product_rows:
        display_row = formatted_result_row(row, searched_substance=substance)
        view_only_badge = (
            f' <span class="badge text-bg-warning" title="{h(str(row.get("view_only_reason") or ""))}">'
            f'view only</span>'
            if row.get("view_only")
            else ""
        )
        product_link = link_or_unavailable(display_row["product_details_url"], "Open Product")
        smpc_link = document_cell(row, display_row, "smpc_url", "SmPC")
        pil_link = document_cell(row, display_row, "pil_url", "PIL")
        assessment_link = document_cell(row, display_row, "assessment_report_url", "Assessment")
        body_rows.append(
            f"""
            <tr class="result-row">
                <td>{h(display_row["molecule"])}</td>
                <td>{h(display_row["product"])}</td>
                <td>{h(display_row["company"])}</td>
                <td>{h(display_row["country"])}</td>
                <td>{h(display_row["region"])}</td>
                <td>{h(display_row["registration_status"])}</td>
                <td>{h(display_row["source"])}{view_only_badge}</td>
                <td>{product_link}</td>
                <td>{h(display_row["strength"])}</td>
                <td>{h(display_row["dosage_form"])}</td>
                <td>{h(display_row["pack_size"])}</td>
                <td>{h(display_row["atc_code"])}</td>
                <td>{h(display_row["therapeutic_category"])}</td>
                <td>{h(display_row["ma_holder"])}</td>
                <td>{h(display_row["applicant_sponsor"])}</td>
                <td>{h(display_row["manufacturer_name"])}</td>
                <td>{h(display_row["manufacturer_country"])}</td>
                <td>{h(display_row["manufacturer_address"])}</td>
                <td>{h(display_row["manufacturer_role"])}</td>
                <td>{h(display_row["verification_status"])}</td>
                <td>{h(display_row["registration_status"])}</td>
                <td>{h(display_row["registration_number"])}</td>
                <td>{h(display_row["registration_date"])}</td>
                <td>{smpc_link}</td>
                <td>{pil_link}</td>
                <td>{assessment_link}</td>
            </tr>
            """
        )
    if not body_rows:
        body_rows.append(
            """
            <tr>
                <td colspan="26" class="text-center text-muted py-4">
                    No direct product records on this page. Use the official source links above for manual verification.
                </td>
            </tr>
            """
        )

    registry_link_rows = []
    for row in visible_registry_rows:
        display_row = formatted_result_row(row, searched_substance=substance)
        registry_link_rows.append(
            f"""
            <tr>
                <td>{h(display_row["country"])}</td>
                <td>{h(display_row["source"])}</td>
                <td>{h(display_row["registration_status"])}</td>
                <td>{link_or_unavailable(display_row["product_details_url"], "Open Official Registry")}</td>
            </tr>
            """
        )
    registry_links_section = ""
    if registry_link_rows:
        registry_links_section = f"""
        <div class="alert alert-info border mt-3">
            <strong>Official source links:</strong> Product data was not extracted from these countries yet. Use these regulator links for manual verification.
            <div class="table-responsive mt-2">
                <table class="table table-sm table-bordered bg-white mb-0">
                    <thead>
                        <tr>
                            <th>Country</th>
                            <th>Source</th>
                            <th>Status</th>
                            <th>Official Link</th>
                        </tr>
                    </thead>
                    <tbody>{"".join(registry_link_rows)}</tbody>
                </table>
            </div>
        </div>
        """

    previous_button = (
        f'<a class="btn btn-outline-primary" href="{h(page_href(page - 1))}">Previous</a>'
        if page > 1
        else '<button class="btn btn-outline-secondary" disabled>Previous</button>'
    )
    next_button = (
        f'<a class="btn btn-outline-primary" href="{h(page_href(page + 1))}">Next</a>'
        if page < total_pages
        else '<button class="btn btn-outline-secondary" disabled>Next</button>'
    )
    source_summary = ", ".join(
        f"{h(source)}: {count}" for source, count in sorted(source_counts.items())
    )
    country_summary = ", ".join(
        f"{h(country)}: {count}" for country, count in sorted(country_counts.items())
    )
    country_options = option_tags(
        merged_options(unique_values(all_rows, "country"), COUNTRY_OPTIONS),
        country,
        "All countries",
    )
    region_options = option_tags(
        merged_options(unique_values(all_rows, "region"), REGION_OPTIONS),
        region,
        "All regions",
    )
    source_options = option_tags(unique_values(all_rows, "source"), source, "All sources")
    formatted_all_rows = [formatted_result_row(row, searched_substance=substance) for row in all_rows]
    status_options = option_tags(
        unique_values(formatted_all_rows, "registration_status"),
        status,
        "All statuses",
    )
    source_inputs = "".join(
        f'<input type="hidden" name="sources" value="{h(selected_source)}">'
        for selected_source in selected_sources
    )
    active_filter_parts = []
    for label, value in [
        ("Country", country),
        ("Region", region),
        ("Source", source),
        ("Status", status),
        ("Substance", substance_filter),
        ("Product", product_filter),
        ("Company", company_filter),
        ("Strength", strength_filter),
        ("Dosage Form", dosage_form_filter),
        ("Pack Size", pack_size_filter),
        ("ATC Code", atc_code_filter),
        ("Therapeutic Category", therapeutic_category_filter),
        ("MA Holder", ma_holder_filter),
        ("Manufacturer Name", manufacturer_name_filter),
        ("Manufacturer Country", manufacturer_country_filter),
        ("Registration Number", registration_number_filter),
        ("Registration Date", registration_date_filter),
    ]:
        if value:
            active_filter_parts.append(f"{h(label)}: <strong>{h(value)}</strong>")
    active_filter_summary = (
        " | ".join(active_filter_parts)
        if active_filter_parts
        else "No column filters active"
    )
    if product_rows:
        result_alert_class = "success"
        result_alert_title = "Results found"
        result_alert_detail = (
            f"Showing {len(product_rows)} product records"
            f"{f' and {len(registry_rows)} official registry links' if registry_rows else ''} "
            f"from {len(product_all_rows)} selected-source product records."
        )
    elif registry_rows:
        result_alert_class = "info"
        result_alert_title = "Official source links"
        result_alert_detail = (
            f"No direct product records were extracted. Showing {len(registry_rows)} official regulator "
            f"links at the top for manual verification."
        )
    else:
        result_alert_class = "warning"
        result_alert_title = "No results found"
        result_alert_detail = (
            f"No records matched the active substance and filters. "
            f"The selected sources returned {len(all_rows)} records before column filters."
        )
    no_results_help = ""
    if not rows:
        no_results_help = (
            '<div class="alert alert-light border">'
            '<strong>Try this:</strong> clear Country/Region/Source/Status filters, '
            'check spelling of the active substance, or search with all sources selected.'
            '</div>'
        )
    eu_countries_in_results = {
        row.get("country")
        for row in rows
        if row.get("region") == "EU" and row.get("country") in EU_COUNTRIES
    }
    capped_note = capped_sources_note(all_rows)
    # A regulator whose terms let its data be read but not kept or passed on.
    # The rows are here to look at; they are not stored and not exported, and
    # the page has to say so rather than let a reader assume otherwise.
    view_only_sources = sorted({
        str(row.get("source") or "") for row in all_rows if row.get("view_only")
    })
    view_only_note = ""
    if view_only_sources:
        notice = next(
            (str(row.get("view_only_reason") or "") for row in all_rows if row.get("view_only_reason")),
            "",
        )
        copyright_line = ", ".join(sorted({
            str(row.get("copyright") or "") for row in all_rows if row.get("view_only") and row.get("copyright")
        }))
        view_only_note = (
            f'<p class="alert alert-warning py-2 mb-2"><strong>View only &mdash; '
            f'{h(", ".join(view_only_sources))}:</strong> {h(notice)}'
            + (f' <span class="text-muted">{h(copyright_line)}</span>' if copyright_line else "")
            + "</p>"
        )
    # Sources the store answered for, because they were checked for this
    # molecule recently and every row they found is stored. The reader can
    # still ask them all again.
    from_store = [item for item in freshness if not item.ask_live]
    store_note = ""
    if from_store:
        oldest = min(item.checked_at for item in from_store)[:10]
        refresh_href = "/search_page?" + urlencode(common_query + [("refresh", "true")])
        store_note = (
            '<p class="alert alert-light border py-2 mb-2 small">'
            f'<strong>{len(from_store)} source{"s" if len(from_store) != 1 else ""} answered from the store</strong>, '
            f'each checked for this molecule since {h(oldest)}: '
            f'{h(", ".join(sorted(item.source for item in from_store)))}. '
            f'<a href="{h(refresh_href)}">Check them live now</a> (slower).'
            "</p>"
        )
    eu_coverage_note = ""
    if region == "EU":
        note_class = "success" if len(eu_countries_in_results) == len(EU_COUNTRIES) else "warning"
        eu_coverage_note = (
            f'<p class="alert alert-{note_class} py-2">'
            f'EU country coverage: <strong>{len(eu_countries_in_results)}/{len(EU_COUNTRIES)}</strong> '
            f'EU countries shown for this substance from the currently available EU sources.'
            f'</p>'
        )
    clear_href = "/search_page?" + urlencode(
        [("substance", substance), ("live", str(live).lower())]
        + [("sources", selected_source) for selected_source in selected_sources]
        + [("page_size", str(page_size)), ("search_mode", normalized_search_mode)]
    )
    has_active_filters = any(
        [
            country,
            region,
            source,
            status,
            table_search,
            substance_filter,
            product_filter,
            company_filter,
            strength_filter,
            dosage_form_filter,
            pack_size_filter,
            atc_code_filter,
            therapeutic_category_filter,
            ma_holder_filter,
            manufacturer_name_filter,
            manufacturer_country_filter,
            registration_number_filter,
            registration_date_filter,
        ]
    )
    show_all_button = (
        f'<a class="btn btn-warning" href="{h(clear_href)}">Show all results</a>'
        if has_active_filters
        else ""
    )

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>PharmaSearch Results</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
        <style>
            .results-table {{
                font-size: 0.84rem;
                min-width: 3350px;
                table-layout: auto;
            }}
            .results-table th,
            .results-table td {{
                vertical-align: top;
                white-space: normal;
                overflow-wrap: anywhere;
            }}
            .results-table th {{
                min-width: 130px;
            }}
            .results-table th:nth-child(2),
            .results-table td:nth-child(2) {{
                min-width: 280px;
            }}
            .results-table th:nth-child(8),
            .results-table td:nth-child(8),
            .results-table th:nth-child(18),
            .results-table td:nth-child(18),
            .results-table th:nth-child(19),
            .results-table td:nth-child(19),
            .results-table th:nth-child(20),
            .results-table td:nth-child(20) {{
                min-width: 230px;
            }}
            .results-table th:nth-child(12),
            .results-table td:nth-child(12),
            .results-table th:nth-child(13),
            .results-table td:nth-child(13) {{
                min-width: 220px;
            }}
            /* The results scroll inside their own box, with the header
               pinned to its top: scrolled across to the last columns, or
               down past the first rows, the column titles stay in view. */
            .results-scroll {{
                overflow: auto;
                max-height: calc(100vh - 120px);
                padding-bottom: 0.5rem;
                border: 1px solid #dee2e6;
                border-radius: 6px;
            }}
            .results-table thead {{
                position: sticky;
                top: 0;
                z-index: 3;
            }}
            .results-table thead th {{
                box-shadow: inset 0 -1px 0 #adb5bd;
            }}
            .results-table thead tr.table-dark th {{
                background: #212529;
                color: #fff;
            }}
            .results-table thead tr.table-secondary th {{
                background: #e2e3e5;
            }}
        </style>
    </head>
    <body>
    <div class="container-fluid px-4 mt-4">
        <h2>PharmaSearch Results</h2>
        <p>Active Substance: <strong>{h(substance)}</strong></p>
        <div class="alert alert-{result_alert_class}">
            <strong>{result_alert_title}:</strong> {h(result_alert_detail)}
        </div>
        {no_results_help}
        <p class="small text-muted mb-1">By source: {source_summary}</p>
        <p class="small text-muted">By country: {country_summary}</p>
        <p class="small">Active filters: {active_filter_summary}</p>
        {capped_note}
        {view_only_note}
        {store_note}
        {eu_coverage_note}
        {registry_links_section}
        <div class="d-flex justify-content-between align-items-center mb-3">
            <div>Page <strong>{page}</strong> of <strong>{total_pages}</strong> ({start + 1 if rows else 0}-{min(start + page_size, len(rows))} shown)</div>
            <div class="d-flex gap-2">
                {show_all_button}
                <a class="btn btn-secondary" href="/">Back</a>
                <a class="btn btn-outline-success" href="{h(export_all_href)}">Export All Excel</a>
                <a class="btn btn-success" href="{h(export_href)}">Export Current Page Excel</a>
                <div class="btn-group">{previous_button}{next_button}</div>
            </div>
        </div>
        <form id="result-filter-form" method="get" action="/search_page">
            <input type="hidden" name="substance" value="{h(substance)}">
            <input type="hidden" name="live" value="{h(str(live).lower())}">
            <input type="hidden" name="page" value="1">
            <input type="hidden" name="page_size" value="{page_size}">
            <input type="hidden" name="table_search" value="{h(table_search)}">
            <input type="hidden" name="sort_by" value="{h(sort_by)}">
            <input type="hidden" name="sort_dir" value="{h(sort_dir)}">
            <input type="hidden" name="search_mode" value="{h(normalized_search_mode)}">
            {source_inputs}
        </form>
        <div class="d-flex justify-content-between align-items-center mb-2">
            <small id="page-filter-status" class="text-muted">Header filters apply to the displayed results.</small>
            <div class="d-flex gap-2">
                <a class="btn btn-sm btn-outline-secondary" href="{h(clear_href)}">Clear filters</a>
                <button form="result-filter-form" class="btn btn-sm btn-primary">Apply filters</button>
            </div>
        </div>
        <div class="results-scroll">
        <table class="table table-striped table-bordered align-middle results-table">
            <thead>
                <tr class="table-dark">
                    <th>{sort_label("substance", "Molecule")}</th>
                    <th>{sort_label("product", "Product Name")}</th>
                    <th>{sort_label("company", "Company")}</th>
                    <th>{sort_label("country", "Country")}</th>
                    <th>{sort_label("region", "Region")}</th>
                    <th>{sort_label("status", "Status")}</th>
                    <th>{sort_label("source", "Source")}</th>
                    <th>Product Details</th>
                    <th>{sort_label("strength", "Strength")}</th>
                    <th>{sort_label("dosage_form", "Dosage Form")}</th>
                    <th>{sort_label("pack_size", "Pack Size")}</th>
                    <th>{sort_label("atc_code", "ATC Code")}</th>
                    <th>{sort_label("therapeutic_category", "Therapeutic Category")}</th>
                    <th>{sort_label("ma_holder", "MA Holder Name")}</th>
                    <th>Applicant / Sponsor</th>
                    <th>{sort_label("manufacturer_name", "Manufacturer Name")}</th>
                    <th>{sort_label("manufacturer_country", "Manufacturer Country")}</th>
                    <th>Manufacturer Address / Site</th>
                    <th>Manufacturer Role</th>
                    <th>Verification</th>
                    <th>Registration Status</th>
                    <th>{sort_label("registration_number", "Registration Number")}</th>
                    <th>{sort_label("registration_date", "Registration Date")}</th>
                    <th>SMPC URL</th>
                    <th>PIL URL</th>
                    <th>Assessment Report URL</th>
                </tr>
                <tr class="table-secondary">
                    <th><input form="result-filter-form" class="form-control form-control-sm" name="substance_filter" value="{h(substance_filter)}" placeholder="Filter"></th>
                    <th><input form="result-filter-form" class="form-control form-control-sm" name="product_filter" value="{h(product_filter)}" placeholder="Filter"></th>
                    <th><input form="result-filter-form" class="form-control form-control-sm" name="company_filter" value="{h(company_filter)}" placeholder="Filter"></th>
                    <th><select id="country-filter" form="result-filter-form" class="form-select form-select-sm" name="country">{country_options}</select></th>
                    <th><select id="region-filter" form="result-filter-form" class="form-select form-select-sm" name="region">{region_options}</select></th>
                    <th><select form="result-filter-form" class="form-select form-select-sm" name="status" onchange="this.form.submit()">{status_options}</select></th>
                    <th><select form="result-filter-form" class="form-select form-select-sm" name="source" onchange="this.form.submit()">{source_options}</select></th>
                    <th><input class="form-control form-control-sm page-column-filter" data-column="7" placeholder="Filter"></th>
                    <th><input form="result-filter-form" class="form-control form-control-sm" name="strength_filter" value="{h(strength_filter)}" placeholder="Filter"></th>
                    <th><input form="result-filter-form" class="form-control form-control-sm" name="dosage_form_filter" value="{h(dosage_form_filter)}" placeholder="Filter"></th>
                    <th><input form="result-filter-form" class="form-control form-control-sm" name="pack_size_filter" value="{h(pack_size_filter)}" placeholder="Filter"></th>
                    <th><input form="result-filter-form" class="form-control form-control-sm" name="atc_code_filter" value="{h(atc_code_filter)}" placeholder="Filter"></th>
                    <th><input form="result-filter-form" class="form-control form-control-sm" name="therapeutic_category_filter" value="{h(therapeutic_category_filter)}" placeholder="Filter"></th>
                    <th><input form="result-filter-form" class="form-control form-control-sm" name="ma_holder_filter" value="{h(ma_holder_filter)}" placeholder="Filter"></th>
                    <th><input class="form-control form-control-sm page-column-filter" data-column="14" placeholder="Filter"></th>
                    <th><input form="result-filter-form" class="form-control form-control-sm" name="manufacturer_name_filter" value="{h(manufacturer_name_filter)}" placeholder="Filter"></th>
                    <th><input form="result-filter-form" class="form-control form-control-sm" name="manufacturer_country_filter" value="{h(manufacturer_country_filter)}" placeholder="Filter"></th>
                    <th><input class="form-control form-control-sm page-column-filter" data-column="17" placeholder="Filter"></th>
                    <th><input class="form-control form-control-sm page-column-filter" data-column="18" placeholder="Filter"></th>
                    <th><input class="form-control form-control-sm page-column-filter" data-column="19" placeholder="Filter"></th>
                    <th><select form="result-filter-form" class="form-select form-select-sm" name="status">{status_options}</select></th>
                    <th><input form="result-filter-form" class="form-control form-control-sm" name="registration_number_filter" value="{h(registration_number_filter)}" placeholder="Filter"></th>
                    <th><input form="result-filter-form" class="form-control form-control-sm" name="registration_date_filter" value="{h(registration_date_filter)}" placeholder="Filter"></th>
                    <th><input class="form-control form-control-sm page-column-filter" data-column="23" placeholder="Filter"></th>
                    <th><input class="form-control form-control-sm page-column-filter" data-column="24" placeholder="Filter"></th>
                    <th><input class="form-control form-control-sm page-column-filter" data-column="25" placeholder="Filter"></th>
                </tr>
            </thead>
            <tbody>{"".join(body_rows)}</tbody>
        </table>
        </div>
        <div class="d-flex justify-content-between align-items-center mb-3">
            <div>Page <strong>{page}</strong> of <strong>{total_pages}</strong></div>
            <div class="btn-group">{previous_button}{next_button}</div>
        </div>
        <a class="btn btn-secondary" href="/">Back</a>
    </div>
    <script>
    const resultFilterForm = document.getElementById("result-filter-form");
    const countryFilter = document.getElementById("country-filter");
    const regionFilter = document.getElementById("region-filter");
    function submitFilters() {{
        resultFilterForm.submit();
    }}
    countryFilter.addEventListener("change", function () {{
        if (countryFilter.value) {{
            regionFilter.value = "";
        }}
        submitFilters();
    }});
    regionFilter.addEventListener("change", function () {{
        if (regionFilter.value) {{
            countryFilter.value = "";
        }}
        submitFilters();
    }});
    document.querySelectorAll('select[form="result-filter-form"]:not(#country-filter):not(#region-filter)').forEach(function (control) {{
        control.addEventListener("change", submitFilters);
    }});
    const pageColumnFilters = document.querySelectorAll(".page-column-filter");
    const resultRows = document.querySelectorAll(".result-row");
    const pageFilterStatus = document.getElementById("page-filter-status");
    function applyPageColumnFilters() {{
        let shown = 0;
        resultRows.forEach(function (row) {{
            const matches = Array.from(pageColumnFilters).every(function (control) {{
                const query = control.value.trim().toLowerCase();
                if (!query) return true;
                const cell = row.cells[Number(control.dataset.column)];
                return cell && cell.textContent.toLowerCase().includes(query);
            }});
            row.hidden = !matches;
            if (matches) shown += 1;
        }});
        pageFilterStatus.textContent = `Showing ${{shown}} of ${{resultRows.length}} displayed rows after page filters.`;
    }}
    pageColumnFilters.forEach(function (control) {{
        control.addEventListener("input", applyPageColumnFilters);
    }});
    </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


@app.get("/export/{substance:path}")
def export_live(
    substance: str,
    live: bool = True,
    sources: list[str] = Query(default=DEFAULT_SOURCES),
    page: int = 1,
    page_size: int = 50,
    country: str = "",
    region: str = "",
    source: str = "",
    status: str = "",
    table_search: str = "",
    substance_filter: str = "",
    product_filter: str = "",
    company_filter: str = "",
    strength_filter: str = "",
    dosage_form_filter: str = "",
    pack_size_filter: str = "",
    atc_code_filter: str = "",
    therapeutic_category_filter: str = "",
    ma_holder_filter: str = "",
    manufacturer_name_filter: str = "",
    manufacturer_country_filter: str = "",
    registration_number_filter: str = "",
    registration_date_filter: str = "",
    sort_by: str = "",
    sort_dir: str = "asc",
    export_all: bool = False,
):
    status = english_text(status)
    cache_key = result_cache_key(
        substance,
        live,
        sources,
        country=country,
        region=region,
        source=source,
        status=status,
        table_search=table_search,
        substance_filter=substance_filter,
        product_filter=product_filter,
        company_filter=company_filter,
        strength_filter=strength_filter,
        dosage_form_filter=dosage_form_filter,
        pack_size_filter=pack_size_filter,
        atc_code_filter=atc_code_filter,
        therapeutic_category_filter=therapeutic_category_filter,
        ma_holder_filter=ma_holder_filter,
        manufacturer_name_filter=manufacturer_name_filter,
        manufacturer_country_filter=manufacturer_country_filter,
        registration_number_filter=registration_number_filter,
        registration_date_filter=registration_date_filter,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )
    results = SEARCH_RESULT_CACHE.get(cache_key)
    if results is None:
        results, _, _ = filtered_search_results(
            substance,
            live=live,
            sources=sources,
            country=country,
            region=region,
            source=source,
            status=status,
            table_search=table_search,
            substance_filter=substance_filter,
            product_filter=product_filter,
            company_filter=company_filter,
            strength_filter=strength_filter,
            dosage_form_filter=dosage_form_filter,
            pack_size_filter=pack_size_filter,
            atc_code_filter=atc_code_filter,
            therapeutic_category_filter=therapeutic_category_filter,
            ma_holder_filter=ma_holder_filter,
            manufacturer_name_filter=manufacturer_name_filter,
            manufacturer_country_filter=manufacturer_country_filter,
            registration_number_filter=registration_number_filter,
            registration_date_filter=registration_date_filter,
            sort_by=sort_by,
            sort_dir=sort_dir,
            live_timeout=COMPLETE_SEARCH_TIMEOUT_SECONDS,
            include_lookup_rows=False,
        )
    if not export_all:
        page = max(page, 1)
        page_size = min(max(page_size, 10), 200)
        start = (page - 1) * page_size
        results = results[start : start + page_size]
    if not export_all:
        results = enriched_cached_results(results)
    path = write_excel_export(substance, results, deep=False)
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=Path(path).name,
    )


@app.get("/deep_export/{substance:path}")
def deep_export_live(
    substance: str,
    live: bool = True,
    sources: list[str] = Query(default=DEFAULT_SOURCES),
    country: str = "",
    region: str = "",
    source: str = "",
    status: str = "",
    table_search: str = "",
    substance_filter: str = "",
    product_filter: str = "",
    company_filter: str = "",
    strength_filter: str = "",
    dosage_form_filter: str = "",
    pack_size_filter: str = "",
    atc_code_filter: str = "",
    therapeutic_category_filter: str = "",
    ma_holder_filter: str = "",
    manufacturer_name_filter: str = "",
    manufacturer_country_filter: str = "",
    registration_number_filter: str = "",
    registration_date_filter: str = "",
    sort_by: str = "",
    sort_dir: str = "asc",
):
    query = urlencode(
        [("sources", selected_source) for selected_source in parse_sources(sources)]
        + [
            (key, value)
            for key, value in [
                ("live", str(live).lower()),
                ("country", country),
                ("region", region),
                ("source", source),
                ("status", status),
                ("table_search", table_search),
                ("substance_filter", substance_filter),
                ("product_filter", product_filter),
                ("company_filter", company_filter),
                ("strength_filter", strength_filter),
                ("dosage_form_filter", dosage_form_filter),
                ("pack_size_filter", pack_size_filter),
                ("atc_code_filter", atc_code_filter),
                ("therapeutic_category_filter", therapeutic_category_filter),
                ("ma_holder_filter", ma_holder_filter),
                ("manufacturer_name_filter", manufacturer_name_filter),
                ("manufacturer_country_filter", manufacturer_country_filter),
                ("registration_number_filter", registration_number_filter),
                ("registration_date_filter", registration_date_filter),
                ("sort_by", sort_by),
                ("sort_dir", sort_dir),
            ]
            if value
        ]
    )
    export_url = f"/export/{quote(substance)}"
    if query:
        export_url = f"{export_url}?{query}"
    return RedirectResponse(export_url, status_code=307)


@app.get("/products", response_class=HTMLResponse)
def products():
    rows = list_product_details()
    body_rows = []
    for row in rows:
        body_rows.append(
            f"""
            <tr>
                <td>{h(row.get("product", ""))}</td>
                <td>{h(row.get("substance", ""))}</td>
                <td>{h(row.get("company", ""))}</td>
                <td>{h(row.get("country", ""))}</td>
                <td>{h(row.get("region", ""))}</td>
                <td>{h(row.get("status", ""))}</td>
                <td>{h(row.get("source", ""))}</td>
                <td>{h(row.get("registration_date", ""))}</td>
            </tr>
            """
        )
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Products</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    </head>
    <body>
    <div class="container mt-5">
        <h2>Saved Products</h2>
        <p class="alert alert-info">Total Products: <strong>{len(rows)}</strong></p>
        <table class="table table-striped table-bordered">
            <thead class="table-dark">
                <tr>
                    <th>Product</th>
                    <th>Substance</th>
                    <th>Company</th>
                    <th>Country</th>
                    <th>Region</th>
                    <th>Status</th>
                    <th>Source</th>
                    <th>Registration Date</th>
                </tr>
            </thead>
            <tbody>{"".join(body_rows)}</tbody>
        </table>
    </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


CATALOGUE_COLUMNS = (
    "Molecule",
    "Product Name",
    "Company",
    "Country",
    "Region",
    "Status",
    "Source",
    "Product Details",
    "Strength",
    "Dosage Form",
    "Pack Size",
    "ATC Code",
    "Therapeutic Category",
    "MA Holder Name",
    "Manufacturer Name",
    "Manufacturer Country",
    "Registration Status",
    "Registration Number",
    "Registration Date",
    "SMPC URL",
    "PIL URL",
    "Assessment Report URL",
)

# The harvest stores a handoff row for registries whose live endpoint returns
# nothing. They carry no product data, so the catalogue hides them by default.
REGISTRY_HANDOFF_PATTERN = re.compile(r"official .* registry search", flags=re.IGNORECASE)


def _is_registry_handoff(row: dict[str, Any]) -> bool:
    return bool(REGISTRY_HANDOFF_PATTERN.search(str(row.get("product") or "")))


@app.get("/catalogue", response_class=HTMLResponse)
def catalogue(
    page: int = 1,
    page_size: int = 50,
    substance: str = "",
    country: str = "",
    source: str = "",
    company: str = "",
    product: str = "",
    include_handoffs: bool = False,
):
    """Everything harvested, with no substance required.

    The search page answers "what exists for this molecule". This answers "what
    do we hold", which is the view a harvest produces and the one that shows
    coverage across every registry at once.
    """
    rows = list_product_details()
    if not include_handoffs:
        rows = [row for row in rows if not _is_registry_handoff(row)]

    filters = (
        ("substance", substance),
        ("country", country),
        ("source", source),
        ("company", company),
        ("product", product),
    )
    for field, needle in filters:
        if needle.strip():
            wanted = needle.strip().lower()
            rows = [row for row in rows if wanted in str(row.get(field) or "").lower()]

    total = len(rows)
    page_size = max(1, min(page_size, 500))
    pages = max(1, ceil(total / page_size))
    page = max(1, min(page, pages))
    visible = rows[(page - 1) * page_size : page * page_size]
    visible = prepared_cached_results(visible)

    by_source = Counter(str(row.get("source") or "") for row in rows)
    by_country = Counter(str(row.get("country") or "") for row in rows)

    body_rows = []
    for row in visible:
        display_row = formatted_result_row(row, searched_substance=row.get("substance", ""))
        body_rows.append(
            f"""
            <tr>
                <td>{h(display_row["molecule"])}</td>
                <td>{h(display_row["product"])}</td>
                <td>{h(display_row["company"])}</td>
                <td>{h(display_row["country"])}</td>
                <td>{h(display_row["region"])}</td>
                <td>{h(display_row["registration_status"])}</td>
                <td>{h(display_row["source"])}</td>
                <td>{link_or_unavailable(display_row["product_details_url"], "Open Product")}</td>
                <td>{h(display_row["strength"])}</td>
                <td>{h(display_row["dosage_form"])}</td>
                <td>{h(display_row["pack_size"])}</td>
                <td>{h(display_row["atc_code"])}</td>
                <td>{h(display_row["therapeutic_category"])}</td>
                <td>{h(display_row["ma_holder"])}</td>
                <td>{h(display_row["manufacturer_name"])}</td>
                <td>{h(display_row["manufacturer_country"])}</td>
                <td>{h(display_row["registration_status"])}</td>
                <td>{h(display_row["registration_number"])}</td>
                <td>{h(display_row["registration_date"])}</td>
                <td>{document_cell(row, display_row, "smpc_url", "SmPC")}</td>
                <td>{document_cell(row, display_row, "pil_url", "PIL")}</td>
                <td>{document_cell(row, display_row, "assessment_report_url", "Assessment")}</td>
            </tr>
            """
        )
    if not body_rows:
        body_rows.append(
            f'<tr><td colspan="{len(CATALOGUE_COLUMNS)}" class="text-center text-muted py-4">'
            "No harvested records match these filters.</td></tr>"
        )

    def page_href(number):
        query = [(field, value) for field, value in filters if value.strip()]
        query.extend([("page", str(number)), ("page_size", str(page_size))])
        if include_handoffs:
            query.append(("include_handoffs", "true"))
        return f"/catalogue?{urlencode(query)}"

    headers = "".join(f"<th>{h(label)}</th>" for label in CATALOGUE_COLUMNS)
    source_summary = ", ".join(f"{name}: {count}" for name, count in by_source.most_common())
    country_summary = ", ".join(f"{name}: {count}" for name, count in by_country.most_common(12))
    previous_link = (
        f'<a class="btn btn-outline-secondary" href="{h(page_href(page - 1))}">Previous</a>'
        if page > 1
        else '<span class="btn btn-outline-secondary disabled">Previous</span>'
    )
    next_link = (
        f'<a class="btn btn-outline-secondary" href="{h(page_href(page + 1))}">Next</a>'
        if page < pages
        else '<span class="btn btn-outline-secondary disabled">Next</span>'
    )

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>PharmaSearch Catalogue</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
        <style>
            .catalogue-scroll {{ overflow-x: auto; }}
            .catalogue-scroll table {{ min-width: 2400px; }}
        </style>
    </head>
    <body>
    <div class="container-fluid mt-4">
        <h2>Harvested Catalogue</h2>
        <p class="alert alert-info mb-2">
            <strong>{total}</strong> product records across <strong>{len(by_source)}</strong> sources
            and <strong>{len(by_country)}</strong> countries. No substance needed &mdash; this is
            everything the harvest has stored.
        </p>
        <p class="small text-muted">By source &mdash; {h(source_summary)}</p>
        <p class="small text-muted">By country &mdash; {h(country_summary)}</p>
        <form class="row g-2 mb-3" method="get" action="/catalogue">
            <div class="col"><input class="form-control" name="substance" value="{h(substance)}" placeholder="Molecule"></div>
            <div class="col"><input class="form-control" name="product" value="{h(product)}" placeholder="Product"></div>
            <div class="col"><input class="form-control" name="company" value="{h(company)}" placeholder="Company"></div>
            <div class="col"><input class="form-control" name="country" value="{h(country)}" placeholder="Country"></div>
            <div class="col"><input class="form-control" name="source" value="{h(source)}" placeholder="Source"></div>
            <div class="col"><input class="form-control" name="page_size" value="{page_size}" placeholder="Rows"></div>
            <div class="col-auto"><button class="btn btn-primary" type="submit">Filter</button></div>
            <div class="col-auto"><a class="btn btn-outline-secondary" href="/catalogue">Clear</a></div>
        </form>
        <div class="d-flex gap-2 align-items-center mb-2">
            {previous_link}{next_link}
            <span class="text-muted">Page {page} of {pages}</span>
            <a class="btn btn-outline-primary ms-auto" href="/">Back to search</a>
        </div>
        <div class="catalogue-scroll">
        <table class="table table-striped table-bordered align-middle">
            <thead class="table-dark"><tr>{headers}</tr></thead>
            <tbody>{"".join(body_rows)}</tbody>
        </table>
        </div>
    </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


@app.get("/reset_db")
def reset_db():
    # This used to clear the database on a bare GET, which any crawler or link
    # prefetcher could trigger. Clearing now lives behind the admin sign-in.
    return RedirectResponse("/admin", status_code=308)


@app.get("/connector_status")
def connector_status():
    return {"connectors": connector_status_rows()}


@app.get("/connector_health")
def connector_health():
    return {"connectors": connector_health_rows()}


@app.get("/ai_status")
def ai_status():
    return current_ai_status()


@app.get("/harvest_coverage")
def harvest_coverage_report():
    """What the source-wide harvest has stored so far, per source."""
    rows = harvest_coverage()
    return {
        "sources": rows,
        "rows": sum(row["rows"] for row in rows),
        "molecules_available": len(load_vocabulary()),
    }


def _connectors_page(request: Request, view: str) -> HTMLResponse:
    rows = connector_health_rows()
    user = read_session(request.cookies.get(SESSION_COOKIE_NAME))
    return templates.TemplateResponse(
        request=request,
        name="connectors.html",
        context={"rows": rows, "view": view, "user": user, "user_is_admin": is_admin(user)},
    )


@app.get("/connector_status_page", response_class=HTMLResponse)
def connector_status_page(request: Request):
    return _connectors_page(request, "status")


@app.get("/connector_health_page", response_class=HTMLResponse)
def connector_health_page(request: Request):
    return _connectors_page(request, "health")


@app.get("/crawl/{substance}")
def crawl_ema_search(substance: str):
    return run_ema_search(substance)


@app.get("/crawl_mhra")
def crawl_mhra(substance: str):
    url = f"https://products.mhra.gov.uk/search/?search={quote(substance)}&page=1"
    result = extract_mhra_product_page(url)
    save_product_detail(
        {
            "substance": result.get("active_substance", substance),
            "product": result.get("product_name", ""),
            "country": "United Kingdom",
            "status": "Authorised",
            "product_url": result.get("product_url", ""),
            "source": "MHRA",
        }
    )
    return result


@app.get("/mhra_full/{substance}")
def mhra_full(substance: str):
    return {"substance": substance, "results": run_mhra_search(substance)}


@app.get("/crawl_substance/{substance}")
def crawl_substance(substance: str):
    product_url = find_product_url(substance)
    if not product_url:
        return {"error": f"No product found for {substance}"}
    try:
        return save_ema_product_page(product_url)
    except (NotAnEmaPage, EmptyProductPage) as exc:
        return {"error": str(exc)}


class EmptyProductPage(ValueError):
    """Raised when an EMA page was read but named no product."""


def save_ema_product_page(url: str) -> dict[str, Any]:
    """Read one EMA product page and save it as an EMA record.

    Only pages on the EMA website are read (extract_product_page refuses the
    rest, before and after any redirect), and a page that yields no product
    name is not saved: a nameless row is not a registration, and it would
    still be stamped as a regulator record.
    """
    result = extract_product_page(url)
    if not str(result.get("product_name") or "").strip():
        raise EmptyProductPage(f"No product was found on {url}")
    saved = save_product_detail(
        {
            "substance": result.get("active_substance", ""),
            "product": result.get("product_name", ""),
            "company": result.get("mah", ""),
            "country": "European Union",
            "region": "EU",
            "status": result.get("status", ""),
            "product_url": result.get("product_url", url),
            "atc_code": result.get("atc_code", ""),
            "registration_date": result.get("authorisation_date", ""),
            "smpc_url": result.get("smpc_url", ""),
            "pil_url": result.get("pil_url", ""),
            "assessment_report_url": result.get("assessment_report_url", ""),
            "source": "EMA",
            "source_url": url,
        }
    )
    return {"message": "Product saved", "product": saved.get("product"), "data": result}


# POST, not GET: it writes to the store, and a GET can be set off by nothing
# more than a link or a browser prefetch while someone is signed in.
@app.post("/crawl_url")
def crawl_url(url: str):
    try:
        return save_ema_product_page(url)
    except NotAnEmaPage:
        return JSONResponse(
            {"detail": "Only https pages on ema.europa.eu can be read."}, status_code=400
        )
    except EmptyProductPage as exc:
        return JSONResponse({"detail": str(exc)}, status_code=422)


@app.get("/crawl_all_products/{substance}")
def crawl_all_products(substance: str):
    saved_products = []
    for product_data in run_ema_search(substance):
        try:
            result = save_ema_product_page(product_data["url"])
            saved_products.append(result["product"])
        except Exception:
            logger.exception("EMA product crawl failed")
    return {
        "substance": substance,
        "products_saved": len(saved_products),
        "products": saved_products,
    }
