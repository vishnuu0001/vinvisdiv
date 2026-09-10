import os
import re

import httpx

base = "https://dev274740.service-now.com"
client = httpx.Client(timeout=30, verify=True, follow_redirects=True)
login = client.post(
    base + "/login.do",
    data={
        "user_name": os.environ["SN_USER"],
        "user_password": os.environ["SN_PASSWORD"],
        "sys_action": "sysverb_login",
    },
)
token = None
for pattern in (
    r"var\s+g_ck\s*=\s*['\"]([^'\"]+)",
    r"NOW\.user_token\s*=\s*['\"]([^'\"]+)",
):
    match = re.search(pattern, login.text)
    if match:
        token = match.group(1)
        break

print(f"user_token_found={bool(token)}")
headers = {"Accept": "application/json"}
if token:
    headers["X-UserToken"] = token
response = client.get(
    base + "/api/now/table/sys_db_object",
    params={"sysparm_limit": "1", "sysparm_fields": "name,label"},
    headers=headers,
)
print(f"schema_api_status={response.status_code}")
print(response.text[:300] if response.status_code != 200 else "schema_api_authenticated=true")
if response.status_code == 200:
    table_response = client.get(
        base + "/api/now/table/sys_db_object",
        params={
            "sysparm_query": "name=u_novastra_imported_incident",
            "sysparm_limit": "1",
            "sysparm_fields": "sys_id,name,label",
        },
        headers=headers,
    )
    records = table_response.json().get("result", []) if table_response.status_code == 200 else []
    print(f"target_table_status={table_response.status_code}")
    print(f"target_table_exists={bool(records)}")
