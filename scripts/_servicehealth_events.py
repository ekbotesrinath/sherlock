"""Query Azure Activity Log REST API for ServiceHealth events (last 7 days)."""
import json, time, httpx
from pathlib import Path
from datetime import datetime, timezone, timedelta

cache_path = Path.home() / ".sherlock" / "azure_token_cache.json"
data = json.loads(cache_path.read_text())
now = time.time()

# Get management API token
token = None
for scope, (tok, exp) in data.get("access_tokens", {}).items():
    if "management.azure.com" in scope and exp > now + 60:
        token = tok
        break

if not token:
    print("No valid management token — re-run make azure-connect-interactive")
    raise SystemExit(1)

sub_id = "ff032cfd-cfe7-4ad3-98e2-704b5dcee132"
start = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
end   = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

url = (
    f"https://management.azure.com/subscriptions/{sub_id}"
    f"/providers/microsoft.insights/eventtypes/management/values"
    f"?api-version=2015-04-01"
    f"&$filter=eventTimestamp ge '{start}' and eventTimestamp le '{end}'"
    f" and category eq 'ServiceHealth'"
    f"&$select=eventTimestamp,operationName,level,description,eventName,properties,status"
)

headers = {"Authorization": f"Bearer {token}"}
print(f"Querying Activity Log for ServiceHealth events ({start} → {end})...\n")

seen_titles = set()
page = 0

with httpx.Client(timeout=30) as client:
    while url:
        r = client.get(url, headers=headers)
        if r.status_code != 200:
            print(f"HTTP {r.status_code}: {r.text[:500]}")
            break
        body = r.json()
        events = body.get("value", [])
        for ev in events:
            ts   = ev.get("eventTimestamp", "")[:19]
            op_raw = ev.get("operationName", {})
            op   = op_raw.get("value", "") if isinstance(op_raw, dict) else str(op_raw)
            lvl_raw = ev.get("level", {})
            lvl  = lvl_raw.get("value", "") if isinstance(lvl_raw, dict) else str(lvl_raw)
            props = ev.get("properties", {})
            title   = props.get("title", "")
            service = props.get("service", "")
            region  = props.get("region", "")
            inc_type= props.get("incidentType", "")
            stage   = props.get("stage", "")
            status  = props.get("status", "")
            impact_desc = props.get("impactedServices", "")  # JSON list or string
            communication = props.get("communication", "")

            # Deduplicate by title+stage
            key = f"{title}|{stage}|{ts[:10]}"
            if key in seen_titles:
                continue
            seen_titles.add(key)

            print(f"[{ts}] {lvl}")
            print(f"  Title:         {title}")
            print(f"  Service:       {service}")
            print(f"  Region:        {region}")
            print(f"  IncidentType:  {inc_type}")
            print(f"  Stage:         {stage}")
            print(f"  Status:        {status}")
            if impact_desc:
                try:
                    imp = json.loads(impact_desc) if impact_desc.startswith("[") else impact_desc
                    if isinstance(imp, list):
                        svcs = [f"{x.get('ServiceName','?')} ({x.get('ImpactedRegions',[])})" for x in imp[:5]]
                        print(f"  ImpactedSvcs:  {', '.join(svcs)}")
                except Exception:
                    print(f"  ImpactedSvcs:  {str(impact_desc)[:120]}")
            if communication:
                print(f"  Summary:       {communication[:200].strip()}")
            print()

        url = body.get("nextLink")
        page += 1
        if page > 10:
            print("... (truncated at 10 pages)")
            break

print(f"Total unique events displayed: {len(seen_titles)}")
