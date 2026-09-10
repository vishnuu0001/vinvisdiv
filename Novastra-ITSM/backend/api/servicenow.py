# ---------------------------------------------------------------------------
# Author: Vishnuu A
# Scope: ServiceNow integration — fetch incident details and run RAG on them.
# Date: 2026-02-25
# ---------------------------------------------------------------------------
"""
ServiceNow integration — fetch incident details and run RAG on them.
Supports both live SN REST API and manual input.
Screenshot/attachment handling moved to the agent chat endpoint.
"""
import asyncio
import logging
import uuid
from typing import Any, Dict, Optional
from urllib.parse import unquote, urlparse

import httpx
from fastapi import APIRouter, HTTPException, UploadFile, File, Form

import backend.config as cfg
from backend.models.schemas import (
    ManualTicketRequest,
    QueryResponse,
    ServiceNowConnectionRequest,
    ServiceNowSyncRequest,
    ServiceNowSyncStatusResponse,
    ServiceNowTicketRequest,
    TicketUpdateRequest,
    TicketUpdateResponse,
)
from backend.rag.document_loader import load_image_bytes
from backend.rag.pipeline import query_rag
from backend.security.crypto import decrypt_credential_envelope, encrypt_credential_envelope
from backend.services.servicenow_sync import one_time_sync, test_connection
from backend.services.sync_status_store import get_sync_status

router = APIRouter(prefix="/api/servicenow", tags=["ServiceNow"])
logger = logging.getLogger(__name__)

# ── In-memory sync job store ──────────────────────────────────
# Maps job_id -> {status, result, error}
_sync_jobs: Dict[str, Dict[str, Any]] = {}


# Function: _resolve_provider
def _resolve_provider(req_provider: Optional[str]) -> str:
    if req_provider:
        return req_provider.value if hasattr(req_provider, "value") else req_provider
    return cfg.LLM_PROVIDER


# Function: _clean_credential
def _clean_credential(value: Optional[str]) -> str:
    if not value:
        return ""
    return unquote(value.strip().strip('"').strip("'"))


# Function: _normalize_instance_url
def _normalize_instance_url(base_url: str) -> str:
    """
    Accept either instance base URL or full login URL and return instance root.
    Examples:
      - https://dev123.service-now.com
      - https://dev123.service-now.com/login.do?...  -> https://dev123.service-now.com
    """
    raw = (base_url or "").strip()
    if not raw:
        return ""

    # If user pastes without scheme, assume https.
    if "//" not in raw:
        raw = f"https://{raw}"

    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        return ""

    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


# Function: _resolve_sn_credentials
def _resolve_sn_credentials(
    base_url: Optional[str],
    username: Optional[str],
    password: Optional[str],
    encrypted_password: Optional[str] = None,
) -> tuple[str, str, str]:
    resolved_base = _normalize_instance_url(base_url or cfg.SERVICENOW_BASE_URL or "")
    resolved_user = _clean_credential(username or cfg.SERVICENOW_USERNAME)
    try:
        decrypted_password = decrypt_credential_envelope(encrypted_password)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    resolved_pass = _clean_credential(decrypted_password or password or cfg.SERVICENOW_PASSWORD)

    if not all([resolved_base, resolved_user, resolved_pass]):
        raise HTTPException(
            status_code=400,
            detail="ServiceNow credentials are incomplete. Provide base URL, username, and password/token.",
        )

    return resolved_base, resolved_user, resolved_pass


# Function: _resolve_sn_oauth_credentials
def _resolve_sn_oauth_credentials(
    client_id: Optional[str],
    client_secret: Optional[str],
) -> tuple[str, str]:
    """Optional OAuth client id/secret (resource owner password credentials grant),
    used instead of basic auth for instances that reject basic auth on the REST API.
    Returns ("", "") when neither the request nor config supplies them, meaning the
    caller should fall back to basic auth."""
    resolved_client_id = _clean_credential(client_id or cfg.SERVICENOW_CLIENT_ID)
    resolved_client_secret = _clean_credential(client_secret or cfg.SERVICENOW_CLIENT_SECRET)
    return resolved_client_id, resolved_client_secret


# ── Live ServiceNow fetch ─────────────────────────────────────

