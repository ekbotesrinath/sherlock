"""List all PostgreSQL Flexible Server instances in the subscription."""
import json, time, httpx
from pathlib import Path

cache_path = Path.home() / ".sherlock" / "azure_token_cache.json"
data = json.loads(cache_path.read_text())
now = time.time()
token = None
for scope, (tok, exp) in data.get("access_tokens", {}).items():
    if "management.azure.com" in scope and exp > now + 60:
        token = tok
        break

if not token:
    print("No management token")
    raise SystemExit(1)

sub = "ff032cfd-cfe7-4ad3-98e2-704b5dcee132"
url = (
    f"https://management.azure.com/subscriptions/{sub}"
    "/providers/Microsoft.DBforPostgreSQL/flexibleServers?api-version=2023-06-01-preview"
)
r = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=15)
print(f"HTTP {r.status_code}")
servers = r.json().get("value", [])
for s in servers:
    name  = s["name"]
    rid   = s["id"]
    rg    = rid.split("/")[4]
    loc   = s.get("location", "?")
    state = s.get("properties", {}).get("state", "?")
    sku   = s.get("sku", {}).get("name", "?")
    stor  = s.get("properties", {}).get("storage", {}).get("storageSizeGB", "?")
    tier  = s.get("sku", {}).get("tier", "?")
    print(f"  Name:  {name}")
    print(f"  RG:    {rg}  Location: {loc}  State: {state}")
    print(f"  SKU:   {sku} ({tier})  StorageGB: {stor}")
    print(f"  ID:    {rid}")
    print()
