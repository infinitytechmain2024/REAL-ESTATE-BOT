"""Container healthcheck against the running Redis-aware HTTP endpoint."""

import json
from urllib.request import urlopen

with urlopen("http://127.0.0.1:8090/healthz", timeout=3) as response:
    result = json.load(response)
if result != {"ok": True}:
    raise SystemExit("browser-session dependency health check failed")
