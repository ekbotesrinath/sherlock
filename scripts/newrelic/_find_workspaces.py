"""One-off script: list Log Analytics workspaces using cached Sherlock token."""
import json, time, httpx
from pathlib import Path

cache_path = Path.home() / ".sherlock" / "azure_token_cache.json"
data = json.loads(cache_path.read_text())
now = time.time()

token = None
for scope, (tok, exp) in data.get("access_tokens", {}).items():
    if exp > now + 60:
        token = tok
        print(f"Found valid token (scope: {scope[:70]})")
        break

if not token:
    print("No valid access token in cache — re-run make azure-connect-interactive")
    raise SystemExit(1)

sub_id = "ff032cfd-cfe7-4ad3-98e2-704b5dcee132"
url = (
    f"https://management.azure.com/subscriptions/{sub_id}"
    "/providers/Microsoft.OperationalInsights/workspaces?api-version=2022-10-01"
)
r = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=15)
print(f"HTTP {r.status_code}")
if r.status_code == 200:
    ws = r.json().get("value", [])
    print(f"Found {len(ws)} workspace(s):\n")
    for w in ws:
        name = w["name"]
        cid  = w.get("properties", {}).get("customerId", "N/A")
        rg   = w["id"].split("/")[4]
        loc  = w.get("location", "?")
        print(f"  Name: {name}")
        print(f"  WorkspaceId (GUID): {cid}")
        print(f"  ResourceGroup: {rg}  Location: {loc}")
        print()
else:
    print(r.text[:800])
