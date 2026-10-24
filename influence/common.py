"""Shared paths and HTTP helpers for the corporate influence pipeline."""

import csv
import os
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "influence"
RAW = DATA / "raw"          # downloaded source files (git-ignored, re-creatable)
CLEAN = DATA / "clean"      # one normalised CSV per source, committed

USER_AGENT = "institutions-research/0.1 (open-data research; github.com/intelligent-cream/institutions)"
# SEC asks for "Name email" in the User-Agent; set SEC_USER_AGENT to your own contact.
SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT", "institutions-research contact@example.com")


def get(url, params=None, headers=None, retries=4, pause=1.0, data=None):
    """GET (or POST when `data` is given) with retries; returns bytes."""
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
    h = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    h.update(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=h)
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001 - retry any network error
            if attempt == retries - 1:
                raise
            wait = pause * 2 ** attempt
            if getattr(e, "code", None) == 429:
                wait = max(wait, 30)
            time.sleep(wait)


def get_json(url, params=None, **kw):
    return json.loads(get(url, params, **kw))


def cached(path, fetch):
    """Return bytes from `path`, downloading with `fetch()` the first time."""
    path = Path(path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(fetch())
    return path.read_bytes()


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path, rows, fields=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = fields or (list(rows[0].keys()) if rows else [])
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def companies():
    """The company universe with resolved identifiers (falls back to the seed list)."""
    p = DATA / "companies.csv"
    return read_csv(p if p.exists() else DATA / "companies_seed.csv")
