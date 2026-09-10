# ---------------------------------------------------------------------------
# Author: Vishnuu A
# Scope: AI-Powered Digital Operations Cockpit – FastAPI Backend
# Date: 2025-07-30
# ---------------------------------------------------------------------------
"""
AI-Powered Digital Operations Cockpit – FastAPI Backend
Port: 8087

Routes
------
Connection management  : /api/status, /api/connect, /api/sync, /api/disconnect
Dashboard JSON data    : /api/kpis, /api/monthly-volume, /api/cycle-time, etc.
Chart PNG rendering    : /render/*.png
Prometheus metrics     : /metrics
"""

import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from auth import DASHBOARD_APP, auth_required, decode_access_token, extract_bearer_token

from config import settings
from data_cache import cache
from sn_client import ServiceNowClient
from security.crypto import decrypt_value, encrypt_value
import settings_store
import kpis as kpi_engine
import charts
from automation import score_automation_candidates
from ollama_service import (
    generate_insights_ollama,
    enrich_automation_candidates_ollama,
    get_ollama_model_processor,
    build_automation_key,
    build_automation_id_key,
    resolve_ollama_model,
)
from qdrant_store import CriticalAlertStore

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Qdrant critical alert store (module-level singleton)
# ---------------------------------------------------------------------------
alert_store = CriticalAlertStore(
    url=settings.QDRANT_URL,
    collection=settings.QDRANT_COLLECTION,
    timeout=settings.QDRANT_TIMEOUT_SECONDS,
)

# In-memory list of synthetically-invoked incidents (also persisted to Qdrant)
_invoked_critical_incidents: List[Dict[str, Any]] = []

# ---------------------------------------------------------------------------
# Simple TTL cache for expensive LLM results
# ---------------------------------------------------------------------------
import threading as _threading

_llm_cache_lock = _threading.Lock()
_llm_cache: Dict[str, Dict[str, Any]] = {}   # key -> {"data": ..., "expires_at": float}
_LLM_CACHE_TTL = 600  # seconds (10 min)

_prewarm_lock = _threading.Lock()
_prewarm_running = False

# Prevents concurrent Ollama insights calls (prewarm thread vs user request).
_insights_generation_lock = _threading.Lock()

# Short-lived KPI result cache (30s) — avoids redundant pandas work on frequent polls.
_kpi_cache_lock = _threading.Lock()
_kpi_cache: Dict[str, Any] = {}
_KPI_CACHE_TTL = 30  # seconds

# A successful ServiceNow connection is retained server-side for five minutes.
# Credentials remain inside the ServiceNowClient and are never returned to the UI.
_CONNECTION_SESSION_TTL = 5 * 60
_connection_session_lock = _threading.Lock()
_connection_session: Dict[str, Any] = {"client": None, "expires_at": 0.0}


# Function: _set_connection_session
def _set_connection_session(client: ServiceNowClient) -> float:
    expires_at = time.time() + _CONNECTION_SESSION_TTL
    with _connection_session_lock:
        _connection_session["client"] = client
        _connection_session["expires_at"] = expires_at
    return expires_at


# Function: _get_connection_session
def _get_connection_session() -> tuple[Optional[ServiceNowClient], float]:
    with _connection_session_lock:
        expires_at = float(_connection_session.get("expires_at") or 0.0)
        client = _connection_session.get("client")
        if client is None or expires_at <= time.time():
            _connection_session["client"] = None
            _connection_session["expires_at"] = 0.0
            return None, 0.0
        return client, expires_at


# Function: _clear_connection_session
def _clear_connection_session() -> None:
    with _connection_session_lock:
        _connection_session["client"] = None
        _connection_session["expires_at"] = 0.0

# Cached result of resolve_ollama_model() — avoids HTTP call on every /api/status poll.
_ollama_model_cache_lock = _threading.Lock()
_ollama_model_resolved: Optional[str] = None
_ollama_model_resolved_at: float = 0.0
_OLLAMA_MODEL_CACHE_TTL = 300  # seconds (5 min)


# Function: _get_ollama_model_cached
def _get_ollama_model_cached() -> Optional[str]:
    global _ollama_model_resolved, _ollama_model_resolved_at
    with _ollama_model_cache_lock:
        if time.time() < _ollama_model_resolved_at + _OLLAMA_MODEL_CACHE_TTL:
            return _ollama_model_resolved
    model = resolve_ollama_model(
        settings.OLLAMA_MODEL,
        base_url=settings.OLLAMA_BASE_URL,
        timeout=min(float(settings.OLLAMA_TIMEOUT_SECONDS), 10.0),
    )
    with _ollama_model_cache_lock:
        _ollama_model_resolved = model
        _ollama_model_resolved_at = time.time()
    return model


# Function: _generate_insights_background
def _generate_insights_background(
    summary: Dict[str, Any],
    incident_data: Dict[str, Any],
    change_data: Dict[str, Any],
    sr_data: Dict[str, Any],
    hotspots: List[Dict[str, Any]],
) -> None:
    """Generate LLM insights in a background thread; cache the result."""
    _insights_cache_key = "insights:executive"
    with _insights_generation_lock:
        if _llm_cache_get(_insights_cache_key) is not None:
            return
        try:
            result = generate_insights_ollama(
                summary, incident_data, change_data, sr_data, hotspots,
                base_url=settings.OLLAMA_BASE_URL,
                model_name=_get_ollama_model_cached(),
                timeout=float(settings.OLLAMA_TIMEOUT_SECONDS),
            )
            _llm_cache_set(_insights_cache_key, result)
            logger.info("Background insights generation completed and cached")
        except Exception as exc:
            logger.warning("Background insights generation failed: %s", exc)


# Function: _build_insights_kpi_data
def _build_insights_kpi_data() -> tuple:
    """Return (summary, incident_data, change_data, sr_data, hotspots) from live cache."""
    summary = kpi_engine.summary_kpis(cache.incidents_df, cache.changes_df, cache.service_requests_df)
    incident_data = {
        "summary": kpi_engine.summary_kpis(cache.incidents_df, pd.DataFrame(), pd.DataFrame()),
        "cycle_time": kpi_engine.cycle_time_stats(cache.incidents_df, "resolution_hours"),
        "priority_dist": kpi_engine.priority_distribution(cache.incidents_df),
    }
    change_data = {
        "risk": kpi_engine.change_risk_summary(cache.changes_df),
        "cycle_time": kpi_engine.cycle_time_stats(cache.changes_df, "implementation_hours"),
    }
    sr_data = {"summary": kpi_engine.sr_inquiry_summary(cache.service_requests_df)}
    hotspots = kpi_engine.application_hotspots(cache.incidents_df, top_n=5)
    return summary, incident_data, change_data, sr_data, hotspots


