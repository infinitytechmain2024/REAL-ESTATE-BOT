# Vendored SearXNG

This directory is a **full, unmodified snapshot** of the official SearXNG
repository, vendored into this project so that the bot can be built and
deployed as a single unit.

| | |
|---|---|
| Upstream | <https://github.com/searxng/searxng> |
| Commit | `d226b78bc4c9ab93a84849b8ad128a68c41be17c` |
| Commit date | 2026-08-29 |
| Version | `2026.8.29+d226b78bc` |
| Licence | AGPL-3.0-or-later (see `LICENSE`) |

## Local modifications

The snapshot is intentionally kept as close to upstream as possible. Only two
changes were made, both required by the fact that the upstream git history is
not shipped:

1. **`.git/` and `.github/` removed.** The history (124 MB) and the upstream CI
   workflows are not useful inside this repository and the nested `.git` would
   break `git` in the parent project.
2. **`searx/version_frozen.py` added, and un-ignored in `.gitignore`.**
   `searx/version.py` normally derives the version by shelling out to `git`.
   Without a git checkout it falls back to `searx.version_frozen`, so the
   version is pinned there. This is the mechanism upstream provides for
   building outside a git repository (`python -m searx.version freeze`).

**No application code, engine, template or static asset was deleted.**

## How the web UI is disabled

The task requires "backend + JSON API only". Rather than deleting frontend
files — which would make future updates painful and can break Flask routes that
the API layer also uses — the UI is disabled at runtime:

* `searxng/settings/settings.yml` sets `search.formats: [json]`, so SearXNG
  refuses to render HTML result pages at all.
* `searxng/api_only.py` is a thin WSGI wrapper around `searx.webapp.app` that
  allowlists only the endpoints the bot needs (`/search`, `/healthz`,
  `/config`, `/stats`) and returns `404` for everything else, including
  `/`, `/preferences`, `/static/*` and `/autocompleter`.

The wrapper is what the container actually serves, so no HTML interface is
reachable even though the sources are still on disk.

## Updating

```sh
rm -rf searxng
git clone https://github.com/searxng/searxng.git searxng
cd searxng
python -m searx.version freeze     # regenerates searx/version_frozen.py
rm -rf .git .github
# re-apply the '!searx/version_frozen.py' line in .gitignore
```

Then update the commit/date/version table above and re-run
`make searxng-smoke` (see the project README) to confirm the JSON API still
answers.
