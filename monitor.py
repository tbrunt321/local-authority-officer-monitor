#!/usr/bin/env python3
"""
Local Authority Statutory Officer Monitor - v6

Multi-source prototype for the first 10 councils.

Evidence layers:
1. Official council sources configured in officer_sources.csv.
2. Official council committee/ModernGov material discovered from source pages.
3. Web/news search via Bing HTML results (no API key required for prototype).
4. Search-result snippets are treated as LEADS, not confirmation.
5. Official council pages/documents are preferred for confirmation.

Outputs:
  data/officers.json
  data/search_results.json
  data/history.json

Statuses:
  Confirmed       = strong official evidence
  Incoming        = future appointment explicitly identified
  Potential change = strong secondary evidence or conflicting current evidence
  Departure       = departure identified without confirmed replacement
  Not verified    = insufficient evidence

The program is deliberately conservative. It should never turn a generic
phrase into an officer name.
"""

import csv
import io
import json
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
DATA.mkdir(exist_ok=True)

COUNCILS_FILE = BASE / "councils.csv"
SOURCES_FILE = BASE / "officer_sources.csv"
OFFICERS_FILE = DATA / "officers.json"
RESULTS_FILE = DATA / "search_results.json"
HISTORY_FILE = DATA / "history.json"

TIMEOUT = 25
MAX_WEB_RESULTS_PER_QUERY = 8
MAX_OFFICIAL_LINKS = 8

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/151 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

ROLES = {
    "Chief Executive": [
        r"\bchief executive\b",
        r"\bhead of paid service\b",
    ],
    "Monitoring Officer": [
        r"\bmonitoring officer\b",
        r"\bsolicitor to the council and monitoring officer\b",
    ],
    "Section 151 Officer": [
        r"\bsection\s*151\s*officer\b",
        r"\bs\.?\s*151\s*officer\b",
        r"\bchief finance officer\b",
        r"\bchief financial officer\b",
    ],
}

NAME = r"[A-Z][A-Za-z'’\-]{1,30}(?:\s+[A-Z][A-Za-z'’\-]{1,30}){1,3}"

BAD_NAME_PARTS = {
    "council", "councillor", "officer", "executive", "director",
    "service", "finance", "monitoring", "section", "management",
    "leadership", "authority", "committee", "department", "team",
    "planning", "car", "parks", "emergency", "responsible", "role",
    "position", "support", "statutory", "current", "former", "interim",
}

BAD_NAMES = {
    "Chief Executive", "Monitoring Officer", "Section 151 Officer",
    "Chief Finance Officer", "Chief Financial Officer",
    "Head of Paid Service", "Senior Leadership Team",
    "Management Team", "Core Services", "Car Parks",
    "Emergency Planning", "Worthing Councils",
}

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default

def save_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

