#!/usr/bin/env python3
"""
Access scanner: point it at a client's website and get back what marketing
assets exist (GTM, GA4, Google Ads, Meta pixel, CMS, hosting, DNS, registrar,
booking tools, social profiles) plus clues about who likely controls them.

Usage:
    python3 access_scanner.py https://example.com [--pages 8] [--out scan.json]

Standard library only. Respects HTTPS_PROXY. Optional CA bundle via SSL_CERT_FILE.
"""
import argparse
import json
import os
import re
import ssl
import sys
import urllib.parse
import urllib.request
from html import unescape

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"


def _ctx():
    cafile = os.environ.get("SSL_CERT_FILE")
    if not cafile and os.path.exists("/root/.ccr/ca-bundle.crt"):
        cafile = "/root/.ccr/ca-bundle.crt"
    return ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()


CTX = _ctx()


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            body = r.read(3_000_000).decode("utf-8", "replace")
            return r.status, {k.lower(): v for k, v in r.headers.items()}, body, r.geturl()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, "", url
    except Exception as e:  # noqa: BLE001
        return None, {}, f"ERROR: {e}", url


# ---------------------------------------------------------------- detectors

ID_PATTERNS = {
    "gtm": r"\bGTM-[A-Z0-9]{4,9}\b",
    "ga4": r"\bG-[A-Z0-9]{8,12}\b",
    "ua": r"\bUA-\d{4,10}-\d{1,4}\b",
    "google_ads": r"\bAW-\d{6,12}\b",
    "meta_pixel": r"fbq\(\s*['\"]init['\"]\s*,\s*['\"](\d{10,20})|facebook\.com/tr\?id=(\d{10,20})",
    "tiktok_pixel": r"ttq\.load\(\s*['\"]([A-Z0-9]{15,25})",
    "linkedin_insight": r"_linkedin_partner_id\s*=\s*['\"]?(\d{4,10})",
    "microsoft_uet": r"bat\.bing\.com.*?ti[:=]\s*['\"]?(\d{5,10})",
    "clarity": r"clarity\.ms/tag/([a-z0-9]{8,12})",
    "hotjar": r"hjid\s*:\s*(\d{5,10})",
}

VENDOR_MARKERS = {
    # CMS / site builders
    "WordPress": r"wp-content/|wp-includes/|<meta name=\"generator\" content=\"WordPress",
    "Shopify": r"cdn\.shopify\.com|Shopify\.theme",
    "Squarespace": r"static1\.squarespace\.com|squarespace-cdn",
    "Wix": r"static\.wixstatic\.com|wix-bolt|x-wix",
    "Webflow": r"webflow\.(js|com)|data-wf-site",
    "Duda": r"dudaone|multiscreensite\.com",
    "GoDaddy Website Builder": r"img1\.wsimg\.com|godaddy-website-builder",
    "Teleflora / eFlorist": r"teleflora|eflorist",
    "BigCommerce": r"cdn\d*\.bigcommerce\.com",
    "Unbounce": r"unbounce\.com|ubembed",
    "Elementor": r"wp-content/plugins/elementor",
    "Divi": r"et_pb_|/Divi/",
    # booking / forms / call tracking / crm
    "Calendly": r"calendly\.com",
    "Acuity": r"acuityscheduling\.com",
    "WhatConverts": r"iconnode\.com|whatconverts",
    "CallRail": r"cdn\.callrail\.com",
    "HubSpot": r"js\.hs-scripts\.com|hsforms",
    "Klaviyo": r"static\.klaviyo\.com",
    "Mailchimp": r"chimpstatic\.com|list-manage\.com",
    "Gravity Forms": r"gform_",
    "Cloudflare Web Analytics": r"static\.cloudflareinsights\.com|data-cf-beacon",
    "Google Maps embed": r"maps\.google\.com/maps\?|google\.com/maps/embed",
    "Google Business Profile link": r"g\.page/|maps\.app\.goo\.gl|business\.google\.com",
}

META_VERIFICATIONS = {
    "google_site_verification": r"<meta[^>]+name=[\"']google-site-verification[\"'][^>]+content=[\"']([^\"']+)",
    "facebook_domain_verification": r"<meta[^>]+name=[\"']facebook-domain-verification[\"'][^>]+content=[\"']([^\"']+)",
    "generator": r"<meta[^>]+name=[\"']generator[\"'][^>]+content=[\"']([^\"']+)",
}

HOST_HEADERS = [
    ("server", None), ("x-powered-by", None), ("via", None), ("x-served-by", None),
    ("cf-ray", "Cloudflare proxy"), ("x-vercel-id", "Vercel"), ("x-nf-request-id", "Netlify"),
    ("x-github-request-id", "GitHub Pages"), ("x-wix-request-id", "Wix"), ("x-shopid", "Shopify"),
    ("x-amz-cf-id", "AWS CloudFront"), ("x-kinsta-cache", "Kinsta"), ("x-wpe-request-id", "WP Engine"),
    ("x-pantheon-styx-hostname", "Pantheon"), ("x-sucuri-id", "Sucuri"), ("x-hostinger", "Hostinger"),
]

