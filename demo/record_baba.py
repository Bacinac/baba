#!/usr/bin/env python3
"""Record BABA's /api GET responses by driving the real UI (Playwright).

Logs in, walks every route (extracting dynamic ids from the list pages first),
and captures each /api GET → api-fixtures.json keyed by path. Read-only against
production; the capture is anonymised in a later pass before it ever ships.

    BABA_URL=http://192.0.2.11:5173 BABA_USER=admin BABA_PASS=... \
      python record_baba.py /out/api-fixtures.json
"""
import json
import os
import re
import sys
import time

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

BASE = os.environ["BABA_URL"].rstrip("/")
USER = os.environ.get("BABA_USER", "admin")
PASS = os.environ["BABA_PASS"]
OUT = sys.argv[1] if len(sys.argv) > 1 else "/out/api-fixtures.json"

STATIC_ROUTES = [
    "/", "/live", "/activity", "/identities", "/analytics", "/search",
    "/settings", "/settings/cameras", "/settings/detection", "/settings/face-recognition",
    "/settings/notifications", "/settings/storage", "/settings/system", "/settings/users",
    "/settings/ai", "/settings/audit", "/settings/preferences", "/account", "/about",
]

fixtures = {}


def key_for(url):
    path = url.split("/api", 1)[1] if "/api" in url else url
    # drop cache-buster / volatile params so the key recurs
    path = re.sub(r"[?&]t=\d+", "", path)
    path = re.sub(r"[?&]_=\d+", "", path)
    return path or "/"


def on_response(resp):
    try:
        req = resp.request
        if req.method != "GET" or "/api/" not in resp.url or resp.status != 200:
            return
        ct = (resp.headers or {}).get("content-type", "")
        if "application/json" not in ct:
            return
        # skip binary/pixel endpoints — those ship as static blurred assets
        if re.search(r"/(live\.jpg|snapshot|stream\.|frame|thumbnail)", resp.url):
            return
        fixtures[key_for(resp.url)] = resp.json()
    except (PlaywrightError, ValueError) as e:
        print(f"  ! {resp.url}: {e}", file=sys.stderr)


def visit(page, path: str, settle_s: float) -> None:
    try:
        page.goto(f"{BASE}{path}", wait_until="networkidle", timeout=20000)
        time.sleep(settle_s)
    except PlaywrightError as e:
        print(f"  ! {path}: {e}", file=sys.stderr)


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = ctx.new_page()
        page.on("response", on_response)

        page.goto(f"{BASE}/login", wait_until="networkidle")
        page.fill("#username", USER)
        page.fill("#password", PASS)
        page.click("button[type=submit]")
        page.wait_for_url(lambda u: "/login" not in u, timeout=15000)
        time.sleep(1.5)

        # 1) static routes
        for r in STATIC_ROUTES:
            visit(page, r, 0.6)

        # 2) dynamic detail pages — pull ids from what we captured
        idents = fixtures.get("/identities") or fixtures.get("/identities/") or []
        gids = [i.get("gid") or i.get("id") for i in (idents if isinstance(idents, list) else [])][:4]
        cams = fixtures.get("/cameras") or []
        cids = [c.get("id") or c.get("slug") for c in (cams if isinstance(cams, list) else [])][:2]
        for gid in filter(None, gids):
            visit(page, f"/identities/{gid}", 0.5)
        for slug in filter(None, [c.get("slug") for c in (cams if isinstance(cams, list) else [])][:2]):
            visit(page, f"/live/{slug}", 0.5)
        for cid in filter(None, cids):
            visit(page, f"/settings/cameras/{cid}", 0.5)

        browser.close()

    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(fixtures, f, ensure_ascii=False)
    print(f"snimljeno {len(fixtures)} endpointa -> {OUT}")
    for k in sorted(fixtures)[:40]:
        print("  ", k)


if __name__ == "__main__":
    main()
