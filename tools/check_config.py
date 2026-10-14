#!/usr/bin/env python3
"""
Validate a client onboarding config and render a preview of the client page.

Usage:
    python3 check_config.py config.json [--template index.html] [--out preview] [--emit] [--contact-name "Full Name"] [--token abc123def456]

- Validates the config against the rules the page and backend expect.
- Renders the page with the config (backend mocked, nothing is written anywhere)
  and saves <out>-desktop.png and <out>-mobile.png.
- Exits 1 if validation fails or the page throws errors.
- With --emit, also writes <out>-sheet.json: a fresh token, the client link, and the exact
  rows to write to the tracker Sheet (client tab values and the Clients row).

The template defaults to the live page at https://onboarding.twowheelsmarketing.com/index.html.
Needs: playwright with chromium (pip install playwright --break-system-packages && playwright install chromium).
"""
import argparse
import datetime
import secrets
import string
import http.server
import json
import os
import re
import shutil
import socketserver
import sys
import tempfile
import threading
import urllib.request

LIVE_TEMPLATE = "https://onboarding.twowheelsmarketing.com/index.html"
PAGE_URL = "https://onboarding.twowheelsmarketing.com/?c="
ITEM_TITLES = {
    "ga4": "Google Analytics", "gtm": "Google Tag Manager", "gbp": "Google Business Profile",
    "meta": "Facebook and Instagram", "gads": "Google Ads", "site_static": "Your website",
    "site_wordpress": "Your website (WordPress)", "calendly": "Calendly", "dns_cloudflare": "Domain (Cloudflare)",
}
HEADER = ["Key", "Item", "Client checked", "Client answer", "Last updated", "TWM confirmed", "Notes", "Answer source"]

ITEM_RULES = {
    "ga4": ["id"],
    "gtm": ["id"],
    "gbp": ["listing"],
    "meta": [],
    "gads": [],
    "site_static": ["host"],
    "site_wordpress": [],
    "calendly": [],
    "dns_cloudflare": [],
    "custom": ["title", "ask", "sections"],
}
DASHES = re.compile("[\u2014\u2013]")  # em and en dash
MAX_CONFIG_CHARS = 45000  # one Sheets cell holds 50,000


def validate(cfg):
    errors, warnings = [], []
    for k in ["client", "domain", "scanned", "agency_email", "contact_email", "items"]:
        if not cfg.get(k):
            errors.append(f"missing top-level '{k}'")
    if not cfg.get("contact"):
        warnings.append("no 'contact' first name: heading will read 'Getting us connected' with no name")
    if not cfg.get("found"):
        warnings.append("no 'found' rows: the collapsed 'What we found' panel at the bottom will be hidden")
    for i, row in enumerate(cfg.get("found", [])):
        if not (isinstance(row, list) and len(row) == 2 and all(isinstance(x, str) for x in row)):
            errors.append(f"found[{i}] must be [label, text]")
    keys = []
    for i, it in enumerate(cfg.get("items", [])):
        full = str(it.get("key"))
        keys.append(full)
        key = full.split(":")[0]
        if key == "custom" and not re.fullmatch(r"custom:[a-z0-9_]{2,30}", full):
            errors.append(f"items[{i}] custom items need a key like 'custom:shopify' (lowercase, digits, underscore)")
        if key not in ITEM_RULES:
            errors.append(f"items[{i}] unknown key '{key}' (allowed: {', '.join(ITEM_RULES)})")
            continue
        v = it.get("vars", {})
        for req in ITEM_RULES[key]:
            if not v.get(req):
                errors.append(f"items[{i}] ({key}) missing vars.{req}")
        if key == "custom":
            if v.get("kind") not in (None, "found", "missing", "optional"):
                errors.append(f"items[{i}] custom kind must be found, missing or optional")
            for j, sec in enumerate(v.get("sections", [])):
                if not sec.get("steps"):
                    errors.append(f"items[{i}] custom sections[{j}] has no steps")
            for f in ("title", "ask"):
                if v.get(f) and re.search(r"<[a-zA-Z/]", v[f]):
                    errors.append(f"items[{i}] custom {f} contains HTML; use **bold**, `code`, [text](https://url)")
        ans = (v.get("answer") if key == "custom" else None)
        if ans and ans.get("type") not in ("text", "select"):
            errors.append(f"items[{i}] answer.type must be text or select")
        if ans and ans.get("type") == "select" and not ans.get("options"):
            errors.append(f"items[{i}] select answer needs options (first option should be \"\")")
        if ans and any(x not in ans.get("options", []) for x in ans.get("no_check", [])) and ans.get("type") == "select":
            errors.append(f"items[{i}] answer.no_check values must match options exactly")
        if ans and "auto_check" in ans and not isinstance(ans["auto_check"], bool):
            errors.append(f"items[{i}] answer.auto_check must be true or false")
    for i, q in enumerate(cfg.get("questions", [])):
        k = str(q.get("key"))
        keys.append(k)
        if not re.fullmatch(r"q:[a-z0-9_]{2,30}", k):
            errors.append(f"questions[{i}] key must look like 'q:usp' (lowercase, digits, underscore)")
        for f in ("title", "question"):
            if not q.get(f):
                errors.append(f"questions[{i}] ({k}) missing {f}")
        if q.get("type") not in (None, "long", "short"):
            errors.append(f"questions[{i}] ({k}) type must be long or short")
        if q.get("suggestion") is not None and not str(q["suggestion"]).strip():
            errors.append(f"questions[{i}] ({k}) suggestion is empty; omit it instead")
        if len(str(q.get("suggestion", ""))) > 3500:
            errors.append(f"questions[{i}] ({k}) suggestion is too long (keep it under 3,500 characters)")
    dupes = {k for k in keys if keys.count(k) > 1}
    if dupes:
        errors.append(f"duplicate item keys: {', '.join(sorted(dupes))} (each becomes a tracker row and must be unique)")
    logo = cfg.get("logo")
    if logo and not (logo.startswith("data:image/") or logo.startswith("https://")):
        errors.append("logo must be a data:image/... URI or an https URL")
    if cfg.get("logo_style") not in (None, "chip"):
        errors.append("logo_style must be omitted or 'chip'")
    text = json.dumps(cfg, ensure_ascii=False)
    if DASHES.search(text):
        errors.append("contains an em or en dash; rewrite with commas, colons or periods")
    if len(json.dumps(cfg, separators=(",", ":"))) > MAX_CONFIG_CHARS:
        errors.append(f"config is over {MAX_CONFIG_CHARS} characters (usually the logo); shrink the logo")
    if re.search(r"(?i)\bclaude\b|\bAI\b|anthropic", text.replace(cfg.get("logo", "") or "", "")):
        warnings.append("config text mentions Claude/AI/Anthropic; client-facing copy should not")
    return errors, warnings