def direct_get(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        r.raise_for_status()
        return r, None
    except requests.RequestException as exc:
        return None, str(exc)

def get_url(url):
    r, err = direct_get(url)
    if r:
        return r, err, False

    # GitHub-hosted runners can be blocked. Use Jina only as a transport
    # fallback; retain the original URL as the evidence URL.
    if url.startswith(("http://", "https://")):
        proxy = "https://r.jina.ai/" + url
        try:
            p = requests.get(proxy, headers=HEADERS, timeout=TIMEOUT)
            p.raise_for_status()

            class ProxyResponse:
                status_code = p.status_code
                headers = {"content-type": "text/plain; charset=utf-8"}
                text = p.text
                content = p.content

            return ProxyResponse(), None, True
        except requests.RequestException:
            pass

    return None, err, False

def html_text(raw):
    soup = BeautifulSoup(raw, "html.parser")
    for x in soup(["script", "style", "noscript", "svg"]):
        x.decompose()
    return "\n".join(
        re.sub(r"\s+", " ", x).strip()
        for x in soup.get_text("\n").splitlines()
        if x.strip()
    )

def pdf_text(content):
    try:
        reader = PdfReader(io.BytesIO(content))
        return "\n".join((p.extract_text() or "") for p in reader.pages[:80])
    except Exception:
        return ""

def get_document(url):
    r, error, proxy = get_url(url)
    if not r:
        return {"url": url, "text": "", "error": error, "proxy": False}

    ctype = r.headers.get("content-type", "").lower()
    ispdf = "pdf" in ctype or url.lower().endswith(".pdf")
    text = pdf_text(r.content) if ispdf else html_text(r.text)

    return {
        "url": url,
        "text": text,
        "kind": "pdf" if ispdf else "html",
        "error": None,
        "proxy": proxy,
    }

def clean_name(name):
    name = re.sub(r"\s+", " ", name).strip(" ,:;.-–—")
    if not name or name.lower() in {x.lower() for x in BAD_NAMES}:
        return None

    parts = name.lower().split()
    if any(p in BAD_NAME_PARTS for p in parts):
        return None
    if len(parts) > 4:
        return None

    # Prevent obvious sentence fragments.
    if any(x in name.lower() for x in (
        " is ", " are ", " was ", " were ", " will ",
        " and ", " the ", " our ", " who ", " has ",
    )):
        return None

    return name

def role_regex(role):
    return "|".join(ROLES[role])

def extract_role(text, role, source_url):
    """
    Only accept:
      Role: Name
      Role - Name
      Name - Role
      Name, Role
      Name (Role)
      table-style Role | Name

    Do NOT use arbitrary nearby capitalised words.
    """
    rr = role_regex(role)
    patterns = [
        rf"(?:{rr})\s*(?:is|:|-|–|—)\s*(?P<name>{NAME})",
        rf"(?P<name>{NAME})\s*(?:-|–|—|:|,)\s*(?:{rr})",
        rf"(?P<name>{NAME})\s*,\s*(?:who\s+is\s+)?(?:the\s+)?(?:{rr})",
        rf"(?P<name>{NAME})\s*\((?:{rr})\)",
    ]

    results = []

    # HTML/PDF extraction can join lines, so inspect both whole text and lines.
    chunks = [text]
    chunks.extend(
        re.sub(r"\s+", " ", line).strip()
        for line in text.splitlines()
        if line.strip()
    )

    for chunk in chunks:
        if not re.search(rr, chunk, re.I):
            continue

        for pat in patterns:
            m = re.search(pat, chunk, re.I)
            if not m:
                continue

            name = clean_name(m.groupdict().get("name", ""))
            if name:
                results.append({
                    "name": name,
                    "source": source_url,
                    "evidence": chunk[:500],
                    "match_type": "explicit",
                })

        # Table separator format.
        for sep in (" | ", " – ", " — ", "\t"):
            if sep not in chunk:
                continue
            parts = [p.strip() for p in chunk.split(sep) if p.strip()]
            if len(parts) == 2:
                if re.search(rr, parts[0], re.I):
                    name = clean_name(parts[1])
                    if name:
                        results.append({
                            "name": name,
                            "source": source_url,
                            "evidence": chunk[:500],
                            "match_type": "table",
                        })
                elif re.search(rr, parts[1], re.I):
                    name = clean_name(parts[0])
                    if name:
                        results.append({
                            "name": name,
                            "source": source_url,
                            "evidence": chunk[:500],
                            "match_type": "table",
                        })

    # Deduplicate exact candidates.
    unique = {}
    for item in results:
        unique[(item["name"].lower(), item["source"])] = item

    return list(unique.values())

def search_web(query):
    """
    Search Bing's public HTML results. This is a prototype and deliberately
    uses snippets as discovery evidence, not final confirmation.
    """
    url = "https://www.bing.com/search?q=" + quote_plus(query)
    r, error, _ = get_url(url)
    if not r:
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    results = []

    for li in soup.select("li.b_algo"):
        a = li.select_one("h2 a")
        if not a:
            continue
        href = a.get("href", "")
        title = a.get_text(" ", strip=True)
        snippet = li.select_one(".b_caption p")
        snippet = snippet.get_text(" ", strip=True) if snippet else ""

        if href.startswith("http"):
            results.append({
                "title": title,
                "url": href,
                "snippet": snippet,
                "source_type": "web_search",
            })

        if len(results) >= MAX_WEB_RESULTS_PER_QUERY:
            break

    return results

def extract_web_leads(council, role, results):
    """
    Look for explicit appointment/change language in search results.
    Search snippets never become Confirmed.
    """
    rr = role_regex(role)
    leads = []

    change_words = re.compile(
        r"\b(appointed|appointment|joins|joined|new|incoming|"
        r"takes up|take up|will become|will be|designated|"
        r"designated as|leaving|leaves|departure|resigns|resigned|"
        r"interim|effective from|with effect from)\b",
        re.I,
    )

    for result in results:
        text = f"{result['title']} {result['snippet']}"
        if not re.search(rr, text, re.I):
            continue
        if not change_words.search(text):
            continue

        for pattern in [
            rf"(?:{rr}).{{0,80}}?(?P<name>{NAME})",
            rf"(?P<name>{NAME}).{{0,80}}?(?:{rr})",
        ]:
            m = re.search(pattern, text, re.I)
            if not m:
                continue

            name = clean_name(m.groupdict().get("name", ""))
            if not name:
                continue

            leads.append({
                "council": council,
                "role": role,
                "name": name,
                "url": result["url"],
                "title": result["title"],
                "snippet": result["snippet"],
                "status": "Potential change",
                "evidence_type": "web/news lead",
            })
            break

    return leads

def source_urls_for(row):
    return [
        row.get("primary_source", "").strip(),
        row.get("secondary_source", "").strip(),
        row.get("tertiary_source", "").strip(),
    ]

def load_sources():
    with SOURCES_FILE.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))