# Function: _fetch_sn_incident
async def _fetch_sn_incident(ticket_number: str) -> dict:
    if not all([cfg.SERVICENOW_BASE_URL, cfg.SERVICENOW_USERNAME, cfg.SERVICENOW_PASSWORD]):
        raise HTTPException(
            status_code=503,
            detail="ServiceNow credentials are not configured. Use manual input or screenshot upload instead.",
        )

    url = f"{cfg.SERVICENOW_BASE_URL}/api/now/table/incident"
    params = {
        "sysparm_query": f"number={ticket_number}",
        "sysparm_limit": "1",
        "sysparm_fields": "number,short_description,description,priority,category,state,assigned_to,caller_id,opened_at",
    }

    async with httpx.AsyncClient(timeout=15, verify=cfg.SERVICENOW_VERIFY_SSL) as client:
        response = await client.get(
            url,
            params=params,
            auth=(cfg.SERVICENOW_USERNAME, cfg.SERVICENOW_PASSWORD),
            headers={"Accept": "application/json"},
        )

    if response.status_code == 401:
        raise HTTPException(status_code=401, detail="ServiceNow authentication failed.")
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail=f"ServiceNow returned HTTP {response.status_code}")

    data = response.json()
    records = data.get("result", [])
    if not records:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_number} not found in ServiceNow.")
    return records[0]


# Function: _ticket_to_question
def _ticket_to_question(ticket: dict, extra_context: Optional[str] = None) -> str:
    lines = [
        f"Incident Number: {ticket.get('number', 'N/A')}",
        f"Short Description: {ticket.get('short_description', '')}",
        f"Description: {ticket.get('description', '')}",
        f"Priority: {ticket.get('priority', 'N/A')}",
        f"Category: {ticket.get('category', 'N/A')}",
    ]
    if extra_context:
        lines.append(f"Additional Context: {extra_context}")
    lines.append("\nBased on the above incident details, what is the recommended resolution?")
    return "\n".join(lines)


# Function: fetch_and_resolve
@router.post("/fetch-and-resolve", response_model=QueryResponse)
async def fetch_and_resolve(request: ServiceNowTicketRequest):
    """
    Fetch a live ServiceNow ticket and recommend a resolution using the knowledge base.
    """
    ticket = await _fetch_sn_incident(request.ticket_number)
    question = _ticket_to_question(ticket, request.additional_context)
    provider = _resolve_provider(request.llm_provider)

    try:
        result = query_rag(question=question, llm_provider=provider)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return QueryResponse(**result)


# Function: get_servicenow_connection_defaults
@router.get("/connection-defaults")
async def get_servicenow_connection_defaults():
    """Return connection defaults and a short-lived encrypted password envelope.

    Plaintext credentials and encryption keys never leave the backend. The opaque
    envelope can only be decrypted by this service and expires after one hour.
    """
    has_basic = bool(cfg.SERVICENOW_BASE_URL and cfg.SERVICENOW_USERNAME and cfg.SERVICENOW_PASSWORD)
    has_oauth = bool(cfg.SERVICENOW_CLIENT_ID and cfg.SERVICENOW_CLIENT_SECRET)
    return {
        "base_url": cfg.SERVICENOW_BASE_URL or None,
        "username": cfg.SERVICENOW_USERNAME or None,
        "client_id": cfg.SERVICENOW_CLIENT_ID or None,
        "encrypted_password": encrypt_credential_envelope(cfg.SERVICENOW_PASSWORD),
        "has_password_configured": has_basic,
        "has_client_secret_configured": has_oauth,
        "suggested_auth_type": "oauth" if has_oauth else ("basic" if has_basic else None),
    }


# Function: test_servicenow_connection
@router.post("/test-connection")
async def test_servicenow_connection(request: ServiceNowConnectionRequest):
    """Validate ServiceNow credentials before running one-time sync."""
    if request.auth_type not in {"basic", "oauth"}:
        raise HTTPException(status_code=400, detail="auth_type must be 'basic' or 'oauth'.")

    base_url, username, password = _resolve_sn_credentials(
        request.base_url,
        request.username,
        request.password,
        request.encrypted_password,
    )
    client_id, client_secret = "", ""
    if request.auth_type == "oauth":
        client_id, client_secret = _resolve_sn_oauth_credentials(request.client_id, request.client_secret)
        if not client_id or not client_secret:
            raise HTTPException(
                status_code=400,
                detail="auth_type is 'oauth' but client_id/client_secret were not provided.",
            )

    result = await test_connection(
        base_url=base_url,
        username=username,
        password=password,
        timeout_seconds=cfg.SERVICENOW_TIMEOUT_SECONDS,
        verify_ssl=cfg.SERVICENOW_VERIFY_SSL,
        client_id=client_id or None,
        client_secret=client_secret or None,
    )

    if not result.get("ok"):
        status_code = int(result.get("status_code") or 502)
        if status_code not in {400, 401, 403, 404, 408, 429, 500, 502, 503, 504}:
            status_code = 502
        raise HTTPException(status_code=status_code, detail=result.get("message", "Connection failed."))

    return result