# Function: _background_prewarm_llm_cache
def _background_prewarm_llm_cache() -> None:
    """
    Pre-compute expensive LLM results immediately after a data sync so that
    the first user request after sync gets a cache hit instead of waiting.
    Runs in a daemon thread; silently skips if already running.
    """
    global _prewarm_running
    with _prewarm_lock:
        if _prewarm_running:
            return
        _prewarm_running = True

    try:
        if not settings.OLLAMA_ENABLED:
            return

        # --- Leadership Insights ---
        try:
            summary, incident_data, change_data, sr_data, hotspots = _build_insights_kpi_data()
            with _insights_generation_lock:
                if _llm_cache_get("insights:executive") is None:
                    insights = generate_insights_ollama(
                        summary, incident_data, change_data, sr_data, hotspots,
                        base_url=settings.OLLAMA_BASE_URL,
                        model_name=_get_ollama_model_cached(),
                        timeout=float(settings.OLLAMA_TIMEOUT_SECONDS),
                    )
                    _llm_cache_set("insights:executive", insights)
                    logger.info("Pre-warm: leadership insights cached")
                else:
                    logger.info("Pre-warm: insights already cached, skipping Ollama call")
        except Exception as exc:
            logger.warning("Pre-warm insights failed: %s", exc)

    finally:
        with _prewarm_lock:
            _prewarm_running = False


# Function: _llm_cache_get
def _llm_cache_get(key: str) -> Any:
    with _llm_cache_lock:
        entry = _llm_cache.get(key)
        if entry and time.time() < entry["expires_at"]:
            return entry["data"]
    return None


# Function: _llm_cache_set
def _llm_cache_set(key: str, data: Any) -> None:
    with _llm_cache_lock:
        _llm_cache[key] = {"data": data, "expires_at": time.time() + _LLM_CACHE_TTL}


# Function: _rehydrate_synthetics
def _rehydrate_synthetics() -> None:
    """After any sync, re-inject synthetic incidents that ServiceNow doesn't have."""
    if not _invoked_critical_incidents or not cache.is_loaded:
        return
    try:
        existing_nums = set(cache.incidents_df["number"].astype(str).tolist())
        to_inject = [r for r in _invoked_critical_incidents if r["number"] not in existing_nums]
        if to_inject:
            cache.inject_rows(pd.DataFrame(to_inject))
            logger.info("Re-injected %d synthetic incidents after sync", len(to_inject))
    except Exception as exc:
        logger.warning("_rehydrate_synthetics failed: %s", exc)


# Function: _run_auto_sync
def _run_auto_sync() -> None:
    """Background scheduler job: re-sync from ServiceNow every N minutes."""
    if not (settings.SERVICENOW_BASE_URL and settings.SERVICENOW_USERNAME):
        return
    try:
        logger.info("Auto-sync: starting scheduled ServiceNow refresh…")
        client = ServiceNowClient()
        test = client.test_connection()
        if not test.get("success"):
            logger.warning("Auto-sync: SN connection test failed — skipping")
            return
        cache.load_from_servicenow(client)
        _rehydrate_synthetics()
        logger.info("Auto-sync: completed successfully")
    except Exception as exc:
        logger.warning("Auto-sync: failed — %s", exc)


# ---------------------------------------------------------------------------
# Application lifecycle
# ---------------------------------------------------------------------------