def selected_names():
    if not COUNCILS_FILE.exists():
        return set()

    with COUNCILS_FILE.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    return {
        r["council"].strip()
        for r in rows
        if r.get("selected_for_search", "").strip().lower()
        in {"yes", "true", "1"}
    }

def council_searches(council, role):
    role_terms = {
        "Chief Executive": '"chief executive"',
        "Monitoring Officer": '"monitoring officer"',
        "Section 151 Officer": '"section 151" "officer"',
    }
    term = role_terms[role]

    # Search both current appointments and changes.
    return [
        f'"{council}" {term}',
        f'"{council}" {term} appointed',
        f'"{council}" {term} "with effect from"',
        f'"{council}" {term} leaving OR departure OR resigns',
    ]

def scan_council(row):
    council = row["council"].strip()
    print(f"\nScanning: {council}")

    documents = []
    errors = []

    for url in source_urls_for(row):
        if not url:
            continue
        doc = get_document(url)
        documents.append(doc)
        if doc.get("error"):
            errors.append({
                "url": url,
                "error": doc["error"],
            })

    official_results = {role: [] for role in ROLES}

    for role in ROLES:
        for doc in documents:
            if not doc.get("text"):
                continue
            official_results[role].extend(
                extract_role(doc["text"], role, doc["url"])
            )

    # Search the wider web. These are leads only.
    web_leads = []

    for role in ROLES:
        for query in council_searches(council, role):
            results = search_web(query)
            web_leads.extend(
                extract_web_leads(council, role, results)
            )
            time.sleep(0.2)

    roles = {}

    for role in ROLES:
        candidates = official_results[role]

        # Strong official evidence only.
        names = {}
        for item in candidates:
            names.setdefault(item["name"].lower(), []).append(item)

        if len(names) == 1:
            best = next(iter(names.values()))[0]
            roles[role] = {
                "name": best["name"],
                "source": best["source"],
                "confidence": "High",
                "status": "Confirmed",
                "evidence": best["evidence"],
            }
        elif len(names) > 1:
            roles[role] = {
                "name": None,
                "source": None,
                "confidence": "Conflicting evidence",
                "status": "Potential change",
                "candidates": sorted({
                    item["name"] for item in candidates
                }),
            }
        else:
            roles[role] = {
                "name": None,
                "source": None,
                "confidence": "Not verified",
                "status": "Not verified",
            }

        # Attach web leads separately. They must never overwrite official
        # current officer information.
        role_leads = [
            x for x in web_leads
            if x["role"] == role
        ]
        if role_leads:
            roles[role]["web_leads"] = role_leads[:5]

        print(
            f"  {role}: {roles[role].get('name') or 'Not verified'} "
            f"[{roles[role].get('status')}]"
        )

    return {
        "council": council,
        "checked_at": now(),
        "roles": roles,
        "sources_checked": [d["url"] for d in documents],
        "errors": errors,
    }

