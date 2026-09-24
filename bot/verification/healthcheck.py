"""Container health: the page answers on loopback."""

from urllib.request import urlopen

with urlopen("http://127.0.0.1:8095/healthz", timeout=3) as response:
    raise SystemExit(0 if response.status == 200 else 1)