def render(cfg, template_path, out):
    from playwright.sync_api import sync_playwright

    tmp = tempfile.mkdtemp()
    shutil.copy(template_path, os.path.join(tmp, "index.html"))

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=tmp, **k)

        def log_message(self, *a):
            pass

    srv = socketserver.TCPServer(("127.0.0.1", 0), Quiet)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    body = json.dumps({"ok": True, "config": cfg, "progress": {}})
    errors = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        for name, w in (("desktop", 1280), ("mobile", 390)):
            pg = b.new_page(viewport={"width": w, "height": 900})
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.route("**/config.js", lambda r, q: r.fulfill(status=200, content_type="application/javascript",
                                                             body='window.TWM_SCRIPT_URL="https://mock.invalid/exec";'))
            pg.route("https://mock.invalid/**", lambda r, q: r.fulfill(status=200, content_type="application/json", body=body))
            pg.goto(f"http://127.0.0.1:{port}/?c=previewtoken01")
            pg.wait_for_selector(".item", timeout=15000)
            for d in pg.query_selector_all("details.item, details.q"):
                d.evaluate("el => el.open = true")
            if pg.evaluate("document.documentElement.scrollWidth") > w + 1:
                errors.append(f"{name}: page scrolls sideways")
            pg.screenshot(path=f"{out}-{name}.png", full_page=True)
        b.close()
    srv.shutdown()
    return errors


def emit(cfg, out, contact_name=None, token=None):
    token = token or "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(12))
    assert re.fullmatch(r"[a-z0-9]{12}", token), "token must be 12 lowercase letters/digits"
    rows = [HEADER]
    for it in cfg["items"]:
        key = it["key"]
        title = it["vars"]["title"] if key.startswith("custom:") else ITEM_TITLES[key]
        rows.append([key, title, "FALSE", "", "", "FALSE", "", ""])
    for q in cfg.get("questions", []):
        rows.append([q["key"], q["title"], "FALSE", "", "", "FALSE", "", ""])
    link = PAGE_URL + token
    sheet = {
        "token": token,
        "link": link,
        "tab_name": cfg["client"],
        "tab_range": f"'{cfg['client']}'!A1:H{len(rows)}",
        "tab_values": rows,
        "clients_row": [token, cfg["client"], cfg["domain"], contact_name or cfg.get("contact", ""), link,
                        datetime.date.today().isoformat(), json.dumps(cfg, separators=(",", ":"), ensure_ascii=False)],
    }
    path = f"{out}-sheet.json"
    with open(path, "w") as f:
        json.dump(sheet, f, ensure_ascii=False, indent=1)
    return path, token, link


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("config")
    ap.add_argument("--template", help="local index.html (default: download the live page)")
    ap.add_argument("--out", default="preview")
    ap.add_argument("--emit", action="store_true", help="write <out>-sheet.json with token, link and tracker rows")
    ap.add_argument("--contact-name", help="full contact name for the Clients tab (default: config 'contact')")
    ap.add_argument("--token", help="reuse an existing 12-char token instead of generating one")
    a = ap.parse_args()

    cfg = json.load(open(a.config))
    errors, warnings = validate(cfg)
    for w in warnings:
        print("WARN ", w)
    for e in errors:
        print("ERROR", e)
    if errors:
        sys.exit(1)

    template = a.template
    if not template:
        template = os.path.join(tempfile.mkdtemp(), "index.html")
        urllib.request.urlretrieve(LIVE_TEMPLATE, template)
    render_errors = render(cfg, template, a.out)
    for e in render_errors:
        print("ERROR", e)
    if render_errors:
        sys.exit(1)
    if a.emit:
        if "/" in cfg["client"] or "'" in cfg["client"] or len(cfg["client"]) > 90:
            print("ERROR client name can't contain / or ' and must be under 90 characters (it becomes a Sheet tab name)")
            sys.exit(1)
        path, token, link = emit(cfg, a.out, a.contact_name, a.token)
        print(f"OK  token {token}  link {link}  sheet rows: {path}")
    print(f"OK  config valid ({len(json.dumps(cfg, separators=(',', ':')))} chars); previews: {a.out}-desktop.png, {a.out}-mobile.png")


if __name__ == "__main__":
    main()