# Function: lifespan
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: restore persisted alerts, load data, start auto-sync scheduler."""
    # 0. Hydrate ServiceNow connection settings from the encrypted DB store so a
    # restart picks up the last value saved via the Connection panel, not stale
    # .env values. Mutates the settings singleton in place so every existing
    # consumer (sn_client.py, auto-sync, /api/status, …) sees the DB-backed value.
    try:
        persisted = settings_store.get_servicenow_config()
        settings.SERVICENOW_BASE_URL = persisted["url"]
        settings.SERVICENOW_USERNAME = persisted["username"]
        settings.SERVICENOW_PASSWORD = persisted["password"]
        settings.SERVICENOW_VERIFY_SSL = persisted["verify_ssl"]
    except Exception as exc:
        logger.warning("Failed to load persisted ServiceNow settings, using .env defaults: %s", exc)

    # 1. Restore persisted critical incidents from Qdrant
    try:
        persisted = alert_store.load_all()
        for rec in persisted:
            nums = {r["number"] for r in _invoked_critical_incidents}
            if rec.get("number") not in nums:
                _invoked_critical_incidents.append(rec)
        if persisted:
            logger.info("Restored %d critical incidents from Qdrant", len(persisted))
    except Exception as exc:
        logger.warning("Could not restore from Qdrant: %s", exc)

    # 2. Load data from XLSX fallback or ServiceNow
    if settings.XLSX_DATA_PATH and Path(settings.XLSX_DATA_PATH).exists():
        try:
            logger.info("Auto-loading XLSX fallback data from %s", settings.XLSX_DATA_PATH)
            cache.load_from_xlsx(Path(settings.XLSX_DATA_PATH))
            _rehydrate_synthetics()
            # Prewarm LLM cache after data is ready so first user gets a cache hit
            _threading.Thread(target=_background_prewarm_llm_cache, daemon=True, name="llm-prewarm").start()
        except Exception as exc:
            logger.warning("Auto-load from XLSX failed: %s", exc)
    elif settings.SERVICENOW_BASE_URL and settings.SERVICENOW_USERNAME:
        logger.info("ServiceNow credentials detected — running initial sync…")
        try:
            _run_auto_sync()
        except Exception as exc:
            logger.warning("Initial auto-sync failed: %s", exc)

    # 3. Start background auto-sync scheduler
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler(daemon=True)
        scheduler.add_job(
            _run_auto_sync,
            trigger="interval",
            minutes=settings.AUTO_SYNC_INTERVAL_MINUTES,
            id="auto_sync",
            max_instances=1,
            coalesce=True,
        )
        scheduler.start()
        logger.info(
            "Auto-sync scheduler started — interval: %d min",
            settings.AUTO_SYNC_INTERVAL_MINUTES,
        )
        app.state.scheduler = scheduler
    except Exception as exc:
        logger.warning("Could not start auto-sync scheduler: %s", exc)

    yield

    # Shutdown scheduler on exit
    try:
        if hasattr(app.state, "scheduler"):
            app.state.scheduler.shutdown(wait=False)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# App instance
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Digital Operations Cockpit API",
    description="AI-Powered ITSM analytics dashboard for ServiceNow data.",
    version="1.0.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------

_configured_origins = [o.strip() for o in (settings.CORS_ORIGINS or "").split(",") if o.strip()]
_required_public_origins = [
    "https://stratapp.org",
    "https://www.stratapp.org",
    "https://api.stratapp.org",
]

ALLOWED_ORIGINS = [
    *_configured_origins,
    *_required_public_origins,
    *[f"http://localhost:{port}" for port in range(3000, 5201)],
    *[f"http://127.0.0.1:{port}" for port in range(3000, 5201)],
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=r"https://.*\.service-now\.com",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_PUBLIC_PATHS = {"/", "/api/health", "/docs", "/openapi.json", "/redoc"}


# Function: _origin_allowed
def _origin_allowed(origin: Optional[str]) -> bool:
    if not origin:
        return False
    if origin in ALLOWED_ORIGINS:
        return True
    return re.match(r"^https://.*\.service-now\.com$", origin) is not None


# Function: _cors_json_response
def _cors_json_response(request: Request, payload: Dict[str, Any], status_code: int) -> JSONResponse:
    response = JSONResponse(payload, status_code=status_code)
    origin = request.headers.get("origin")
    if _origin_allowed(origin):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Vary"] = "Origin"
    return response


# Function: enforce_auth
@app.middleware("http")
async def enforce_auth(request: Request, call_next):
    path = request.url.path
    if (
        request.method == "OPTIONS"
        or not auth_required()
        or not path.startswith("/api")
        or path in _PUBLIC_PATHS
        or path.startswith("/api/render/")
    ):
        return await call_next(request)
    token = extract_bearer_token(request.headers.get("Authorization", ""))
    if not token:
        return _cors_json_response(request, {"error": "Authentication required"}, status_code=401)
    try:
        payload = decode_access_token(token)
    except ValueError as exc:
        return _cors_json_response(request, {"error": str(exc)}, status_code=401)
    if payload.get("role") != "admin" and DASHBOARD_APP not in (payload.get("apps") or []):
        return _cors_json_response(request, {"error": "Access denied for Dashboard"}, status_code=403)
    request.state.auth = payload
    return await call_next(request)

# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------

class ConnectRequest(BaseModel):
    url: str
    username: str
    password: str = ""
    encrypted_password: Optional[str] = None
    verify_ssl: bool = True


# ---------------------------------------------------------------------------
# Dependency helpers
# ---------------------------------------------------------------------------

# Function: require_data
def require_data() -> None:
    """Raise 503 if the data cache has not been loaded yet."""
    if not cache.is_loaded:
        raise HTTPException(
            status_code=503,
            detail="Data not synced. Call POST /api/sync first.",
        )


# Function: _filter_by_date
def _filter_by_date(
    df: pd.DataFrame,
    start_date: Optional[str],
    end_date: Optional[str],
    date_col: str = "opened_at",
) -> pd.DataFrame:
    """Return df filtered to [start_date, end_date] UTC range (both ends inclusive)."""
    if df.empty or (not start_date and not end_date):
        return df
    if date_col not in df.columns:
        return df
    try:
        col = pd.to_datetime(df[date_col], utc=True, errors="coerce")
    except Exception:
        return df
    mask = pd.Series(True, index=df.index)
    if start_date:
        try:
            sd = pd.Timestamp(start_date, tz="UTC")
            mask &= col >= sd
        except Exception as exc:
            logger.warning("Date filter start_date=%s error: %s", start_date, exc)
    if end_date:
        try:
            ed = pd.Timestamp(end_date, tz="UTC") + pd.Timedelta(days=1)
            mask &= col < ed
        except Exception as exc:
            logger.warning("Date filter end_date=%s error: %s", end_date, exc)
    return df[mask]


# Function: _servicenow_error_status
def _servicenow_error_status(result: Dict[str, Any]) -> int:
    """Map a failed test_connection() result to the outgoing HTTP status.

    ServiceNow auth rejections (401/403) mean the credentials are wrong or
    the instance is dormant — that's a client-correctable 401, not a gateway
    failure. Anything else (network unreachable, timeout, non-JSON/hibernating
    instance response) is a genuine upstream problem, so 502 stays accurate.
    """
    upstream_status = result.get("status_code")
    if upstream_status in (401, 403):
        return 401
    if upstream_status == 503:
        return 503
    if upstream_status in (408, 504):
        return 504
    message = str(result.get("message") or "").lower()
    if "timed out" in message or "timeout" in message:
        return 504
    if "non-json response" in message or "hibernating" in message or "html login page" in message:
        return 503
    return 502


# Function: _get_client
def _get_client(req: Optional[ConnectRequest] = None) -> ServiceNowClient:
    if req:
        return ServiceNowClient(
            base_url=req.url,
            username=req.username,
            password=req.password,
            verify_ssl=req.verify_ssl,
        )
    session_client, _ = _get_connection_session()
    return session_client or ServiceNowClient()


# Function: _resolve_connect_request
def _resolve_connect_request(req: ConnectRequest) -> ConnectRequest:
    """Decrypt a UI credential envelope without ever returning plaintext to the browser."""
    password = req.password
    if req.encrypted_password:
        try:
            password = decrypt_value(req.encrypted_password) or ""
        except RuntimeError as exc:
            raise HTTPException(
                status_code=400,
                detail="The encrypted ServiceNow credential is invalid or expired. Refresh and try again.",
            ) from exc

    url = req.url.strip()
    username = req.username.strip()
    if not url or not username or not password:
        raise HTTPException(status_code=400, detail="ServiceNow URL, username, and password are required.")

    return ConnectRequest(
        url=url,
        username=username,
        password=password,
        encrypted_password=None,
        verify_ssl=req.verify_ssl,
    )


# Function: _public_servicenow_config
def _public_servicenow_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Convert the server-side plaintext password into an encrypted UI envelope."""
    password = str(config.get("password") or "")
    try:
        encrypted_password = encrypt_value(password) or ""
    except RuntimeError as exc:
        logger.error("ServiceNow credential envelope encryption is unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Credential encryption is unavailable.") from exc

    return {
        "url": str(config.get("url") or ""),
        "username": str(config.get("username") or ""),
        "password": "",
        "encrypted_password": encrypted_password,
        "verify_ssl": bool(config.get("verify_ssl", True)),
    }


# ---------------------------------------------------------------------------
# Connection routes
# ---------------------------------------------------------------------------