def detect_changes(previous, current):
    old = {
        x.get("council"): x
        for x in previous.get("councils", [])
        if isinstance(x, dict)
    }

    changes = []

    for council in current:
        old_council = old.get(council["council"])
        if not old_council:
            continue

        for role in ROLES:
            before = old_council.get("roles", {}).get(role, {})
            after = council.get("roles", {}).get(role, {})

            old_name = before.get("name")
            new_name = after.get("name")

            if old_name and new_name and old_name.lower() != new_name.lower():
                if after.get("status") == "Confirmed":
                    changes.append({
                        "detected_at": now(),
                        "council": council["council"],
                        "role": role,
                        "previous_officer": old_name,
                        "current_officer": new_name,
                        "status": "Confirmed change - review effective date",
                        "source": after.get("source"),
                    })

            # A web lead suggesting someone new is also surfaced, but as a
            # potential change rather than confirmation.
            for lead in after.get("web_leads", []):
                if not old_name or lead["name"].lower() != old_name.lower():
                    changes.append({
                        "detected_at": now(),
                        "council": council["council"],
                        "role": role,
                        "previous_officer": old_name,
                        "possible_new_officer": lead["name"],
                        "status": "Potential change - secondary source",
                        "source": lead["url"],
                        "title": lead["title"],
                        "snippet": lead["snippet"],
                    })

    return changes

def main():
    print("=" * 60)
    print("LOCAL AUTHORITY STATUTORY OFFICER MONITOR v6")
    print("=" * 60)

    rows = load_sources()
    selected = selected_names()

    if selected:
        rows = [r for r in rows if r["council"].strip() in selected]

    print(f"\nCouncils searched: {len(rows)}")

    current = []

    for row in rows:
        try:
            current.append(scan_council(row))
        except Exception as exc:
            print("  ERROR:", exc)
            current.append({
                "council": row["council"],
                "checked_at": now(),
                "roles": {},
                "sources_checked": [],
                "errors": [{"error": str(exc)}],
            })

    previous = load_json(
        OFFICERS_FILE,
        {"generated_at": None, "councils": []},
    )

    changes = detect_changes(previous, current)

    # First run establishes baseline.
    if not previous.get("councils"):
        changes = []

    save_json(
        OFFICERS_FILE,
        {
            "generated_at": now(),
            "monitor_version": "v6",
            "councils": current,
        },
    )

    save_json(
        RESULTS_FILE,
        {
            "generated_at": now(),
            "monitor_version": "v6",
            "councils_searched": len(current),
            "results": current,
            "potential_changes": changes,
        },
    )

    history = load_json(HISTORY_FILE, {"changes": []})
    history.setdefault("changes", []).extend(changes)
    save_json(HISTORY_FILE, history)

    print("\n" + "=" * 60)
    print("SEARCH COMPLETE")
    print("Potential/confirmed changes:", len(changes))
    print("=" * 60)

if __name__ == "__main__":
    main()
