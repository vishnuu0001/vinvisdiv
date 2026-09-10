#!/usr/bin/env python
"""Idempotently import the historical incident workbook into ServiceNow.

Credentials are read from SERVICENOW_BASE_URL, SERVICENOW_USERNAME and
SERVICENOW_PASSWORD (or Novastra-ITSM/.env). They are never printed or written
to the checkpoint. Each source incident is keyed as ``NOVASTRA:<Number>`` in
ServiceNow's standard ``correlation_id`` field, making interrupted runs safe to
resume without creating duplicates.

Examples:
    python datapocessing/DataInsert.py --dry-run --max-rows 10
    python datapocessing/DataInsert.py
    python datapocessing/DataInsert.py --verify-only
"""
from __future__ import annotations

import argparse
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKBOOK = PROJECT_ROOT / "data" / "Closed incidents until 19April-26.xlsx"
DEFAULT_CHECKPOINT = Path(__file__).with_name("DataInsert.results.jsonl")
SOURCE_PREFIX = "NOVASTRA:"
DEFAULT_TABLE = "u_novastra_imported_incident"
MAX_IMPORT_ROWS = 1000
MAX_TEXT = 12000
JSON_CONTENT_TYPE = "application/json"
JSON_HEADERS = {"Accept": JSON_CONTENT_TYPE, "Content-Type": JSON_CONTENT_TYPE}

FIELD_MAP = {
    "short description": "u_short_description",
    "service offering": "u_service_offering",
    "assignment group": "u_assignment_group",
    "assigned to": "u_assigned_to",
    "on hold reason": "u_hold_reason",
    "external url": "u_external_url",
    "change request": "rfc",
    "configuration item": "u_cmdb_ci",
    "resolution notes": "u_close_notes",
    "resolution code": "u_close_code",
}
REFERENCE_FIELDS = {
    "caller_id", "service_offering", "assignment_group", "assigned_to",
    "cmdb_ci", "rfc",
}
STATE_MAP = {
    "new": "1", "in progress": "2", "on hold": "3", "resolved": "6",
    "closed": "7", "cancelled": "8", "canceled": "8",
}
CLOSE_CODE_MAP = {
    "solved (permanently)": "Solution provided",
    "solved (work around)": "Workaround provided",
    "solved (workaround)": "Workaround provided",
    "known error": "Known error",
    "duplicate": "Duplicate",
    "no resolution provided": "No resolution provided",
    "resolved by caller": "Resolved by Caller",
    "resolved by change": "Resolved by Change",
    "resolved by problem": "Resolved by Problem",
    "resolved by request": "Resolved by Request",
}


@dataclass(frozen=True)
class Credentials:
    base_url: str
    username: str
    password: str
    auth_mode: str = "basic"


SCHEMA_FIELDS = {
    "u_source_number": ("Source Number", "string", 100),
    "u_number": ("Incident Number", "string", 100),
    "u_short_description": ("Short Description", "string", 500),
    "u_description": ("Description", "string", 4000),
    "u_caller_id": ("Caller", "string", 255),
    "u_state": ("State", "string", 40),
    "u_urgency": ("Urgency", "string", 40),
    "u_impact": ("Impact", "string", 40),
    "u_priority": ("Priority", "string", 40),
    "u_opened_at": ("Opened At", "glide_date_time", 0),
    "u_resolved_at": ("Resolved At", "glide_date_time", 0),
    "u_closed_at": ("Closed At", "glide_date_time", 0),
    "u_work_notes": ("Work Notes", "string", 4000),
    "u_close_notes": ("Resolution Notes", "string", 4000),
    "u_close_code": ("Resolution Code", "string", 100),
    "u_source_metadata": ("Source Metadata", "string", 4000),
    "u_service_offering": ("Service Offering", "string", 255),
    "u_assignment_group": ("Assignment Group", "string", 255),
    "u_assigned_to": ("Assigned To", "string", 255),
    "u_hold_reason": ("On Hold Reason", "string", 255),
    "u_external_url": ("External URL", "string", 4000),
    "u_cmdb_ci": ("Configuration Item", "string", 255),
    "rfc": ("Change Request", "string", 255),
}