# Function: servicenow_sync_status
@router.get("/sync-status", response_model=ServiceNowSyncStatusResponse)
async def servicenow_sync_status(max_age_hours: int = 168):
    """Return whether chat can be used based on vector DB refresh recency."""
    if max_age_hours < 1 or max_age_hours > 168:
        raise HTTPException(status_code=400, detail="max_age_hours must be between 1 and 168")
    return get_sync_status(max_age_hours=max_age_hours)


# Function: sync_servicenow_tickets
@router.post("/one-time-sync")
async def sync_servicenow_tickets(request: ServiceNowSyncRequest):
    """
    Start a one-time ServiceNow incident ingestion in the background.
    Returns a job_id immediately; poll GET /sync-job/{job_id} for status.
    """
    if request.auth_type not in {"basic", "oauth"}:
        raise HTTPException(status_code=400, detail="auth_type must be 'basic' or 'oauth'.")

    base_url, username, password = _resolve_sn_credentials(
        request.base_url,
        request.username,
        request.password,
        request.encrypted_password,
    )
    client_id, client_secret = "", ""
    if request.auth_type == "oauth":
        client_id, client_secret = _resolve_sn_oauth_credentials(request.client_id, request.client_secret)
        if not client_id or not client_secret:
            raise HTTPException(
                status_code=400,
                detail="auth_type is 'oauth' but client_id/client_secret were not provided.",
            )

    job_id = str(uuid.uuid4())
    _sync_jobs[job_id] = {"status": "running", "result": None, "error": None}

    # Function: _run
    async def _run():
        try:
            result = await one_time_sync(
                base_url=base_url,
                username=username,
                password=password,
                query=request.query,
                limit=request.limit,
                batch_size=request.batch_size,
                timeout_seconds=cfg.SERVICENOW_TIMEOUT_SECONDS,
                verify_ssl=cfg.SERVICENOW_VERIFY_SSL,
                client_id=client_id or None,
                client_secret=client_secret or None,
            )
            _sync_jobs[job_id]["status"] = "done"
            _sync_jobs[job_id]["result"] = result
        except Exception as exc:
            logger.exception("ServiceNow one-time sync background job failed")
            _sync_jobs[job_id]["status"] = "error"
            _sync_jobs[job_id]["error"] = str(exc)

    # create_task schedules the coroutine concurrently so the event loop
    # remains free to serve poll requests while indexing is in progress.
    asyncio.create_task(_run())
    return {"job_id": job_id, "status": "running"}


# Function: get_sync_job_status
@router.get("/sync-job/{job_id}")
async def get_sync_job_status(job_id: str):
    """Poll the status of a background sync job started by /one-time-sync."""
    job = _sync_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Sync job not found.")
    return {
        "job_id": job_id,
        "status": job["status"],
        "result": job["result"],
        "error": job["error"],
    }


# ── Manual ticket input ───────────────────────────────────────

# Function: manual_resolve
@router.post("/manual-resolve", response_model=QueryResponse)
async def manual_resolve(request: ManualTicketRequest):
    """
    Accept manually typed ticket details and recommend a resolution.
    Useful when live SN connection is unavailable.
    """
    ticket = {
        "number": request.incident_number,
        "short_description": request.short_description,
        "description": request.description,
        "priority": request.priority or "",
        "category": request.category or "",
    }
    question = _ticket_to_question(ticket, request.additional_context)
    provider = _resolve_provider(request.llm_provider)

    try:
        result = query_rag(question=question, llm_provider=provider)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return QueryResponse(**result)


# ── Screenshot-based ticket ───────────────────────────────────