# Function: get_default_config
@app.get("/api/config")
def get_default_config() -> Dict[str, Any]:
    """Return connection settings with the password encrypted for UI pre-population."""
    try:
        config = settings_store.get_servicenow_config()
    except Exception as exc:
        # DB unreachable (e.g. POSTGRES_DSN not yet configured with a real password) —
        # fall back to whatever the in-memory settings singleton currently holds
        # (itself already .env-seeded) instead of a raw 500 on every page load.
        logger.warning("Falling back to in-memory ServiceNow config, DB read failed: %s", exc)
        config = {
            "url": settings.SERVICENOW_BASE_URL or "",
            "username": settings.SERVICENOW_USERNAME or "",
            "password": settings.SERVICENOW_PASSWORD or "",
            "verify_ssl": settings.SERVICENOW_VERIFY_SSL,
        }
    return _public_servicenow_config(config)


# Function: get_status
@app.get("/api/status")
def get_status() -> Dict[str, Any]:
    """Return connection and sync status."""
    synced_at = cache.last_synced_at
    session_client, session_expires_at = _get_connection_session()
    return {
        "connected": session_client is not None,
        "synced": cache.is_loaded,
        "connection_expires_at": (
            datetime.fromtimestamp(session_expires_at, tz=timezone.utc).isoformat()
            if session_expires_at else None
        ),
        "connection_ttl_seconds": max(0, int(session_expires_at - time.time())),
        "last_synced": synced_at.isoformat() if synced_at else None,
        "record_counts": cache.record_counts,
        "servicenow_url": settings.SERVICENOW_BASE_URL or None,
        "ollama": {
            "enabled": settings.OLLAMA_ENABLED,
            "base_url": settings.OLLAMA_BASE_URL,
            "configured_model": settings.OLLAMA_MODEL,
            "selected_model": _get_ollama_model_cached(),
        },
    }


# Function: connect
@app.post("/api/connect")
def connect(req: ConnectRequest) -> Dict[str, Any]:
    """Test connectivity to ServiceNow without loading data."""
    req = _resolve_connect_request(req)
    client = _get_client(req)
    result = client.test_connection()
    if not result.get("success"):
        logger.warning(
            "ServiceNow connect failed for %s: %s",
            req.url,
            result.get("message", "unknown error"),
        )
        raise HTTPException(
            status_code=_servicenow_error_status(result),
            detail=result.get("message", "ServiceNow connection failed."),
        )
    expires_at = _set_connection_session(client)

    # Persist the successful connection (encrypted) so it survives a restart and
    # keep the in-memory settings singleton in sync for other consumers (e.g. the
    # default ServiceNowClient() constructor, auto-sync scheduler).
    try:
        settings_store.save_servicenow_config(
            url=req.url, username=req.username, password=req.password, verify_ssl=req.verify_ssl
        )
        settings.SERVICENOW_BASE_URL = req.url
        settings.SERVICENOW_USERNAME = req.username
        settings.SERVICENOW_PASSWORD = req.password
        settings.SERVICENOW_VERIFY_SSL = req.verify_ssl
    except Exception as exc:
        logger.warning("Failed to persist ServiceNow connection settings: %s", exc)

    return {
        **result,
        "connection_expires_at": datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat(),
        "connection_ttl_seconds": _CONNECTION_SESSION_TTL,
    }


# Function: sync_data
@app.post("/api/sync")
def sync_data(req: Optional[ConnectRequest] = None) -> Dict[str, Any]:
    """
    Pull all ITSM data from ServiceNow into the in-memory cache.
    Optionally accepts explicit credentials; falls back to .env values.
    """
    if req is not None:
        req = _resolve_connect_request(req)
    client = _get_client(req)

    # Verify connectivity first
    test = client.test_connection()
    if not test.get("success"):
        raise HTTPException(
            status_code=_servicenow_error_status(test),
            detail=f"ServiceNow connection failed: {test.get('message')}",
        )

    if req is not None:
        existing_session, _ = _get_connection_session()
        if existing_session is None:
            _set_connection_session(client)

    try:
        cache.load_from_servicenow(client)
    except Exception as exc:
        logger.exception("Data sync failed")
        raise HTTPException(status_code=500, detail=f"Sync error: {exc}") from exc

    # Invalidate LLM cache then immediately kick off background pre-warm
    with _llm_cache_lock:
        _llm_cache.clear()
    logger.info("LLM cache cleared after data sync")
    _threading.Thread(target=_background_prewarm_llm_cache, daemon=True, name="llm-prewarm").start()

    # Re-inject synthetics that SN does not have (e.g. mock invoked incidents)
    _rehydrate_synthetics()

    synced_at = cache.last_synced_at
    return {
        "success": True,
        "message": "Data synchronisation complete.",
        "record_counts": cache.record_counts,
        "synced_at": synced_at.isoformat() if synced_at else None,
    }


# Function: disconnect
@app.delete("/api/disconnect")
def disconnect() -> Dict[str, Any]:
    """Clear the in-memory cache."""
    _clear_connection_session()
    cache.clear()
    return {"success": True, "message": "Cache cleared. Data disconnected."}


# ---------------------------------------------------------------------------
# Dashboard data routes
# ---------------------------------------------------------------------------

