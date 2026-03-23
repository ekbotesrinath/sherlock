"""Query Azure Monitor Metrics for psql-flex-eus2-prd-eswd-wltz-001-0309 (disk queue / IOPS / CPU / connections)."""
import json, time, httpx
from pathlib import Path
from datetime import datetime, timezone, timedelta

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

resource_id = (
    "/subscriptions/ff032cfd-cfe7-4ad3-98e2-704b5dcee132"
    "/resourceGroups/rg-cus-prd-eswd-wltz"
    "/providers/Microsoft.DBforPostgreSQL/flexibleServers"
    "/psql-flex-eus2-prd-eswd-wltz-001-0309"
)

# Incident window: Mar 20 21:00 UTC → Mar 21 02:00 UTC (extra buffer)
start = "2026-03-20T21:00:00Z"
end   = "2026-03-21T02:00:00Z"

metrics = [
    "disk_queue_depth",
    "iops",
    "read_iops",
    "write_iops",
    "io_consumption_percent",
    "cpu_percent",
    "memory_percent",
    "storage_percent",
    "active_connections",
    "read_throughput",
    "write_throughput",
]

base = f"https://management.azure.com{resource_id}/providers/microsoft.insights/metrics"
headers = {"Authorization": f"Bearer {token}"}

print(f"Querying Azure Monitor Metrics for {resource_id.split('/')[-1]}")
print(f"Window: {start} → {end}\n")

for metric in metrics:
    params = {
        "api-version": "2019-07-01",
        "metricnames": metric,
        "interval": "PT5M",
        "timespan": f"{start}/{end}",
        "aggregation": "Maximum,Average",
    }
    r = httpx.get(base, headers=headers, params=params, timeout=15)
    if r.status_code != 200:
        print(f"  {metric}: HTTP {r.status_code} {r.text[:100]}")
        continue
    body = r.json()
    mlist = body.get("value", [])
    for m in mlist:
        mname = m.get("name", {}).get("value", metric)
        unit  = m.get("unit", "?")
        ts_series = m.get("timeseries", [])
        if not ts_series:
            print(f"  {mname} ({unit}): NO_DATA")
            continue
        pts = ts_series[0].get("data", [])
        # Find peak and show values around incident (23:35-00:25)
        peak_val = 0
        peak_time = ""
        incident_pts = []
        for p in pts:
            t = p.get("timeStamp", "")
            val = p.get("maximum") or p.get("average") or 0
            if val and val > peak_val:
                peak_val = val
                peak_time = t
            # Show the incident window more closely
            if "23:" in t or "00:" in t:
                incident_pts.append((t[11:16], round(p.get("maximum", p.get("average", 0)), 2)))
        print(f"\n  [{mname}] unit={unit}  peak={round(peak_val,2)} @ {peak_time[11:16] if peak_time else '?'}")
        print(f"    Timeline (23:xx-00:xx UTC): {incident_pts}")