# Function: screenshot_resolve
@router.post("/screenshot-resolve", response_model=QueryResponse)
async def screenshot_resolve(
    screenshot: UploadFile = File(...),
    additional_context: Optional[str] = Form(None),
    llm_provider: Optional[str] = Form(None),
):
    """
    Upload a ServiceNow screenshot. OCR extracts the text, then RAG recommends a fix.
    """
    content = await screenshot.read()
    docs = load_image_bytes(content, screenshot.filename)

    if not docs:
        raise HTTPException(
            status_code=422,
            detail="Could not extract text from the screenshot. Ensure the image is clear and readable.",
        )

    extracted_text = "\n".join(d.page_content for d in docs)
    question = (
        f"The following text was extracted from a ServiceNow screenshot:\n\n{extracted_text}\n"
    )
    if additional_context:
        question += f"\nAdditional context: {additional_context}\n"
    question += "\nWhat is the recommended resolution for this incident?"

    provider = _resolve_provider(llm_provider)

    try:
        result = query_rag(question=question, llm_provider=provider)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return QueryResponse(**result)


# ── Update ServiceNow ticket with resolution notes ────────────

# Function: update_ticket
@router.post("/update-ticket", response_model=TicketUpdateResponse)
async def update_ticket(request: TicketUpdateRequest):
    """
    Push resolution notes back to a ServiceNow ticket.

    When live SN credentials are configured, patches the ticket's
    work_notes and optionally changes its state.
    When credentials are absent (dev/demo mode) the update is
    acknowledged locally and logged — no external call is made.
    """
    logger.info(
        "Ticket update requested: %s → state=%s", request.ticket_number, request.state
    )

    # ── State code mapping (ServiceNow standard values) ──────
    _STATE_CODES = {
        "new": "1",
        "in_progress": "2",
        "on_hold": "3",
        "resolved": "6",
        "closed": "7",
        "cancelled": "8",
    }
    state_code = _STATE_CODES.get(request.state or "resolved", "6")

    # ── Live ServiceNow PATCH ─────────────────────────────────
    if all([cfg.SERVICENOW_BASE_URL, cfg.SERVICENOW_USERNAME, cfg.SERVICENOW_PASSWORD]):
        # Step 1: look up the sys_id for the ticket number
        lookup_url = f"{cfg.SERVICENOW_BASE_URL}/api/now/table/incident"
        params = {
            "sysparm_query": f"number={request.ticket_number}",
            "sysparm_limit": "1",
            "sysparm_fields": "sys_id,number,state",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            lookup_resp = await client.get(
                lookup_url,
                params=params,
                auth=(cfg.SERVICENOW_USERNAME, cfg.SERVICENOW_PASSWORD),
                headers={"Accept": "application/json"},
            )

        if lookup_resp.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"ServiceNow lookup failed with HTTP {lookup_resp.status_code}",
            )
        records = lookup_resp.json().get("result", [])
        if not records:
            raise HTTPException(
                status_code=404,
                detail=f"Ticket {request.ticket_number} not found in ServiceNow.",
            )

        sys_id = records[0]["sys_id"]

        # Step 2: PATCH the ticket — add work_notes + set state
        patch_url = f"{cfg.SERVICENOW_BASE_URL}/api/now/table/incident/{sys_id}"
        payload = {
            "work_notes": f"[AI Support Agent Resolution]\n{request.resolution_notes}",
            "state": state_code,
        }

        async with httpx.AsyncClient(timeout=15) as client:
            patch_resp = await client.patch(
                patch_url,
                json=payload,
                auth=(cfg.SERVICENOW_USERNAME, cfg.SERVICENOW_PASSWORD),
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )

        if patch_resp.status_code not in (200, 204):
            raise HTTPException(
                status_code=502,
                detail=f"ServiceNow update failed with HTTP {patch_resp.status_code}: {patch_resp.text[:200]}",
            )

        logger.info("Ticket %s updated in ServiceNow (state → %s).", request.ticket_number, request.state)
        return TicketUpdateResponse(
            ticket_number=request.ticket_number,
            status="updated",
            message=f"Ticket {request.ticket_number} updated in ServiceNow. State set to '{request.state}'.",
        )

    # ── Demo / dev mode — no live SN credentials ─────────────
    logger.info(
        "Demo mode: ticket %s update acknowledged (no SN credentials configured).",
        request.ticket_number,
    )
    return TicketUpdateResponse(
        ticket_number=request.ticket_number,
        status="demo_acknowledged",
        message=(
            f"Demo mode: resolution notes recorded for {request.ticket_number}. "
            "Configure SERVICENOW_BASE_URL/USERNAME/PASSWORD to push live updates."
        ),
    )
