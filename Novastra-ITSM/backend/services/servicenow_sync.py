# ---------------------------------------------------------------------------
# Author: Vishnuu A
# Scope: ServiceNow one-time ingestion service.
# Date: 2026-02-27
# ---------------------------------------------------------------------------
"""
ServiceNow one-time ingestion service.
Fetches incidents in pages, converts them to RAG documents, and indexes into PostgreSQL pgvector.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from langchain_core.documents import Document

import backend.config as cfg
from backend.rag.vectorstore import index_documents
from backend.services.embedding_worker import index_incidents_to_qdrant
from backend.services.embedding_worker_lancedb import index_incidents_to_lancedb
from backend.services.operational_ingestion import (
    count_incidents_by_source,
    finalize_sync_log,
    persist_incidents,
    start_sync_log,
)
from backend.services.sync_status_store import record_sync

logger = logging.getLogger(__name__)

_INCIDENT_FIELDS = [
    "sys_id",
    "number",
    "short_description",
    "description",
    "category",
    "subcategory",
    "priority",
    "state",
    "assignment_group",
    "assigned_to",
    "opened_at",
    "resolved_at",
    "closed_at",
    "cmdb_ci",
    "business_service",
    "work_notes",
    "close_notes",
    "sys_updated_on",
]


def _servicenow_fields() -> list[str]:
    if cfg.SERVICENOW_TABLE.startswith("u_"):
        return ["sys_id", "sys_updated_on", *[
            f"u_{field}" for field in _INCIDENT_FIELDS
            if field not in {"sys_id", "sys_updated_on"}
        ]]
    return _INCIDENT_FIELDS


def _normalize_custom_table_record(record: dict) -> dict:
    if not cfg.SERVICENOW_TABLE.startswith("u_"):
        return record
    normalized = {"sys_id": record.get("sys_id"), "sys_updated_on": record.get("sys_updated_on")}
    for field in _INCIDENT_FIELDS:
        if field not in normalized:
            normalized[field] = record.get(f"u_{field}", "")
    return normalized


# Function: _safe_text
def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in {"none", "null", "nan"}:
        return ""
    return text


# Function: _clip
def _clip(value: str, max_chars: int) -> str:
    text = _safe_text(value)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + " ...[truncated]"


# Function: _build_incident_doc
def _build_incident_doc(record: dict, source_name: str) -> Document:
    number = _safe_text(record.get("number")) or "UNKNOWN"
    short_description = _clip(record.get("short_description"), 600)
    description = _clip(record.get("description"), 4000)
    category = _safe_text(record.get("category"))
    subcategory = _safe_text(record.get("subcategory"))
    priority = _safe_text(record.get("priority"))
    state = _safe_text(record.get("state"))
    assignment_group = _safe_text(record.get("assignment_group"))
    assigned_to = _safe_text(record.get("assigned_to"))
    opened_at = _safe_text(record.get("opened_at"))
    resolved_at = _safe_text(record.get("resolved_at"))
    closed_at = _safe_text(record.get("closed_at"))
    cmdb_ci = _safe_text(record.get("cmdb_ci"))
    business_service = _safe_text(record.get("business_service"))
    work_notes = _clip(record.get("work_notes"), 6000)
    close_notes = _clip(record.get("close_notes"), 3000)

    content = (
        f"Incident Number: {number}\n"
        f"Short Description: {short_description}\n"
        f"Description: {description}\n"
        f"Category: {category}\n"
        f"Subcategory: {subcategory}\n"
        f"Priority: {priority}\n"
        f"State: {state}\n"
        f"Assignment Group: {assignment_group}\n"
        f"Assigned To: {assigned_to}\n"
        f"CMDB CI: {cmdb_ci}\n"
        f"Business Service: {business_service}\n"
        f"Opened At: {opened_at}\n"
        f"Resolved At: {resolved_at}\n"
        f"Closed At: {closed_at}\n"
        f"Work Notes: {work_notes}\n"
        f"Close Notes: {close_notes}\n"
    )

    return Document(
        page_content=content,
        metadata={
            "source": source_name,
            "type": "servicenow_incident",
            "incident_number": number,
            "category": category,
            "priority": priority,
            "state": state,
            # Allow splitter chunking for large ticket payloads to keep embed input
            # under model context limits.
            "atomic": False,
        },
    )


# A ServiceNow PDI (personal developer instance) hibernates after a period of
# inactivity and can take several seconds to wake back up — the very first
# request against a sleeping instance routinely fails at the transport level
# (connection reset/timeout) even though the instance is reachable and the
# credentials are fine, and a retry moments later succeeds once it's awake.
# Mirrors Modernization/services/llm.py's _TRANSIENT_RETRY_* pattern for the
# same class of "flaky external dependency, not a real failure" problem.
_OAUTH_RETRY_ATTEMPTS = 3
_OAUTH_RETRY_BASE_DELAY_SECONDS = 2


# Function: _fetch_oauth_token
async def _fetch_oauth_token(
    base_url: str,
    client_id: str,
    client_secret: str,
    username: str,
    password: str,
    timeout_seconds: int,
    verify_ssl: bool,
) -> str:
    """Resource Owner Password Credentials grant. Used instead of basic auth for
    instances that have basic auth disabled for the REST API."""
    token_url = f"{base_url.rstrip('/')}/oauth_token.do"
    response: httpx.Response | None = None
    for attempt in range(1, _OAUTH_RETRY_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout_seconds, verify=verify_ssl) as client:
                response = await client.post(
                    token_url,
                    data={
                        "grant_type": "password",
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "username": username,
                        "password": password,
                    },
                    headers={"Accept": "application/json"},
                )
            break
        except httpx.HTTPError as exc:
            if attempt >= _OAUTH_RETRY_ATTEMPTS:
                raise
            logger.warning(
                "ServiceNow OAuth token request transient error (attempt %d/%d), retrying "
                "(a sleeping PDI can take a few seconds to wake up): %s",
                attempt, _OAUTH_RETRY_ATTEMPTS, exc,
            )
            await asyncio.sleep(_OAUTH_RETRY_BASE_DELAY_SECONDS * attempt)

    assert response is not None  # loop above always either breaks with one or raises
    if response.status_code != 200:
        raise ValueError(f"OAuth token request failed: HTTP {response.status_code}: {response.text[:300]}")
    payload = response.json()
    access_token = payload.get("access_token")
    if not access_token:
        raise ValueError(f"OAuth token response missing access_token: {payload}")
    return access_token


# Function: _auth_kwargs
async def _auth_kwargs(
    base_url: str,
    username: str,
    password: str,
    client_id: str | None,
    client_secret: str | None,
    timeout_seconds: int,
    verify_ssl: bool,
) -> tuple[dict, dict]:
    """Returns (auth_kwarg, extra_headers). Uses OAuth bearer token when a client
    id/secret pair is supplied, otherwise falls back to HTTP basic auth."""
    if client_id and client_secret:
        token = await _fetch_oauth_token(
            base_url, client_id, client_secret, username, password, timeout_seconds, verify_ssl
        )
        return {}, {"Authorization": f"Bearer {token}"}
    return {"auth": (username, password)}, {}


# Function: _session_auth_headers
async def _session_auth_headers(
    client: httpx.AsyncClient,
    base_url: str,
    username: str,
    password: str,
) -> dict[str, str] | None:
    """Establish ServiceNow browser-session auth for PDIs with Basic disabled.

    ServiceNow accepts the same login.do credentials used by the browser, but
    cookie-authenticated REST requests additionally require the session's g_ck
    value in X-UserToken. Return None when login does not produce that token so
    callers can preserve the original upstream 401.
    """
    try:
        response = await client.post(
            f"{base_url.rstrip('/')}/login.do",
            data={
                "user_name": username,
                "user_password": password,
                "sys_action": "sysverb_login",
            },
            follow_redirects=True,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("ServiceNow session-login fallback failed: %s", exc)
        return None

    for pattern in (
        r"var\s+g_ck\s*=\s*['\"]([^'\"]+)",
        r"NOW\.user_token\s*=\s*['\"]([^'\"]+)",
    ):
        match = re.search(pattern, response.text or "")
        if match:
            return {"X-UserToken": match.group(1)}
    logger.warning("ServiceNow session-login fallback returned no g_ck user token")
    return None


# Function: test_connection
async def test_connection(
    base_url: str,
    username: str,
    password: str,
    timeout_seconds: int,
    verify_ssl: bool,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> dict:
    url = f"{base_url.rstrip('/')}/api/now/table/{cfg.SERVICENOW_TABLE}"
    params = {
        "sysparm_limit": "1",
        "sysparm_fields": "sys_id,number,short_description",
    }

    try:
        auth_kwargs, extra_headers = await _auth_kwargs(
            base_url, username, password, client_id, client_secret, timeout_seconds, verify_ssl
        )
    except ValueError as exc:
        return {"ok": False, "status_code": 502, "message": f"OAuth token request failed: {exc}"}
    except httpx.HTTPError as exc:
        return {"ok": False, "status_code": 502, "message": f"OAuth token request failed due to a transport error: {exc}"}

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, verify=verify_ssl) as client:
            response = await client.get(
                url,
                params=params,
                headers={"Accept": "application/json", **extra_headers},
                **auth_kwargs,
            )
            if response.status_code == 401 and not (client_id and client_secret):
                session_headers = await _session_auth_headers(
                    client, base_url, username, password
                )
                if session_headers:
                    response = await client.get(
                        url,
                        params=params,
                        headers={"Accept": "application/json", **session_headers},
                    )
    except httpx.ConnectError as exc:
        return {
            "ok": False,
            "status_code": 502,
            "message": (
                "Could not reach ServiceNow instance. Verify base URL and network access. "
                f"Details: {exc}"
            ),
        }
    except httpx.TimeoutException as exc:
        return {
            "ok": False,
            "status_code": 504,
            "message": f"ServiceNow connection timed out after {timeout_seconds}s. Details: {exc}",
        }
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "status_code": 502,
            "message": f"ServiceNow connection failed due to HTTP transport error: {exc}",
        }

    if response.status_code == 401:
        return {
            "ok": False,
            "status_code": 401,
            "message": "ServiceNow authentication failed. Verify username/password or token.",
        }
    if response.status_code == 403:
        return {
            "ok": False,
            "status_code": 403,
            "message": "ServiceNow denied access (403). Confirm API permissions for incident table.",
        }
    if response.status_code != 200:
        return {
            "ok": False,
            "status_code": response.status_code,
            "message": f"ServiceNow returned HTTP {response.status_code}: {response.text[:200]}",
        }

    try:
        payload = response.json()
        total = len(payload.get("result", []))
    except Exception:
        # ServiceNow can return HTML with HTTP 200 (for example instance hibernation pages).
        body_lower = (response.text or "").lower()
        if "instance hibernating" in body_lower or "developer.servicenow.com" in body_lower:
            return {
                "ok": False,
                "status_code": 503,
                "message": (
                    "ServiceNow instance appears to be hibernating. Open the instance in a browser "
                    "or wake it from developer.servicenow.com, then retry connection."
                ),
            }

        # Use 502 (bad gateway) so the caller knows this is a ServiceNow-side issue, not an
        # app-level auth failure (which would incorrectly trigger a client-side logout redirect).
        return {
            "ok": False,
            "status_code": 502,
            "message": (
                "ServiceNow returned HTTP 200 but the response body is not JSON. "
                "The instance may be redirecting to a login page — check the base URL, "
                "username, and password."
            ),
        }
    return {"ok": True, "message": "Connected to ServiceNow.", "sample_records": total}


# Function: _fetch_servicenow_pages
async def _fetch_servicenow_pages(
    client: httpx.AsyncClient,
    url: str,
    username: str,
    password: str,
    query: str,
    limit: int,
    batch_size: int,
    timeout_seconds: int,
    base_url: str = "",
    client_id: str | None = None,
    client_secret: str | None = None,
    verify_ssl: bool = True,
) -> list[dict]:
    use_oauth = bool(client_id and client_secret)
    use_session = False
    auth_kwargs: dict = {}
    extra_headers: dict = {}
    if use_oauth:
        token = await _fetch_oauth_token(
            base_url, client_id, client_secret, username, password, timeout_seconds, verify_ssl
        )
        extra_headers = {"Authorization": f"Bearer {token}"}
    else:
        auth_kwargs = {"auth": (username, password)}

    fetched_records: list[dict] = []
    offset = 0
    while len(fetched_records) < limit:
        page_limit = min(batch_size, limit - len(fetched_records))
        params = {
            "sysparm_query": query,
            "sysparm_limit": str(page_limit),
            "sysparm_offset": str(offset),
            "sysparm_display_value": "true",
            "sysparm_exclude_reference_link": "true",
            "sysparm_fields": ",".join(_servicenow_fields()),
        }

        try:
            response = await client.get(
                url,
                params=params,
                headers={"Accept": "application/json", **extra_headers},
                **auth_kwargs,
            )
            # OAuth token may have expired mid-sync (default lifetime ~30 min); refresh once and retry.
            if use_oauth and response.status_code == 401:
                token = await _fetch_oauth_token(
                    base_url, client_id, client_secret, username, password, timeout_seconds, verify_ssl
                )
                extra_headers = {"Authorization": f"Bearer {token}"}
                response = await client.get(
                    url, params=params, headers={"Accept": "application/json", **extra_headers}
                )
            elif not use_oauth and not use_session and response.status_code == 401:
                session_headers = await _session_auth_headers(
                    client, base_url, username, password
                )
                if session_headers:
                    use_session = True
                    auth_kwargs = {}
                    extra_headers = session_headers
                    response = await client.get(
                        url,
                        params=params,
                        headers={"Accept": "application/json", **extra_headers},
                    )
        except httpx.ConnectError as exc:
            raise ValueError(f"Could not reach ServiceNow instance. Verify base URL/network. Details: {exc}")
        except httpx.TimeoutException as exc:
            raise ValueError(f"ServiceNow sync request timed out after {timeout_seconds}s: {exc}")
        except httpx.HTTPError as exc:
            raise ValueError(f"ServiceNow sync HTTP transport error: {exc}")

        if response.status_code == 401:
            raise ValueError("ServiceNow authentication failed.")
        if response.status_code != 200:
            raise ValueError(f"ServiceNow returned HTTP {response.status_code}: {response.text[:250]}")

        rows = response.json().get("result", [])
        if not rows:
            break

        fetched_records.extend(rows)
        offset += len(rows)

        if len(rows) < page_limit:
            break

    return fetched_records


# Function: _write_servicenow_snapshot
def _write_servicenow_snapshot(snapshot_path: Path, fetched_records: list[dict]) -> None:
    with snapshot_path.open("w", encoding="utf-8") as fp:
        for row in fetched_records:
            fp.write(json.dumps(row, ensure_ascii=True) + "\n")


# Function: _index_docs_to_pgvector
async def _index_docs_to_pgvector(
    docs: list[Document],
    snapshot_path: Path,
    sync_log_id: int | None,
    fetched_records: list[dict],
) -> int:
    chunks_indexed = 0
    try:
        for i in range(0, len(docs), 300):
            batch = docs[i : i + 300]
            batch_count = await asyncio.to_thread(index_documents, batch, cfg.LLM_PROVIDER)
            chunks_indexed += batch_count
    except Exception as exc:
        logger.exception("ServiceNow one-time sync indexing failed")
        if sync_log_id is not None:
            await asyncio.to_thread(
                finalize_sync_log,
                sync_log_id,
                status="failed",
                incidents_count=len(fetched_records),
                qdrant_points_indexed=0,
                details={"error": str(exc)},
            )
        raise ValueError(
            "Fetched incidents from ServiceNow but failed to index into vector DB. "
            "Verify embedding provider availability (Ollama/OpenAI), model settings, and PostgreSQL pgvector health. "
            f"Snapshot saved to: {snapshot_path}. Details: {exc}"
        )
    return chunks_indexed


# Function: _run_modern_servicenow_pipeline
async def _run_modern_servicenow_pipeline(
    fetched_records: list[dict],
    source_name: str,
    sync_log_id: int | None,
    skip_pgvector_embed: bool,
    snapshot_path: Path,
) -> tuple[int, int, str | None]:
    pg_incidents_persisted = 0
    qdrant_points_indexed = 0
    modern_warning = None
    try:
        await asyncio.to_thread(
            persist_incidents,
            fetched_records,
            source_name,
            int(sync_log_id or 0),
        )
        pg_incidents_persisted = await asyncio.to_thread(count_incidents_by_source, source_name)

        # Use appropriate vector store based on configuration
        if cfg.VECTOR_BACKEND in {"lancedb", "hybrid"}:
            qdrant_points_indexed = await asyncio.to_thread(
                index_incidents_to_lancedb,
                fetched_records,
                source_name,
            )
        else:
            qdrant_points_indexed = await asyncio.to_thread(
                index_incidents_to_qdrant,
                fetched_records,
                source_name,
                cfg.LLM_PROVIDER,
            )
    except Exception as exc:
        modern_warning = f"Modern pipeline fallback triggered: {exc}"
        logger.warning("Modern ServiceNow pipeline failed, retaining legacy pgvector path: %s", exc)
        # When LanceDB is the only embedding store, its failure is always fatal — there
        # is no pgvector fallback to rely on for search.
        is_lancedb_primary = cfg.VECTOR_BACKEND in {"lancedb"} and skip_pgvector_embed
        if cfg.MODERN_PIPELINE_STRICT or is_lancedb_primary:
            if sync_log_id is not None:
                await asyncio.to_thread(
                    finalize_sync_log,
                    sync_log_id,
                    status="failed",
                    incidents_count=len(fetched_records),
                    qdrant_points_indexed=qdrant_points_indexed,
                    details={"error": str(exc), "strict_mode": True},
                )
            raise ValueError(
                "Fetched incidents from ServiceNow but failed to index into vector DB. "
                "Verify embedding provider availability (Ollama/OpenAI), model settings, and PostgreSQL pgvector health. "
                f"Snapshot saved to: {snapshot_path}. Details: {exc}"
            )
    return pg_incidents_persisted, qdrant_points_indexed, modern_warning


# Function: _verify_servicenow_dual_write
async def _verify_servicenow_dual_write(
    sync_log_id: int | None,
    fetched_records: list[dict],
    pg_incidents_persisted: int,
    qdrant_points_indexed: int,
    source_name: str,
    chunks_indexed: int,
    require_dual_write: bool,
) -> None:
    if not require_dual_write:
        return
    dual_sync_errors: list[str] = []
    if pg_incidents_persisted <= 0:
        dual_sync_errors.append("PostgreSQL operational incident rows were not persisted.")
    if cfg.VECTOR_BACKEND in {"hybrid", "qdrant"} and qdrant_points_indexed <= 0:
        dual_sync_errors.append("Qdrant points were not indexed.")
    if not dual_sync_errors:
        return
    detail = " ".join(dual_sync_errors)
    if sync_log_id is not None:
        await asyncio.to_thread(
            finalize_sync_log,
            sync_log_id,
            status="failed",
            incidents_count=len(fetched_records),
            qdrant_points_indexed=qdrant_points_indexed,
            details={
                "source_name": source_name,
                "legacy_pgvector_chunks": chunks_indexed,
                "qdrant_points_indexed": qdrant_points_indexed,
                "pg_incidents_persisted": pg_incidents_persisted,
                "dual_write_required": True,
                "error": detail,
            },
        )
    raise ValueError(f"One-time sync verification failed. {detail}")


# Function: _finalize_completed_servicenow_sync
async def _finalize_completed_servicenow_sync(
    sync_log_id: int | None,
    fetched_records: list[dict],
    chunks_indexed: int,
    qdrant_points_indexed: int,
    pg_incidents_persisted: int,
    source_name: str,
    require_dual_write: bool,
    modern_warning: str | None,
) -> str | None:
    if sync_log_id is None:
        return modern_warning
    try:
        await asyncio.to_thread(
            finalize_sync_log,
            sync_log_id,
            status="completed",
            incidents_count=len(fetched_records),
            qdrant_points_indexed=qdrant_points_indexed,
            details={
                "source_name": source_name,
                "legacy_pgvector_chunks": chunks_indexed,
                "qdrant_points_indexed": qdrant_points_indexed,
                "pg_incidents_persisted": pg_incidents_persisted,
                "dual_write_required": bool(require_dual_write),
                "warning": modern_warning,
            },
        )
    except Exception as exc:
        modern_warning = f"Operational sync log update failed: {exc}"
    return modern_warning


# Function: one_time_sync
async def one_time_sync(
    base_url: str,
    username: str,
    password: str,
    query: str = "active=true",
    limit: int = 500,
    batch_size: int = 100,
    timeout_seconds: int | None = None,
    verify_ssl: bool | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> dict:
    timeout_seconds = timeout_seconds or cfg.SERVICENOW_TIMEOUT_SECONDS
    verify_ssl = cfg.SERVICENOW_VERIFY_SSL if verify_ssl is None else verify_ssl

    base_url = base_url.rstrip("/")
    url = f"{base_url}/api/now/table/{cfg.SERVICENOW_TABLE}"
    sync_log_id: int | None = None

    async with httpx.AsyncClient(timeout=timeout_seconds, verify=verify_ssl) as client:
        fetched_records = await _fetch_servicenow_pages(
            client, url, username, password, query, limit, batch_size, timeout_seconds,
            base_url=base_url, client_id=client_id, client_secret=client_secret, verify_ssl=verify_ssl,
        )
    fetched_records = [_normalize_custom_table_record(record) for record in fetched_records]

    if not fetched_records:
        return {
            "status": "ok",
            "message": "No incidents matched the query.",
            "tickets_fetched": 0,
            "chunks_indexed": 0,
            "snapshot_file": None,
        }

    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d_%H%M%S")
    source_name = f"servicenow_incidents_{stamp}.jsonl"

    if cfg.MODERN_PIPELINE_ENABLED:
        sync_log_id = await asyncio.to_thread(start_sync_log, source_name, query, limit)

    data_dir = Path(cfg.DATA_DIR)
    data_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = data_dir / source_name

    # Write snapshot in a thread so we don't block the event loop
    await asyncio.to_thread(_write_servicenow_snapshot, snapshot_path, fetched_records)

    chunks_indexed = 0
    qdrant_points_indexed = 0
    pg_incidents_persisted = 0
    modern_warning = None

    # When LanceDB is the primary vector store, skip pgvector embedding — it doubles
    # embedding time with no search benefit (similarity_search uses LanceDB first).
    # pgvector is still used for keyword/metadata search; only the vector index is skipped.
    _skip_pgvector_embed = cfg.VECTOR_BACKEND in {"lancedb"}

    if not _skip_pgvector_embed:
        docs = [_build_incident_doc(record, source_name) for record in fetched_records]
        chunks_indexed = await _index_docs_to_pgvector(docs, snapshot_path, sync_log_id, fetched_records)
        del docs
    else:
        logger.info("Skipping pgvector embedding (VECTOR_BACKEND=lancedb) — LanceDB is primary search store")

    if cfg.MODERN_PIPELINE_ENABLED:
        pg_incidents_persisted, qdrant_points_indexed, modern_warning = await _run_modern_servicenow_pipeline(
            fetched_records, source_name, sync_log_id, _skip_pgvector_embed, snapshot_path
        )

    require_dual_write = cfg.SYNC_REQUIRE_DUAL_WRITE and cfg.DB_BACKEND == "postgres"

    if cfg.MODERN_PIPELINE_ENABLED:
        await _verify_servicenow_dual_write(
            sync_log_id,
            fetched_records,
            pg_incidents_persisted,
            qdrant_points_indexed,
            source_name,
            chunks_indexed,
            require_dual_write,
        )

    if cfg.MODERN_PIPELINE_ENABLED:
        modern_warning = await _finalize_completed_servicenow_sync(
            sync_log_id,
            fetched_records,
            chunks_indexed,
            qdrant_points_indexed,
            pg_incidents_persisted,
            source_name,
            require_dual_write,
            modern_warning,
        )

    # Persist refresh timestamp used by freshness-based chat gating.
    await asyncio.to_thread(
        record_sync,
        source_name,
        len(fetched_records),
        max(chunks_indexed, qdrant_points_indexed),
    )

    response = {
        "status": "ok",
        "message": "ServiceNow one-time sync completed.",
        "tickets_fetched": len(fetched_records),
        "chunks_indexed": max(chunks_indexed, qdrant_points_indexed),
        "pg_incidents_persisted": pg_incidents_persisted,
        "qdrant_points_indexed": qdrant_points_indexed,
        "lancedb_points_indexed": (
            qdrant_points_indexed if cfg.VECTOR_BACKEND in {"lancedb", "hybrid"} else 0
        ),
        "vector_backend": cfg.VECTOR_BACKEND,
        "sync_log_id": sync_log_id,
        "synced_at": now.isoformat(),
        "snapshot_file": str(snapshot_path),
        "source_name": source_name,
    }
    if modern_warning:
        response["warning"] = modern_warning
    return response