# Function: get_kpis
@app.get("/api/kpis")
def get_kpis(
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    require_data()
    date_filtered = start_date or end_date
    if not date_filtered:
        with _kpi_cache_lock:
            entry = _kpi_cache.get("kpis:summary")
            if entry and time.time() < entry["expires_at"]:
                return entry["data"]
    inc = _filter_by_date(cache.incidents_df, start_date, end_date)
    chg = _filter_by_date(cache.changes_df, start_date, end_date)
    sr = _filter_by_date(cache.service_requests_df, start_date, end_date)
    result = kpi_engine.summary_kpis(inc, chg, sr)
    try:
        candidates = score_automation_candidates(inc, chg, sr, top_n=10)
        if candidates:
            avg_score = sum(asdict(c)["total_score"] for c in candidates) / len(candidates)
            result["automation_score"] = round(avg_score, 1)
        else:
            result["automation_score"] = 0.0
    except Exception as _exc:
        logger.warning("automation_score computation failed: %s", _exc)
        result["automation_score"] = None
    if not date_filtered:
        with _kpi_cache_lock:
            _kpi_cache["kpis:summary"] = {"data": result, "expires_at": time.time() + _KPI_CACHE_TTL}
    return result


# Function: get_monthly_volume
@app.get("/api/monthly-volume")
def get_monthly_volume(
    months: int = Query(default=12, ge=1, le=36),
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
) -> List[Dict[str, Any]]:
    require_data()
    inc = _filter_by_date(cache.incidents_df, start_date, end_date)
    chg = _filter_by_date(cache.changes_df, start_date, end_date)
    sr = _filter_by_date(cache.service_requests_df, start_date, end_date)
    return kpi_engine.monthly_volume(inc, chg, sr, months=months)


# Function: get_cycle_time
@app.get("/api/cycle-time")
def get_cycle_time(
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    require_data()
    inc = _filter_by_date(cache.incidents_df, start_date, end_date)
    chg = _filter_by_date(cache.changes_df, start_date, end_date)
    sr = _filter_by_date(cache.service_requests_df, start_date, end_date)
    return {
        "incidents": kpi_engine.cycle_time_stats(inc, "resolution_hours"),
        "changes": kpi_engine.cycle_time_stats(chg, "implementation_hours"),
        "service_requests": kpi_engine.cycle_time_stats(sr, "closure_hours"),
    }


# Function: get_incidents
@app.get("/api/incidents")
def get_incidents(
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    require_data()
    inc_df = _filter_by_date(cache.incidents_df, start_date, end_date)
    return {
        "summary": kpi_engine.summary_kpis(inc_df, pd.DataFrame(), pd.DataFrame()),
        "mttr_trend": kpi_engine.incident_mttr_trend(inc_df, months=12),
        "priority_dist": kpi_engine.priority_distribution(inc_df),
        "hotspots": kpi_engine.application_hotspots(inc_df, top_n=10),
        "ageing": kpi_engine.ageing_analysis(inc_df),
        "cycle_time": kpi_engine.cycle_time_stats(inc_df, "resolution_hours"),
    }


# Function: get_changes
@app.get("/api/changes")
def get_changes(
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    require_data()
    chg_df = _filter_by_date(cache.changes_df, start_date, end_date)
    return {
        "summary": kpi_engine.change_risk_summary(chg_df),
        "volume_trend": kpi_engine.change_volume_trend(chg_df, months=12),
        "risk": kpi_engine.change_risk_summary(chg_df),
        "priority_dist": kpi_engine.priority_distribution(chg_df),
        "ageing": kpi_engine.ageing_analysis(chg_df),
        "cycle_time": kpi_engine.cycle_time_stats(chg_df, "implementation_hours"),
    }


# Function: get_service_requests
@app.get("/api/service-requests")
def get_service_requests(
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    require_data()
    sr_df = _filter_by_date(cache.service_requests_df, start_date, end_date)
    return {
        "summary": kpi_engine.sr_inquiry_summary(sr_df),
        "ageing": kpi_engine.ageing_analysis(sr_df),
        "priority_dist": kpi_engine.priority_distribution(sr_df),
        "cycle_time": kpi_engine.cycle_time_stats(sr_df, "closure_hours"),
    }


# Function: get_application_hotspots
@app.get("/api/application-hotspots")
def get_application_hotspots(
    top_n: int = Query(default=10, ge=1, le=50),
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
) -> List[Dict[str, Any]]:
    require_data()
    inc = _filter_by_date(cache.incidents_df, start_date, end_date)
    return kpi_engine.application_hotspots(inc, top_n=top_n)


# Function: get_assignment_group_hotspots
@app.get("/api/assignment-group-hotspots")
def get_assignment_group_hotspots(
    top_n: int = Query(default=10, ge=1, le=50),
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
) -> List[Dict[str, Any]]:
    require_data()
    inc = _filter_by_date(cache.incidents_df, start_date, end_date)
    chg = _filter_by_date(cache.changes_df, start_date, end_date)
    sr = _filter_by_date(cache.service_requests_df, start_date, end_date)
    return kpi_engine.assignment_group_hotspots(inc, chg, sr, top_n=top_n)


# Function: _build_heuristic_automation_records
def _build_heuristic_automation_records(candidates: List[Any]) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for i, c in enumerate(candidates):
        row = asdict(c)
        row["_candidate_id"] = i
        # Frontend aliases + enriched display fields
        row["volume"] = row.get("ticket_count", 0)
        row["avg_cycle_time"] = row.get("avg_cycle_time_hours", 0)
        row["est_hours_saved"] = row.get("estimated_hours_saved_monthly", 0)
        row["analysis_source"] = "heuristic"
        row["llm_opportunity_score"] = row.get("total_score", 0)
        row["llm_confidence"] = 55.0
        row["llm_automation_type"] = "Workflow"
        row["llm_risk_level"] = "Medium"
        row["llm_rationale"] = (
            f"Heuristic estimate from volume={row.get('ticket_count', 0)}, "
            f"repetition={row.get('repetition_score', 0):.2f}, cycle={row.get('avg_cycle_time_hours', 0):.1f}h."
        )
        row["llm_next_step"] = "Build pilot runbook/workflow for top recurring path and validate ROI in 2 weeks."
        records.append(row)
    return records


# Function: _get_or_fetch_automation_enrichment
def _get_or_fetch_automation_enrichment(
    records: List[Dict[str, Any]], top_n: int, gpu_required: bool
) -> Dict[str, Any]:
    # Cache key excludes gpu_required — enrichment predictions are identical regardless.
    # The GPU constraint is enforced inside enrich_automation_candidates_ollama (preflight).
    cache_key = f"automation_enriched:{top_n}"
    enriched = _llm_cache_get(cache_key)
    if enriched is not None:
        logger.info("LLM cache hit for automation candidates (top_n=%d)", top_n)
        return enriched

    logger.info("LLM cache miss for automation candidates (top_n=%d) — calling Ollama", top_n)
    enriched = enrich_automation_candidates_ollama(
        records,
        base_url=settings.OLLAMA_BASE_URL,
        model=settings.OLLAMA_MODEL,
        timeout=float(settings.OLLAMA_TIMEOUT_SECONDS),
        require_gpu=gpu_required,
    )
    if enriched:
        _llm_cache_set(cache_key, enriched)
    return enriched


# Function: _apply_automation_enrichment
def _apply_automation_enrichment(records: List[Dict[str, Any]], enriched: Dict[str, Any]) -> None:
    for row in records:
        key_by_id = build_automation_id_key(row.get("_candidate_id", -1))
        key = build_automation_key(
            str(row.get("category", "Unknown")),
            str(row.get("work_type", "Unknown")),
        )
        if key_by_id in enriched:
            row.update(enriched[key_by_id])
        elif key in enriched:
            row.update(enriched[key])


# Function: get_automation_candidates
@app.get("/api/automation-candidates")
def get_automation_candidates(
    top_n: int = Query(default=20, ge=1, le=100),
    deep_analysis: bool = Query(default=True),
    use_ollama: bool = Query(default=True),
    llm_only: bool = Query(default=False),
    gpu_required: bool = Query(default=False),
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
) -> List[Dict[str, Any]]:
    require_data()
    inc = _filter_by_date(cache.incidents_df, start_date, end_date)
    chg = _filter_by_date(cache.changes_df, start_date, end_date)
    sr = _filter_by_date(cache.service_requests_df, start_date, end_date)
    candidates = score_automation_candidates(inc, chg, sr, top_n=top_n)
    records = _build_heuristic_automation_records(candidates)

    # Skip LLM enrichment when date-filtered (cache is keyed on full-dataset top_n only).
    if deep_analysis and use_ollama and settings.OLLAMA_ENABLED and not (start_date or end_date):
        enriched = _get_or_fetch_automation_enrichment(records, top_n, gpu_required)
        if enriched:
            _apply_automation_enrichment(records, enriched)

    if llm_only:
        llm_count = sum(1 for row in records if row.get("analysis_source") == "ollama")
        if llm_count == 0:
            raise HTTPException(
                status_code=503,
                detail=(
                    "LLM GPU enrichment unavailable — Ollama GPU model not loaded or GPU preflight failed. "
                    "Ensure qwen2.5:7b is running on GPU and retry."
                ),
            )

    # Re-rank by deep prediction when available
    records.sort(
        key=lambda r: (float(r.get("llm_opportunity_score", 0)), float(r.get("total_score", 0))),
        reverse=True,
    )

    for row in records:
        row.pop("_candidate_id", None)

    return records


# Function: get_insights
@app.get("/api/insights")
def get_insights(
    quick: bool = Query(default=False),
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    """
    Generate AI-powered leadership insights using Ollama LLM (GPU).
    quick=true: return rule-based insights immediately and warm LLM cache in background.
    quick=false (default): block until LLM result is ready (uses cache if warm).
    Falls back to rule-based insights when Ollama is unavailable.
    When date range is active, returns rule-based insights (LLM cache is full-dataset only).
    """
    require_data()
    if start_date or end_date:
        inc = _filter_by_date(cache.incidents_df, start_date, end_date)
        chg = _filter_by_date(cache.changes_df, start_date, end_date)
        sr = _filter_by_date(cache.service_requests_df, start_date, end_date)
        summary = kpi_engine.summary_kpis(inc, chg, sr)
        incident_data = {"summary": summary, "cycle_time": kpi_engine.cycle_time_stats(inc, "resolution_hours"), "priority_dist": kpi_engine.priority_distribution(inc)}
        change_data = {"risk": kpi_engine.change_risk_summary(chg)}
        sr_data = {"summary": kpi_engine.sr_inquiry_summary(sr)}
        hotspots = kpi_engine.application_hotspots(inc, top_n=5)
        return kpi_engine.generate_leadership_insights(summary, incident_data, change_data, sr_data, hotspots)
    summary, incident_data, change_data, sr_data, hotspots = _build_insights_kpi_data()

    if settings.OLLAMA_ENABLED:
        _insights_cache_key = "insights:executive"
        cached_insights = _llm_cache_get(_insights_cache_key)
        if cached_insights is not None:
            logger.info("LLM cache hit for insights")
            return cached_insights

        if quick:
            # Return rule-based immediately; kick off LLM generation in background
            _threading.Thread(
                target=_generate_insights_background,
                args=(summary, incident_data, change_data, sr_data, hotspots),
                daemon=True,
                name="insights-bg",
            ).start()
            logger.info("Quick insights requested — returning heuristic, LLM warming in background")
            return kpi_engine.generate_leadership_insights(summary, incident_data, change_data, sr_data, hotspots)

        # Full path: block until LLM result is ready (double-checked lock)
        logger.info("LLM cache miss for insights — waiting for insights lock")
        with _insights_generation_lock:
            cached_insights = _llm_cache_get(_insights_cache_key)
            if cached_insights is not None:
                logger.info("LLM cache hit for insights (after lock)")
                return cached_insights
            logger.info("Generating insights via Ollama")
            result = generate_insights_ollama(
                summary, incident_data, change_data, sr_data, hotspots,
                base_url=settings.OLLAMA_BASE_URL,
                model_name=_get_ollama_model_cached(),
                timeout=float(settings.OLLAMA_TIMEOUT_SECONDS),
            )
            _llm_cache_set(_insights_cache_key, result)
            return result

    return kpi_engine.generate_leadership_insights(summary, incident_data, change_data, sr_data, hotspots)


# ---------------------------------------------------------------------------
# New Advanced Dashboards - Repeat Incidents, RCA, Transformation KPIs
# ---------------------------------------------------------------------------

# Function: get_repeat_incidents
@app.get("/api/repeat-incidents")
def get_repeat_incidents(
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    """Analyze repeat incidents and recurring problems."""
    require_data()
    inc = _filter_by_date(cache.incidents_df, start_date, end_date)
    return kpi_engine.repeat_incidents_analysis(inc)


# Function: get_rca_ownership
@app.get("/api/rca-ownership")
def get_rca_ownership(
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    """Root Cause Analysis and ownership metrics."""
    require_data()
    inc = _filter_by_date(cache.incidents_df, start_date, end_date)
    return kpi_engine.rca_ownership_analysis(inc)


# Function: get_preventive_maintenance
@app.get("/api/preventive-maintenance")
def get_preventive_maintenance(
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    """Planned vs executed maintenance and preventive action analysis."""
    require_data()
    chg = _filter_by_date(cache.changes_df, start_date, end_date)
    return kpi_engine.preventive_maintenance_analysis(chg)


# Function: get_adhoc_vs_bau
@app.get("/api/adhoc-vs-bau")
def get_adhoc_vs_bau(
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    """Ad-hoc work vs BAU analysis with aging alerts."""
    require_data()
    sr = _filter_by_date(cache.service_requests_df, start_date, end_date)
    inc = _filter_by_date(cache.incidents_df, start_date, end_date)
    return kpi_engine.adhoc_vs_bau_analysis(sr, inc)


# Function: get_sla_breach_risk
@app.get("/api/sla-breach-risk")
def get_sla_breach_risk(
    months: int = Query(default=12, ge=1, le=36),
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    """SLA breach trend analysis and risk indicators."""
    require_data()
    inc = _filter_by_date(cache.incidents_df, start_date, end_date)
    sr = _filter_by_date(cache.service_requests_df, start_date, end_date)
    return kpi_engine.sla_breach_risk_analysis(inc, sr, months=months)


# Function: get_transformation_kpis
@app.get("/api/transformation-kpis")
def get_transformation_kpis(
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    """Transformation metrics: automation %, effort reduction, incident deflection."""
    require_data()
    inc = _filter_by_date(cache.incidents_df, start_date, end_date)
    chg = _filter_by_date(cache.changes_df, start_date, end_date)
    sr = _filter_by_date(cache.service_requests_df, start_date, end_date)
    return kpi_engine.transformation_kpis(inc, chg, sr)


# Function: get_people_capacity
@app.get("/api/people-capacity")
def get_people_capacity(
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    """People and capacity metrics including team workload distribution."""
    require_data()
    inc = _filter_by_date(cache.incidents_df, start_date, end_date)
    chg = _filter_by_date(cache.changes_df, start_date, end_date)
    sr = _filter_by_date(cache.service_requests_df, start_date, end_date)
    return kpi_engine.people_capacity_metrics(inc, chg, sr)


# ---------------------------------------------------------------------------
# Critical incident invocation & alert routes
# ---------------------------------------------------------------------------

class InvokeRequest(BaseModel):
    short_description: str = "CRITICAL: Production system outage detected — immediate action required"
    description: str = (
        "A high-severity production incident has been automatically raised via the Digital Operations Cockpit. "
        "All critical systems must be assessed immediately. Escalate to on-call team."
    )


# Function: invoke_critical_incident
@app.post("/api/invoke-critical-incident")
def invoke_critical_incident(req: InvokeRequest = InvokeRequest()) -> Dict[str, Any]:
    """
    Create a P1/Critical incident in ServiceNow (when connected) and persist it
    to Qdrant so it survives backend restarts.  The record is also injected into
    the in-memory cache immediately so the dashboard reflects it without a sync.
    """
    synthetic_number = f"INC{str(uuid.uuid4().int)[:7]}"
    now_iso = datetime.now(tz=timezone.utc).isoformat()

    # Attempt to create the incident in ServiceNow
    sn_result: Dict[str, Any] = {"success": False}
    if settings.SERVICENOW_BASE_URL and settings.SERVICENOW_USERNAME:
        try:
            client = _get_client()
            sn_result = client.create_critical_incident(
                req.short_description,
                req.description,
                requested_number=synthetic_number,
            )
            if sn_result.get("number"):
                synthetic_number = sn_result["number"]
        except Exception as exc:
            logger.warning("ServiceNow create failed, injecting synthetic record: %s", exc)

    incident: Dict[str, Any] = {
        "number": synthetic_number,
        "short_description": req.short_description,
        "description": req.description,
        "priority": "1",
        "impact": "1",
        "urgency": "1",
        "state": "1",
        "category": "software",
        "opened_at": now_iso,
        "resolved_at": None,
        "closed_at": None,
        "made_sla": "false",
        "reopen_count": "0",
        "reassignment_count": "0",
        "_synthetic": True,
    }

    # Persist to Qdrant (survives restarts)
    alert_store.upsert(incident)

    # Add to in-memory list (deduplicated by number)
    existing_nums = {r["number"] for r in _invoked_critical_incidents}
    if incident["number"] not in existing_nums:
        _invoked_critical_incidents.append(incident)

    # Inject into live cache immediately so count updates without waiting for sync
    if cache.is_loaded:
        try:
            cache.inject_rows(pd.DataFrame([incident]))
            logger.info("Injected critical incident %s into live cache", synthetic_number)
        except Exception as exc:
            logger.warning("Cache inject failed: %s", exc)

    return {
        "success": True,
        "number": synthetic_number,
        "servicenow_created": sn_result.get("success", False),
        "message": f"Critical incident {synthetic_number} raised successfully.",
        "incident": incident,
    }


# Function: _filter_recent_open_high_priority
def _filter_recent_open_high_priority(inc_df: pd.DataFrame, cutoff: datetime) -> pd.DataFrame:
    if "priority" not in inc_df.columns:
        return inc_df.iloc[0:0]

    p1_mask = inc_df["priority"].astype(str).str.startswith("1")
    p2_mask = inc_df["priority"].astype(str).str.startswith("2")
    filtered = inc_df[p1_mask | p2_mask].copy()

    # Keep only open (not resolved/closed)
    if "state" in filtered.columns:
        open_mask = ~filtered["state"].astype(str).str.lower().str.contains(
            r"resolved|closed|6|7|8", regex=True
        )
        filtered = filtered[open_mask]

    # Keep only recently opened (last 48 h) OR synthetic
    if "opened_at" in filtered.columns:
        try:
            opened = pd.to_datetime(filtered["opened_at"], utc=True, errors="coerce")
            recent_mask = (opened >= cutoff) | filtered.get("_synthetic", pd.Series(False, index=filtered.index)).fillna(False)
            filtered = filtered[recent_mask]
        except Exception:
            pass  # if date parsing fails, keep all

    return filtered


# Function: _cached_critical_alerts
def _cached_critical_alerts(cutoff: datetime) -> List[Dict[str, Any]]:
    """Pull recent open P1/P2 incidents from the live cache."""
    if not cache.is_loaded:
        return []
    try:
        filtered = _filter_recent_open_high_priority(cache.incidents_df.copy(), cutoff)
        return [
            {
                "number": str(row.get("number", "")),
                "short_description": str(row.get("short_description", "")),
                "priority": str(row.get("priority", "1")),
                "opened_at": str(row.get("opened_at", "")),
                "synthetic": bool(row.get("_synthetic", False)),
            }
            for _, row in filtered.head(30).iterrows()
        ]
    except Exception as exc:
        logger.warning("Error computing critical alerts from cache: %s", exc)
        return []


# Function: _invoked_incident_alerts
def _invoked_incident_alerts(existing_numbers: set) -> List[Dict[str, Any]]:
    """Invoked incidents (Qdrant-persisted + in-memory) not already present."""
    alerts = []
    for rec in _invoked_critical_incidents:
        if str(rec.get("number", "")) not in existing_numbers:
            alerts.append({
                "number": str(rec.get("number", "")),
                "short_description": str(rec.get("short_description", "")),
                "priority": str(rec.get("priority", "1")),
                "opened_at": str(rec.get("opened_at", "")),
                "synthetic": True,
            })
    return alerts


# Function: get_critical_alerts
@app.get("/api/critical-alerts")
def get_critical_alerts() -> List[Dict[str, Any]]:
    """
    Return currently-open P1/Critical and P2/High incidents for the rolling alert
    ticker.  Shows:
      • Invoked incidents (from Invoke button + Qdrant-persisted)
      • Open P1/P2 incidents opened in the last 48 hours from the live cache
    """
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=48)
    alerts = _cached_critical_alerts(cutoff)
    alerts.extend(_invoked_incident_alerts({a["number"] for a in alerts}))
    return alerts


# ---------------------------------------------------------------------------
# Chart rendering routes
# ---------------------------------------------------------------------------

# Function: _png_response
def _png_response(data: bytes) -> Response:
    return Response(content=data, media_type="image/png")


# Function: render_executive_overview
@app.get("/render/executive-overview.png", response_class=Response)
@app.get("/api/render/executive-overview.png", response_class=Response)
def render_executive_overview():
    require_data()
    inc_df = cache.incidents_df
    chg_df = cache.changes_df
    sr_df = cache.service_requests_df

    summary = kpi_engine.summary_kpis(inc_df, chg_df, sr_df)
    volume = kpi_engine.monthly_volume(inc_df, chg_df, sr_df, months=12)
    hotspots = kpi_engine.application_hotspots(inc_df, top_n=8)
    mttr = kpi_engine.incident_mttr_trend(inc_df, months=12)

    img = charts.render_executive_montage(summary, volume, hotspots, mttr)
    return _png_response(img)


# Function: render_monthly_volume_chart
@app.get("/render/monthly-volume.png", response_class=Response)
@app.get("/api/render/monthly-volume.png", response_class=Response)
def render_monthly_volume_chart(months: int = Query(default=12, ge=1, le=36)):
    require_data()
    data = kpi_engine.monthly_volume(
        cache.incidents_df, cache.changes_df, cache.service_requests_df, months=months
    )
    img = charts.render_monthly_volume(data)
    return _png_response(img)


# Function: render_incident_mttr_chart
@app.get("/render/incident-mttr.png", response_class=Response)
@app.get("/api/render/incident-mttr.png", response_class=Response)
def render_incident_mttr_chart(months: int = Query(default=12, ge=1, le=36)):
    require_data()
    data = kpi_engine.incident_mttr_trend(cache.incidents_df, months=months)
    img = charts.render_incident_mttr(data)
    return _png_response(img)


# Function: render_change_risk_chart
@app.get("/render/change-risk.png", response_class=Response)
@app.get("/api/render/change-risk.png", response_class=Response)
def render_change_risk_chart():
    require_data()
    chg_df = cache.changes_df
    summary = kpi_engine.change_risk_summary(chg_df)
    volume = kpi_engine.change_volume_trend(chg_df, months=12)
    img = charts.render_change_risk(summary, volume)
    return _png_response(img)


# Function: render_automation_chart
@app.get("/render/automation-opportunities.png", response_class=Response)
@app.get("/api/render/automation-opportunities.png", response_class=Response)
def render_automation_chart(top_n: int = Query(default=20, ge=1, le=100)):
    require_data()
    candidates = score_automation_candidates(
        cache.incidents_df, cache.changes_df, cache.service_requests_df, top_n=top_n
    )
    img = charts.render_automation_quadrant(candidates)
    return _png_response(img)


# Function: render_application_hotspots_chart
@app.get("/render/application-hotspots.png", response_class=Response)
@app.get("/api/render/application-hotspots.png", response_class=Response)
def render_application_hotspots_chart(top_n: int = Query(default=10, ge=1, le=50)):
    require_data()
    data = kpi_engine.application_hotspots(cache.incidents_df, top_n=top_n)
    img = charts.render_application_hotspots(data, title=f"Top {top_n} Application Hotspots")
    return _png_response(img)


# Function: render_sr_productivity_chart
@app.get("/render/service-request-productivity.png", response_class=Response)
@app.get("/api/render/service-request-productivity.png", response_class=Response)
def render_sr_productivity_chart(months: int = Query(default=12, ge=1, le=36)):
    require_data()
    sr_df = cache.service_requests_df
    summary = kpi_engine.sr_inquiry_summary(sr_df)
    ageing = kpi_engine.ageing_analysis(sr_df)
    summary["ageing"] = ageing  # inject ageing into summary for the chart
    monthly = kpi_engine.monthly_volume(
        pd.DataFrame(), pd.DataFrame(), sr_df, months=months
    )
    img = charts.render_sr_productivity(summary, monthly)
    return _png_response(img)


# ---------------------------------------------------------------------------
# Prometheus metrics endpoint
# ---------------------------------------------------------------------------

# Function: prometheus_metrics
@app.get("/metrics")
def prometheus_metrics() -> Response:
    """
    Expose key application metrics in Prometheus text format.
    Metrics are computed from the current cache state.
    """
    lines: List[str] = []

    # Function: gauge
    def gauge(name: str, value: float, labels: str = "") -> None:
        full_name = f"ops_cockpit_{name}"
        lines.append(f"# TYPE {full_name} gauge")
        if labels:
            lines.append(f'{full_name}{{{labels}}} {value}')
        else:
            lines.append(f'{full_name} {value}')

    counts = cache.record_counts
    gauge("incident_count_total", counts.get("incidents", 0))
    gauge("change_count_total", counts.get("changes", 0))
    gauge("service_request_count_total", counts.get("service_requests", 0))
    gauge("cache_loaded", 1.0 if cache.is_loaded else 0.0)

    synced_at = cache.last_synced_at
    if synced_at:
        gauge("last_sync_timestamp_seconds", synced_at.timestamp())

    if cache.is_loaded:
        try:
            summary = kpi_engine.summary_kpis(
                cache.incidents_df, cache.changes_df, cache.service_requests_df
            )
            gauge("sla_compliance_pct", summary.get("sla_compliance_pct", 0))
            gauge("avg_mttr_hours", summary.get("avg_mttr_hours", 0))
            gauge("avg_cycle_time_hours", summary.get("avg_cycle_time_hours", 0))
            gauge("emergency_change_pct", summary.get("emergency_change_pct", 0))
            gauge("total_tickets", summary.get("total_tickets", 0))
        except Exception as exc:
            logger.warning("Error computing KPIs for /metrics: %s", exc)

    body = "\n".join(lines) + "\n"
    return Response(content=body, media_type="text/plain; version=0.0.4; charset=utf-8")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8087, reload=True)