CREDIT_RE = re.compile(r"(?:designed|developed|website|site|built|powered|made)\s+(?:by|with)\s*(?:<[^>]+>\s*)*([^<]{2,80})", re.I)
SOCIAL_RE = re.compile(r"https?://(?:www\.)?(facebook|instagram|linkedin|x|twitter|youtube|tiktok|pinterest)\.com/[^\"'\s<>]+", re.I)


def find_ids(text):
    out = {}
    for name, pat in ID_PATTERNS.items():
        vals = set()
        for m in re.finditer(pat, text):
            groups = [g for g in m.groups() if g] if m.groups() else []
            vals.add(groups[0] if groups else m.group(0))
        if vals:
            out[name] = sorted(vals)
    return out


def merge(a, b):
    for k, v in b.items():
        a.setdefault(k, set()).update(v)


# ---------------------------------------------------------------- GTM container

GTM_TAG_TYPES = {
    "__googtag": "Google tag (GA4 / Ads)", "__gaawe": "GA4 event", "__gaawc": "GA4 config (legacy)",
    "__awct": "Google Ads conversion", "__sp": "Google Ads remarketing", "__gclidw": "Conversion linker",
    "__html": "Custom HTML", "__ua": "Universal Analytics", "__fls": "Floodlight",
    "__baut": "Microsoft UET", "__img": "Custom image",
}


def parse_gtm(container_id):
    status, _, body, _ = fetch(f"https://www.googletagmanager.com/gtm.js?id={container_id}")
    if status != 200 or not body:
        return {"id": container_id, "fetched": False, "status": status}
    ids = find_ids(body)
    ids.pop("gtm", None)
    # Meta pixels added as Custom HTML often keep the ID in a constant variable, not in fbq('init', ...)
    if "fbevents.js" in body and not ids.get("meta_pixel"):
        cands = sorted(set(re.findall(r'"vtp_(?:value|pixelId)":"(\d{15,16})"', body)))
        if cands:
            ids["meta_pixel"] = cands
    types = {label: body.count(f'"{key}"') for key, label in GTM_TAG_TYPES.items() if f'"{key}"' in body}
    vendors = sorted(v for v, p in VENDOR_MARKERS.items() if re.search(p, body, re.I) and v not in ("Elementor",))
    return {"id": container_id, "fetched": True, "ids_inside": ids, "tag_types": types, "vendors_inside": vendors}


# ---------------------------------------------------------------- DNS / RDAP

def doh(name, rtype):
    status, _, body, _ = fetch(f"https://dns.google/resolve?name={urllib.parse.quote(name)}&type={rtype}")
    try:
        return [a["data"].rstrip(".") for a in json.loads(body).get("Answer", []) if a.get("type") != 5 or rtype == "CNAME"]
    except Exception:  # noqa: BLE001
        return []


def classify_ns(ns):
    s = " ".join(ns).lower()
    for key, label in [("cloudflare", "Cloudflare"), ("domaincontrol", "GoDaddy"), ("awsdns", "AWS Route 53"),
                       ("googledomains", "Google Domains / Squarespace"), ("squarespacedns", "Squarespace"),
                       ("registrar-servers", "Namecheap"), ("wixdns", "Wix"), ("nsone", "NS1"),
                       ("dnsimple", "DNSimple"), ("bluehost", "Bluehost"), ("hostgator", "HostGator"),
                       ("siteground", "SiteGround"), ("ionos", "IONOS"), ("1and1", "IONOS"), ("ui-dns", "IONOS"), ("network-solutions", "Network Solutions"),
                       ("worldnic", "Network Solutions"), ("shopify", "Shopify")]:
        if key in s:
            return label
    return "Unknown" if ns else "None found"


def classify_mx(mx):
    s = " ".join(mx).lower()
    if "google" in s:
        return "Google Workspace"
    if "outlook" in s or "protection.outlook" in s:
        return "Microsoft 365"
    if "secureserver" in s:
        return "GoDaddy email"
    if "zoho" in s:
        return "Zoho"
    if "pphosted" in s or "ppe-hosted" in s or "mimecast" in s:
        return "Filtered by an email security gateway (Proofpoint/Mimecast); see SPF for the real provider"
    return "Unknown" if mx else "No email on this domain"


def rdap(domain):
    status, _, body, _ = fetch(f"https://rdap.org/domain/{domain}")
    if status != 200:
        return {}
    try:
        d = json.loads(body)
    except Exception:  # noqa: BLE001
        return {}
    out = {"events": {e.get("eventAction"): e.get("eventDate", "")[:10] for e in d.get("events", [])}}
    for ent in d.get("entities", []):
        if "registrar" in ent.get("roles", []):
            v = ent.get("vcardArray", [None, []])[1]
            out["registrar"] = next((x[3] for x in v if x[0] == "fn"), None)
    return out