def _load_dotenv(path: Path) -> None:
    """Load missing values from .env without overriding the invoking shell."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_credentials() -> Credentials:
    _load_dotenv(PROJECT_ROOT / ".env")
    values = {
        "base_url": os.getenv("SERVICENOW_BASE_URL", "").strip().rstrip("/"),
        "username": os.getenv("SERVICENOW_USERNAME", "").strip(),
        # Match the application connector: portal/form values may be URL encoded.
        "password": unquote(os.getenv("SERVICENOW_PASSWORD", "").strip()),
    }
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise RuntimeError("Missing ServiceNow configuration: " + ", ".join(missing))
    return Credentials(**values)


def clean(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "null"} else text


def choice_number(value: Any) -> str:
    match = re.match(r"\s*(\d+)", clean(value))
    return match.group(1) if match else clean(value)


def sn_datetime(value: Any) -> str:
    if value is None or pd.isna(value) or clean(value) == "":
        return ""
    try:
        if isinstance(value, (int, float)):
            parsed = pd.to_datetime(value, unit="D", origin="1899-12-30")
        else:
            parsed = pd.to_datetime(value)
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError):
        return ""


def first_value(row: pd.Series, *columns: str) -> Any:
    for column in columns:
        value = row.get(column)
        if clean(value):
            return value
    return None


def _source_metadata(row: pd.Series) -> dict[str, str]:
    keep = (
        "CARE_Monitoring_Ref", "ProductTeam_Ref", "ProductSubTeam_Ref",
        "CalendarWeek_Closed", "Month_Closed", "Day_Closed", "TicketCluster",
        "DoubleDataCheck_Base", "DoubledData", "NotReprod_WorkNotes",
    )
    return {key: clean(row.get(key)) for key in keep if clean(row.get(key))}


def build_payload(row: pd.Series, excel_row: int) -> tuple[str, dict[str, str]]:
    source_number = clean(row.get("Number"))
    if not source_number:
        raise ValueError(f"Excel row {excel_row} has no incident Number")

    state_text = clean(row.get("State")).lower()
    close_code = CLOSE_CODE_MAP.get(
        clean(row.get("Resolution code")).lower(), "Solution provided",
    )
    description = clean(row.get("Description"))
    if not description:
        description = clean(row.get("Short description"))

    source_metadata = json.dumps(_source_metadata(row), ensure_ascii=False, sort_keys=True)
    work_notes = clean(row.get("Work notes"))
    provenance = f"[Novastra source incident: {source_number}]\n[Source metadata] {source_metadata}"
    payload = {
        "u_source_number": SOURCE_PREFIX + source_number,
        "u_number": source_number,
        "u_short_description": clean(row.get("Short description"))[:500]
        or f"Imported historical incident {source_number}",
        "u_description": description[:4000],
        "u_caller_id": clean(row.get("Caller")),
        "u_state": STATE_MAP.get(state_text, choice_number(row.get("State")) or "7"),
        "u_urgency": choice_number(row.get("Urgency")),
        "u_impact": choice_number(row.get("Impact")),
        "u_priority": choice_number(row.get("Priority")),
        "u_opened_at": sn_datetime(first_value(row, "Opened", "Created")),
        "u_resolved_at": sn_datetime(row.get("Resolved")),
        "u_closed_at": sn_datetime(first_value(row, "Closed", "ClosedOn")),
        "u_work_notes": (provenance + ("\n" + work_notes if work_notes else ""))[:4000],
        "u_close_notes": clean(row.get("Resolution notes"))[:4000]
        or "Imported historical incident",
        "u_close_code": close_code,
        "u_source_metadata": source_metadata[:4000],
    }
    for source, target in FIELD_MAP.items():
        value = clean(row.get(next((c for c in row.index if c.lower() == source), source)))
        if value and target not in payload:
            payload[target] = value[:MAX_TEXT]
    return source_number, {key: value for key, value in payload.items() if value != ""}


def load_rows(path: Path, sheet: str | int | None) -> list[tuple[int, str, dict[str, str]]]:
    frame = pd.read_excel(path, sheet_name=sheet if sheet is not None else 0)
    rows = []
    for index, row in frame.iterrows():
        source_number, payload = build_payload(row, index + 2)
        rows.append((index + 2, source_number, payload))
    return rows


def _request(client: httpx.Client, method: str, url: str, credentials: Credentials, **kwargs) -> httpx.Response:
    # A single `return response` at the end (rather than one inside the loop
    # too) is deliberate: every attempt but the last raising httpx.RequestError
    # re-raises instead of falling through, so by the time this line is
    # reached `response` always holds a real attempt's result.
    response = None
    for attempt in range(1, 6):
        try:
            auth = (
                (credentials.username, credentials.password)
                if credentials.auth_mode == "basic"
                else None
            )
            response = client.request(method, url, auth=auth, **kwargs)
            if response.status_code not in {429, 500, 502, 503, 504}:
                break
        except httpx.RequestError:
            if attempt == 5:
                raise
        time.sleep(min(20, 1.5 ** attempt))
    return response


def authenticate_client(client: httpx.Client, credentials: Credentials) -> Credentials:
    """Use Basic Auth when available, otherwise establish a browser session.

    Some ServiceNow PDIs disable Basic Auth for REST while still allowing the
    administrator to sign in through login.do. Cookie-authenticated REST calls
    also require the session's g_ck value in X-UserToken.
    """
    probe_url = f"{credentials.base_url}/api/now/table/sys_db_object"
    probe = client.get(
        probe_url,
        params={"sysparm_limit": "1", "sysparm_fields": "name"},
        auth=(credentials.username, credentials.password),
        headers={"Accept": JSON_CONTENT_TYPE},
    )
    if probe.status_code != 401:
        probe.raise_for_status()
        return credentials

    login = client.post(
        f"{credentials.base_url}/login.do",
        data={
            "user_name": credentials.username,
            "user_password": credentials.password,
            "sys_action": "sysverb_login",
        },
        follow_redirects=True,
    )
    login.raise_for_status()
    token = None
    for pattern in (
        r"var\s+g_ck\s*=\s*['\"]([^'\"]+)",
        r"NOW\.user_token\s*=\s*['\"]([^'\"]+)",
    ):
        match = re.search(pattern, login.text)
        if match:
            token = match.group(1)
            break
    if not token:
        raise RuntimeError("ServiceNow login succeeded but no session user token was returned.")

    client.headers["X-UserToken"] = token
    session_credentials = replace(credentials, auth_mode="session")
    validation = _request(
        client,
        "GET",
        probe_url,
        session_credentials,
        params={"sysparm_limit": "1", "sysparm_fields": "name"},
        headers={"Accept": JSON_CONTENT_TYPE, "X-UserToken": token},
    )
    if validation.status_code != 200:
        raise RuntimeError(
            f"ServiceNow session authentication failed ({validation.status_code}): "
            f"{validation.text[:300]}"
        )
    return session_credentials


def ensure_schema(client: httpx.Client, credentials: Credentials, table: str) -> None:
    """Idempotently create the custom import table and its required columns."""
    object_url = f"{credentials.base_url}/api/now/table/sys_db_object"
    response = _request(client, "GET", object_url, credentials, params={
        "sysparm_query": f"name={table}",
        "sysparm_limit": "1",
        "sysparm_fields": "sys_id,name,label",
    })
    response.raise_for_status()
    if not response.json().get("result"):
        response = _request(client, "POST", object_url, credentials, params={
            "sysparm_input_display_value": "true",
        }, headers=JSON_HEADERS, json={
            "name": table,
            "label": "Novastra Imported Incident",
        })
        if response.status_code not in {200, 201}:
            raise RuntimeError(
                f"Could not create ServiceNow table {table} ({response.status_code}): "
                f"{response.text[:500]}"
            )
        print(f"Created ServiceNow table: {table}")

    dictionary_url = f"{credentials.base_url}/api/now/table/sys_dictionary"
    for element, (label, internal_type, max_length) in SCHEMA_FIELDS.items():
        response = _request(client, "GET", dictionary_url, credentials, params={
            "sysparm_query": f"name={table}^element={element}",
            "sysparm_limit": "1",
            "sysparm_fields": "sys_id",
        })
        response.raise_for_status()
        if response.json().get("result"):
            continue
        payload = {
            "name": table,
            "element": element,
            "column_label": label,
            "internal_type": internal_type,
        }
        if max_length:
            payload["max_length"] = str(max_length)
        response = _request(
            client,
            "POST",
            dictionary_url,
            credentials,
            params={"sysparm_input_display_value": "true"},
            headers=JSON_HEADERS,
            json=payload,
        )
        if response.status_code not in {200, 201}:
            raise RuntimeError(
                f"Could not create field {table}.{element} ({response.status_code}): "
                f"{response.text[:500]}"
            )
        print(f"Created field: {element}")


def existing_source_numbers(client: httpx.Client, credentials: Credentials, table: str) -> set[str]:
    """Fetch all records previously imported by this tool."""
    found: set[str] = set()
    offset = 0
    url = f"{credentials.base_url}/api/now/table/{table}"
    while True:
        response = _request(client, "GET", url, credentials, params={
            "sysparm_query": f"u_source_numberSTARTSWITH{SOURCE_PREFIX}",
            "sysparm_fields": "u_source_number",
            "sysparm_limit": "10000",
            "sysparm_offset": str(offset),
        })
        if response.status_code == 401:
            raise RuntimeError(
                "ServiceNow authentication failed (401). Verify that the developer "
                "instance is awake and that the admin password is current."
            )
        if response.status_code == 403:
            raise RuntimeError(
                "ServiceNow authenticated but denied read access to the incident table (403)."
            )
        response.raise_for_status()
        records = response.json().get("result", [])
        found.update(
            clean(item.get("u_source_number")).removeprefix(SOURCE_PREFIX)
            for item in records if clean(item.get("u_source_number"))
        )
        if len(records) < 10000:
            return found
        offset += len(records)


def insert_one(
    client: httpx.Client,
    credentials: Credentials,
    item: tuple[int, str, dict[str, str]],
    table: str,
) -> dict[str, Any]:
    excel_row, source_number, payload = item
    url = f"{credentials.base_url}/api/now/table/{table}"
    response = _request(client, "POST", url, credentials, params={
        "sysparm_input_display_value": "true",
        "sysparm_fields": "sys_id,u_number,u_source_number",
    }, headers=JSON_HEADERS, json=payload)

    # Unknown reference values or instance-specific custom fields must not lose
    # the incident. Retry with the portable core fields and retain provenance.
    if response.status_code in {400, 403}:
        portable = {key: value for key, value in payload.items() if key not in REFERENCE_FIELDS and not key.startswith("u_")}
        response = _request(client, "POST", url, credentials, params={
            "sysparm_input_display_value": "true",
            "sysparm_fields": "sys_id,u_number,u_source_number",
        }, headers=JSON_HEADERS, json=portable)

    if response.status_code not in {200, 201}:
        return {
            "status": "failed", "excel_row": excel_row, "source_number": source_number,
            "http_status": response.status_code, "error": response.text[:500],
        }
    result = response.json().get("result", {})
    return {
        "status": "inserted", "excel_row": excel_row, "source_number": source_number,
        "number": result.get("u_number"), "sys_id": result.get("sys_id"),
    }


def append_result(path: Path, result: dict[str, Any], lock: threading.Lock) -> None:
    with lock, path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(result, ensure_ascii=False) + "\n")


def verify_remote(client: httpx.Client, credentials: Credentials, table: str, expected: int) -> tuple[int, bool]:
    actual = len(existing_source_numbers(client, credentials, table))
    return actual, actual >= expected


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--excel", type=Path, default=DEFAULT_WORKBOOK)
    parser.add_argument("--sheet", default=None)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--table", default=os.getenv("SERVICENOW_TABLE", DEFAULT_TABLE))
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument(
        "--max-rows",
        type=int,
        default=MAX_IMPORT_ROWS,
        help=f"Maximum records to process (hard-capped at {MAX_IMPORT_ROWS}).",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    credentials = load_credentials()
    if not args.excel.exists():
        raise FileNotFoundError(args.excel)
    rows = load_rows(args.excel, args.sheet)
    requested_rows = args.max_rows if args.max_rows > 0 else MAX_IMPORT_ROWS
    rows = rows[:min(requested_rows, MAX_IMPORT_ROWS)]
    print(f"Workbook rows: {len(rows)}; target: {credentials.base_url}; credentials: configured")

    if args.dry_run:
        for excel_row, source_number, payload in rows[:10]:
            print(json.dumps({"excel_row": excel_row, "source_number": source_number, "payload": payload}, ensure_ascii=False))
        print(f"Dry run complete; {len(rows)} rows validated.")
        return 0

    limits = httpx.Limits(max_connections=max(2, args.workers), max_keepalive_connections=max(2, args.workers))
    with httpx.Client(timeout=args.timeout, verify=True, limits=limits) as client:
        credentials = authenticate_client(client, credentials)
        ensure_schema(client, credentials, args.table)
        existing = existing_source_numbers(client, credentials, args.table)
        if args.verify_only:
            actual, complete = verify_remote(client, credentials, args.table, len(rows))
            print(f"Verified imported incidents: {actual}/{len(rows)}; complete={complete}")
            return 0 if complete else 2

        pending = [item for item in rows if item[1] not in existing]
        print(f"Already present: {len(rows) - len(pending)}; pending insert: {len(pending)}")
        lock = threading.Lock()
        inserted = failed = 0
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            futures = [executor.submit(insert_one, client, credentials, item, args.table) for item in pending]
            for completed, future in enumerate(as_completed(futures), 1):
                result = future.result()
                append_result(args.checkpoint, result, lock)
                inserted += result["status"] == "inserted"
                failed += result["status"] == "failed"
                if completed % 100 == 0 or completed == len(futures):
                    print(f"Progress {completed}/{len(futures)}: inserted={inserted}, failed={failed}")

        actual, complete = verify_remote(client, credentials, args.table, len(rows))
        print(f"Import complete: inserted={inserted}, failed={failed}, remote={actual}/{len(rows)}")
        return 0 if failed == 0 and complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
