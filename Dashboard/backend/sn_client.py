# ---------------------------------------------------------------------------
# Author: Vishnuu A
# Scope: ServiceNow REST API client.
# Date: 2026-03-21
# ---------------------------------------------------------------------------
"""
ServiceNow REST API client.
Fetches Incidents, Changes, and Service Request Items via the Table API
using basic authentication and cursor-based pagination.
"""

import logging
import re
from typing import Any, Dict, List, Optional

import httpx

from config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Field lists
# ---------------------------------------------------------------------------

INCIDENT_FIELDS = ",".join([
    "number", "short_description", "state", "priority", "impact", "urgency",
    "category", "subcategory", "assignment_group", "assigned_to", "opened_at",
    "resolved_at", "closed_at", "cmdb_ci", "business_service",
    "u_application_name", "close_code", "reopen_count", "reassignment_count",
    "made_sla", "caller_id",
])

CHANGE_FIELDS = ",".join([
    "number", "short_description", "state", "type", "risk", "impact",
    "priority", "category", "assignment_group", "assigned_to", "opened_at",
    "start_date", "end_date", "actual_start", "actual_end", "closed_at",
    "cmdb_ci", "business_service", "u_application_name", "close_code",
    "review_status", "u_implementation_result", "u_validation_result",
    "u_change_scale",
])

SR_FIELDS = ",".join([
    "number", "short_description", "state", "cat_item", "u_request_category",
    "u_request_type", "u_application", "assignment_group", "assigned_to",
    "opened_at", "closed_at", "due_date", "cmdb_ci", "business_service",
    "made_sla", "priority", "urgency", "impact", "requested_for",
])

PAGE_SIZE = 1000  # records per API call