# ---------------------------------------------------------------- crawl

def internal_links(html, base):
    host = urllib.parse.urlparse(base).netloc.replace("www.", "")
    links = []
    for href in re.findall(r"href=[\"']([^\"'#]+)", html):
        u = urllib.parse.urljoin(base, unescape(href))
        p = urllib.parse.urlparse(u)
        if p.scheme in ("http", "https") and p.netloc.replace("www.", "") == host:
            if not re.search(r"\.(png|jpe?g|gif|svg|webp|ico|pdf|css|js|xml|txt|zip|mp4)$", p.path, re.I):
                links.append(u.split("?")[0])
    return list(dict.fromkeys(links))


def scan(url, max_pages=8):
    if not url.startswith("http"):
        url = "https://" + url
    domain = urllib.parse.urlparse(url).netloc.replace("www.", "")
    status, headers, home, final = fetch(url)
    pages = {final: home}
    for link in internal_links(home, final)[: max_pages - 1]:
        if link not in pages:
            pages[link] = fetch(link)[2]
    corpus = "\n".join(pages.values())

    ids = {}
    merge(ids, find_ids(corpus))
    ids = {k: sorted(v) for k, v in ids.items()}

    vendors = sorted(v for v, p in VENDOR_MARKERS.items() if re.search(p, corpus, re.I))
    hosting_hints = []
    for h, label in HOST_HEADERS:
        if h in headers:
            hosting_hints.append(f"{label or h}: {headers[h]}" if not label else label)
    if headers.get("server", "").lower() == "amazons3":
        hosting_hints.append("AWS S3 static hosting")

    verif = {}
    for k, pat in META_VERIFICATIONS.items():
        m = re.search(pat, home, re.I)
        if m:
            verif[k] = m.group(1)

    ns, mx, txt = doh(domain, "NS"), doh(domain, "MX"), doh(domain, "TXT")
    www_cname = doh("www." + domain, "CNAME")
    txt_flags = [t for t in txt if re.search(r"google-site-verification|facebook-domain-verification|^\"?MS=|v=spf1", t)]

    credits = sorted({c.strip(" .-|©") for c in CREDIT_RE.findall(corpus) if len(c.strip()) > 2})[:5]
    socials = sorted({m.group(0).rstrip("/") for m in SOCIAL_RE.finditer(corpus)})
    emails = sorted(set(re.findall(r"mailto:([^\"'?]+)", corpus)))
    phones = sorted(set(re.findall(r"tel:([+\d\-\s().]+)", corpus)))
    title = re.search(r"<title>([^<]*)", home, re.I)

    gtm = [parse_gtm(g) for g in ids.get("gtm", [])]

    # IDs that only exist inside GTM still count as present on the site
    all_ids = {k: set(v) for k, v in ids.items()}
    for c in gtm:
        merge(all_ids, {k: set(v) for k, v in c.get("ids_inside", {}).items()})

    install = {}
    for k in ("ga4", "google_ads", "meta_pixel"):
        on_page = set(ids.get(k, []))
        in_gtm = set().union(*[set(c.get("ids_inside", {}).get(k, [])) for c in gtm]) if gtm else set()
        if on_page or in_gtm:
            install[k] = "hardcoded + GTM" if on_page and in_gtm else ("GTM" if in_gtm else "hardcoded on page")

    blocked = status != 200 or not home.strip()
    return {
        "url": final,
        "blocked": blocked,
        "blocked_note": "Site refused automated fetch; page-level detection is incomplete. Re-run through a real browser." if blocked else None,
        "domain": domain,
        "http_status": status,
        "title": unescape(title.group(1)).strip() if title else None,
        "pages_scanned": list(pages.keys()),
        "ids": {k: sorted(v) for k, v in all_ids.items()},
        "install_method": install,
        "gtm_containers": gtm,
        "vendors": vendors,
        "verification_tags": verif,
        "hosting": {"headers": hosting_hints, "a_records": doh(domain, "A"), "www_cname": www_cname},
        "dns": {"nameservers": ns, "dns_provider": classify_ns(ns), "mx": mx, "email_provider": classify_mx(mx),
                "txt_of_interest": txt_flags},
        "registration": rdap(domain),
        "site_credits": credits,
        "socials": socials,
        "emails": emails,
        "phones": phones,
        "last_modified": headers.get("last-modified"),
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--pages", type=int, default=8)
    ap.add_argument("--out")
    a = ap.parse_args()
    result = scan(a.url, a.pages)
    text = json.dumps(result, indent=2)
    if a.out:
        with open(a.out, "w") as f:
            f.write(text)
    print(text)