class ServiceNowClient:
    """Thin wrapper around the ServiceNow Table REST API."""

    # Function: __init__
    def __init__(
        self,
        base_url: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        verify_ssl: Optional[bool] = None,
        timeout_seconds: Optional[int] = None,
    ) -> None:
        self.base_url = (base_url or settings.SERVICENOW_BASE_URL).rstrip("/")
        self.username = username or settings.SERVICENOW_USERNAME
        self.password = password or settings.SERVICENOW_PASSWORD
        self.verify_ssl = verify_ssl if verify_ssl is not None else settings.SERVICENOW_VERIFY_SSL
        self.timeout = timeout_seconds or settings.SERVICENOW_TIMEOUT_SECONDS
        self._session_cookies: Optional[httpx.Cookies] = None
        self._session_user_token = ""

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    # Function: _build_client
    def _build_client(self) -> httpx.Client:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self._session_user_token:
            headers["X-UserToken"] = self._session_user_token
        return httpx.Client(
            auth=None if self._session_cookies else (self.username, self.password),
            cookies=self._session_cookies,
            verify=self.verify_ssl,
            timeout=self.timeout,
            headers=headers,
        )

    # Function: _authenticate_web_session
    def _authenticate_web_session(self) -> bool:
        """Establish a server-side ServiceNow UI session for instances that disable REST Basic auth."""
        login_url = f"{self.base_url}/login.do"
        try:
            with httpx.Client(
                verify=self.verify_ssl,
                timeout=self.timeout,
                follow_redirects=True,
                headers={"Accept": "text/html,application/xhtml+xml"},
            ) as client:
                response = client.post(
                    login_url,
                    data={
                        "user_name": self.username,
                        "user_password": self.password,
                        "sys_action": "sysverb_login",
                    },
                )
                response.raise_for_status()
                match = re.search(r"g_ck\s*=\s*['\"]([^'\"]+)['\"]", response.text or "")
                if not match or not client.cookies:
                    logger.warning("ServiceNow web-session fallback did not return a session token.")
                    return False
                self._session_cookies = httpx.Cookies(client.cookies)
                self._session_user_token = match.group(1)
                logger.info("ServiceNow web-session authentication fallback established.")
                return True
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("ServiceNow web-session authentication fallback failed: %s", exc)
            return False

    # Function: _table_url
    def _table_url(self, table: str) -> str:
        return f"{self.base_url}/api/now/table/{table}"

    # Function: _fetch_all
    def _fetch_all(
        self,
        table: str,
        fields: str,
        query: str = "",
        limit: int = 5000,
    ) -> List[Dict[str, Any]]:
        """
        Fetch all records from a ServiceNow table, paginating automatically.
        Returns a flat list of record dicts.
        """
        records: List[Dict[str, Any]] = []
        offset = 0
        url = self._table_url(table)

        with self._build_client() as client:
            while True:
                params: Dict[str, Any] = {
                    "sysparm_fields": fields,
                    "sysparm_display_value": "true",
                    "sysparm_limit": min(PAGE_SIZE, limit - len(records)),
                    "sysparm_offset": offset,
                    "sysparm_exclude_reference_link": "true",
                }
                if query:
                    params["sysparm_query"] = query

                try:
                    response = client.get(url, params=params)
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    logger.error(
                        "HTTP error fetching %s (offset=%d): %s",
                        table, offset, exc.response.text,
                    )
                    raise
                except httpx.RequestError as exc:
                    logger.error("Request error fetching %s: %s", table, exc)
                    raise

                batch = response.json().get("result", [])
                records.extend(batch)
                logger.info("Fetched %d/%d records from %s", len(records), limit, table)

                if len(batch) < PAGE_SIZE or len(records) >= limit:
                    break
                offset += PAGE_SIZE

        return records

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    # Function: test_connection
    def test_connection(self) -> Dict[str, Any]:
        """
        Ping ServiceNow by fetching a single incident record.
        Returns {success: bool, message: str}.
        """
        url = self._table_url("incident")

        def _probe_once(client: httpx.Client) -> Dict[str, Any]:
            response = client.get(
                url,
                params={
                    "sysparm_limit": 1,
                    "sysparm_fields": "number",
                    "sysparm_display_value": "true",
                },
            )
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            if "json" not in content_type.lower():
                body_preview = (response.text or "")[:240]
                lower_preview = body_preview.lower()
                hint = (
                    "The instance may be hibernating, unavailable, or redirecting to an HTML login page."
                )
                if "hibernate" in lower_preview or "wake" in lower_preview:
                    hint = "The ServiceNow instance appears to be hibernating and needs to wake up."
                return {
                    "success": False,
                    "status_code": 503,
                    "message": (
                        "ServiceNow returned a non-JSON response "
                        f"(HTTP {response.status_code}, content-type {content_type or 'unknown'}). "
                        f"{hint}"
                    ),
                }

            try:
                data = response.json()
            except ValueError:
                return {
                    "success": False,
                    "status_code": 502,
                    "message": "ServiceNow returned malformed JSON from the Table API.",
                }

            if "result" in data:
                return {
                    "success": True,
                    "message": f"Connected to {self.base_url} successfully.",
                }
            return {
                "success": False,
                "status_code": 502,
                "message": "Unexpected response format from ServiceNow.",
            }

        try:
            with self._build_client() as client:
                first = _probe_once(client)
                # ServiceNow dev instances can briefly return an HTML wake/login page
                # and then recover seconds later; a single immediate retry avoids false negatives.
                if first.get("success"):
                    return first
                if first.get("status_code") == 503:
                    second = _probe_once(client)
                    if second.get("success"):
                        return second
                    return second
                return first
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401 and self._authenticate_web_session():
                try:
                    with self._build_client() as client:
                        return _probe_once(client)
                except httpx.HTTPStatusError as session_exc:
                    exc = session_exc
            return {
                "success": False,
                "status_code": exc.response.status_code,
                "message": f"HTTP {exc.response.status_code}: {exc.response.text[:200]}",
            }
        except httpx.ReadTimeout as exc:
            return {
                "success": False,
                "status_code": 504,
                "message": (
                    "ServiceNow request timed out. The instance may be slow or asleep. "
                    f"Details: {exc}"
                ),
            }
        except httpx.RequestError as exc:
            return {
                "success": False,
                "message": f"Connection error ({exc.__class__.__name__}): {exc}",
            }

    # Function: fetch_incidents
    def fetch_incidents(self, limit: int = 5000) -> List[Dict[str, Any]]:
        """Fetch incident records from ServiceNow."""
        logger.info("Fetching up to %d incidents from ServiceNow…", limit)
        return self._fetch_all(
            table="incident",
            fields=INCIDENT_FIELDS,
            limit=limit,
        )

    # Function: fetch_changes
    def fetch_changes(self, limit: int = 5000) -> List[Dict[str, Any]]:
        """Fetch change request records from ServiceNow."""
        logger.info("Fetching up to %d change requests from ServiceNow…", limit)
        return self._fetch_all(
            table="change_request",
            fields=CHANGE_FIELDS,
            limit=limit,
        )

    # Function: fetch_service_requests
    def fetch_service_requests(self, limit: int = 5000) -> List[Dict[str, Any]]:
        """Fetch service-request-item records from ServiceNow."""
        logger.info("Fetching up to %d service request items from ServiceNow…", limit)
        return self._fetch_all(
            table="sc_req_item",
            fields=SR_FIELDS,
            limit=limit,
        )

    # Function: create_critical_incident
    def create_critical_incident(self, short_description: str, description: str) -> Dict[str, Any]:
        """
        Create a P1/Critical incident in ServiceNow via the Table API.
        Returns the created record or an error dict.
        """
        url = self._table_url("incident")
        payload = {
            "short_description": short_description,
            "description": description,
            "priority": "1",        # 1 = Critical
            "impact": "1",          # 1 = High
            "urgency": "1",         # 1 = High
            "state": "1",           # 1 = New
            "category": "software",
            "caller_id": self.username,
        }
        try:
            with self._build_client() as client:
                response = client.post(url, json=payload)
                response.raise_for_status()
                result = response.json().get("result", {})
                logger.info("Created critical incident: %s", result.get("number"))
                return {"success": True, "number": result.get("number"), "sys_id": result.get("sys_id")}
        except httpx.HTTPStatusError as exc:
            logger.error("Failed to create critical incident: %s", exc.response.text)
            return {"success": False, "message": f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"}
        except httpx.RequestError as exc:
            logger.error("Request error creating critical incident: %s", exc)
            return {"success": False, "message": f"Connection error: {exc}"}
