#!/usr/bin/env python3
"""
Local Authority Statutory Officer Monitor - v5

v5 is deliberately source-led for the first 10 councils.

Instead of crawling a large number of arbitrary pages and guessing which
nearby capitalised words are names, it:
  1. Reads officer_sources.csv.
  2. Checks the specified official council source pages/documents first.
  3. Follows only a small number of highly relevant links from those sources.
  4. Extracts a person only when the person's name is structurally associated
     with the statutory role.
  5. Uses a conservative "Not verified" result when the evidence is weak.
  6. Keeps the official source URL with every result.
  7. Supports HTML and PDF sources.
  8. Uses a Jina Reader fallback only when a council blocks the GitHub runner.
     The result still points to the original official council URL.

This version is intentionally focused on the first 10 councils. Once these
are reliable, the same source-led configuration can be expanded to all 317.
"""

import csv
import io
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

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

TIMEOUT = 30
MAX_FOLLOW_LINKS = 8

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; LocalAuthorityOfficerMonitor/5.0)",
    "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
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
        r"\bhead of legal and monitoring officer\b",
    ],
    "Section 151 Officer": [
        r"\bsection\s*151\s*officer\b",
        r"\bs\.?\s*151\s*officer\b",
        r"\bchief finance officer\b",
        r"\bchief financial officer\b",
        r"\bfinance director\b",
    ],
}

# Strong name pattern. We only use this after finding an explicit role label.
NAME = r"[A-Z][A-Za-z'’\-]{1,30}(?:\s+[A-Z][A-Za-z'’\-]{1,30}){1,3}"

NON_NAMES = {
    "Chief Executive", "Monitoring Officer", "Section 151 Officer",
    "Chief Finance Officer", "Chief Financial Officer", "Head Paid Service",
    "Executive Director", "Strategic Director", "Corporate Director",
    "Senior Leadership Team", "Management Team", "Council Structure",
    "Our People", "Contact Details", "Current Officer", "Not Found",
    "Car Parks", "Emergency Planning", "Core Services", "Worthing Councils",
    "responsible for", "responsible for overseeing investigations",
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
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )

def normalise(url):
    if not url:
        return ""
    return url.strip()

def direct_get(url):
    try:
        r = requests.get(
            url,
            headers=HEADERS,
            timeout=TIMEOUT,
            allow_redirects=True,
        )
        r.raise_for_status()
        return r, None
    except requests.RequestException as exc:
        return None, str(exc)

def get_with_fallback(url):
    """
    First try the official council URL directly.
    If GitHub's runner is blocked, try Jina Reader as a transport fallback.
    The evidence URL remains the official council URL.
    """
    r, error = direct_get(url)
    if r is not None:
        return r, None, False

    # Do not use the fallback for non-HTTP URLs.
    if not url.startswith(("http://", "https://")):
        return None, error, False

    proxy = "https://r.jina.ai/" + url
    try:
        pr = requests.get(proxy, headers=HEADERS, timeout=TIMEOUT)
        pr.raise_for_status()

        # Make a response-like object sufficient for downstream parsing.
        class ProxyResponse:
            status_code = pr.status_code
            headers = {"content-type": "text/plain; charset=utf-8"}
            text = pr.text
            content = pr.content

        return ProxyResponse(), None, True
    except requests.RequestException:
        return None, error, False

def is_pdf(url, response):
    ctype = response.headers.get("content-type", "").lower()
    return "pdf" in ctype or url.lower().endswith(".pdf")

def html_text(raw):
    soup = BeautifulSoup(raw, "html.parser")
    for x in soup(["script", "style", "noscript", "svg"]):
        x.decompose()

    lines = []
    for x in soup.get_text("\n").splitlines():
        x = re.sub(r"\s+", " ", x).strip()
        if x:
            lines.append(x)
    return "\n".join(lines)

def pdf_text(content):
    try:
        reader = PdfReader(io.BytesIO(content))
        return "\n".join(
            (page.extract_text() or "")
            for page in reader.pages[:80]
        )
    except Exception:
        return ""

def source_document(url):
    response, error, used_proxy = get_with_fallback(url)

    if response is None:
        return {
            "url": url,
            "text": "",
            "error": error,
            "used_proxy": False,
        }

    if is_pdf(url, response):
        text = pdf_text(response.content)
        kind = "pdf"
    else:
        text = html_text(response.text)
        kind = "html"

    return {
        "url": url,
        "text": text,
        "kind": kind,
        "error": None,
        "used_proxy": used_proxy,
    }

def relevant_links(source_url, raw_html):
    """
    Only discover links that look directly relevant to statutory officers.
    This prevents the false positives produced by the old 35-page crawler.
    """
    soup = BeautifulSoup(raw_html, "html.parser")
    host = urlparse(source_url).netloc.lower()

    keywords = (
        "chief", "executive", "monitoring", "finance", "section",
        "s151", "officer", "management", "structure", "governance",
        "constitution", "leadership", "accounts", "annual-governance"
    )

    candidates = []

    for a in soup.find_all("a", href=True):
        url = urljoin(source_url, a["href"])
        parsed = urlparse(url)

        if parsed.scheme not in ("http", "https"):
            continue
        if parsed.netloc.lower() != host:
            continue

        label = (a.get_text(" ", strip=True) + " " + url).lower()
        score = sum(1 for word in keywords if word in label)

        if score:
            candidates.append((score, url))

    candidates.sort(reverse=True)
    return list(dict.fromkeys(url for _, url in candidates))[:MAX_FOLLOW_LINKS]

def clean_name(value):
    value = re.sub(r"\s+", " ", value).strip(" ,:;.-–—")

    if not value:
        return None

    if value.lower() in {x.lower() for x in NON_NAMES}:
        return None

    forbidden = {
        "council", "councillor", "officer", "executive", "director",
        "service", "finance", "monitoring", "section", "management",
        "leadership", "authority", "committee", "department", "team",
        "car", "parks", "emergency", "planning", "responsible",
    }

    if any(word in forbidden for word in value.lower().split()):
        return None

    # Reject sentences masquerading as names.
    if len(value.split()) > 4:
        return None

    return value

def explicit_role_patterns(role):
    role_regex = "|".join(ROLES[role])

    # We deliberately require a role/name relationship.
    return [
        rf"(?:{role_regex})\s*(?:is|:|-|–|—)\s*(?P<name>{NAME})",
        rf"(?P<name>{NAME})\s*(?:-|–|—|:|,)\s*(?:{role_regex})",
        rf"(?P<name>{NAME})\s*,?\s*(?:who\s+is\s+)?(?:the\s+)?(?:{role_regex})",
        rf"(?P<name>{NAME})\s*\((?:{role_regex})\)",
    ]

def extract_from_text(text, role, source_url):
    """
    Return only strong matches.

    We also support the common council table format:
      Name | Position
      Jane Smith | Chief Executive

    and:
      Chief Executive
      Jane Smith
    """
    findings = []

    lines = [
        re.sub(r"\s+", " ", x).strip()
        for x in text.splitlines()
        if x.strip()
    ]

    role_regex = re.compile(
        "|".join(ROLES[role]),
        re.IGNORECASE,
    )

    patterns = explicit_role_patterns(role)

    for i, line in enumerate(lines):
        if not role_regex.search(line):
            continue

        # 1. Strong same-line relationship.
        for pattern in patterns:
            match = re.search(pattern, line, re.IGNORECASE)
            if match:
                candidate = clean_name(
                    match.groupdict().get("name", match.group(0))
                )
                if candidate:
                    findings.append((candidate, source_url, "explicit"))
                    break

        # 2. Table-like / heading relationship.
        # Check only a very small neighbourhood and require the adjacent
        # line to be a plausible name by itself.
        for j in (i - 1, i + 1):
            if j < 0 or j >= len(lines):
                continue

            nearby = lines[j]

            if len(nearby.split()) > 4:
                continue

            match = re.fullmatch(NAME, nearby)
            if match:
                candidate = clean_name(match.group(0))
                if candidate:
                    findings.append((candidate, source_url, "adjacent"))

        # 3. HTML tables can collapse to "Name Position". Try splitting on
        # common separators only.
        for sep in (" | ", " – ", " - ", "\t"):
            if sep in line:
                parts = [x.strip() for x in line.split(sep) if x.strip()]
                if len(parts) == 2:
                    if role_regex.search(parts[0]):
                        candidate = clean_name(parts[1])
                        if candidate:
                            findings.append((candidate, source_url, "table"))
                    elif role_regex.search(parts[1]):
                        candidate = clean_name(parts[0])
                        if candidate:
                            findings.append((candidate, source_url, "table"))

    if not findings:
        return None

    # Explicit matches outrank adjacent matches.
    rank = {"explicit": 3, "table": 3, "adjacent": 1}
    findings.sort(key=lambda x: rank.get(x[2], 0), reverse=True)

    best = findings[0]

    # If there are conflicting candidates, don't guess.
    top_rank = rank.get(best[2], 0)
    top = [x for x in findings if rank.get(x[2], 0) == top_rank]
    names = {x[0].lower() for x in top}

    if len(names) > 1:
        return {
            "name": None,
            "source": source_url,
            "confidence": "Conflicting evidence",
            "candidates": sorted({x[0] for x in top}),
        }

    return {
        "name": best[0],
        "source": best[1],
        "confidence": "High" if best[2] in ("explicit", "table") else "Medium",
    }

def load_sources():
    if not SOURCES_FILE.exists():
        raise FileNotFoundError(
            "officer_sources.csv is missing. Upload it beside monitor.py."
        )

    with SOURCES_FILE.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))

def load_council_selection():
    if not COUNCILS_FILE.exists():
        return {}

    with COUNCILS_FILE.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    selected = {}
    for row in rows:
        if str(row.get("selected_for_search", "")).strip().lower() in {
            "yes", "true", "1"
        }:
            selected[row["council"].strip()] = True
    return selected

def scan_council(source_row):
    council = source_row["council"].strip()

    print(f"\nScanning: {council}")

    urls = [
        source_row.get("primary_source", ""),
        source_row.get("secondary_source", ""),
        source_row.get("tertiary_source", ""),
    ]
    urls = [normalise(x) for x in urls if normalise(x)]

    documents = []
    errors = []

    for url in urls:
        doc = source_document(url)
        documents.append(doc)

        if doc.get("error"):
            errors.append({
                "url": url,
                "error": doc["error"],
                "used_proxy": doc.get("used_proxy", False),
            })
            print("  Source unavailable:", url)

    # For HTML primary/secondary pages, follow only a few highly relevant
    # links. This is a controlled expansion, not a broad website crawl.
    for doc in list(documents):
        if doc.get("kind") != "html" or not doc.get("text"):
            continue

        # Fetch the original page again only when we need its links.
        response, error, _ = get_with_fallback(doc["url"])
        if response is None:
            continue

        extra = relevant_links(doc["url"], response.text)
        existing = {x["url"] for x in documents}

        for url in extra:
            if url in existing or len(documents) >= 15:
                continue
            extra_doc = source_document(url)
            documents.append(extra_doc)
            existing.add(url)

    roles = {}

    for role in ROLES:
        candidates = []

        for doc in documents:
            if not doc.get("text"):
                continue

            result = extract_from_text(
                doc["text"],
                role,
                doc["url"],
            )

            if result:
                candidates.append(result)

        # Prefer High over Medium. Never promote a weak adjacent match if
        # there is no strong evidence.
        high = [x for x in candidates if x["confidence"] == "High"]
        medium = [x for x in candidates if x["confidence"] == "Medium"]

        if high:
            # If multiple high-confidence sources disagree, flag it.
            names = {x["name"].lower() for x in high if x.get("name")}
            if len(names) == 1:
                roles[role] = high[0]
            else:
                roles[role] = {
                    "name": None,
                    "source": high[0].get("source"),
                    "confidence": "Conflicting evidence",
                    "candidates": sorted({
                        x["name"] for x in high if x.get("name")
                    }),
                }
        elif medium:
            names = {x["name"].lower() for x in medium if x.get("name")}
            if len(names) == 1:
                roles[role] = medium[0]
            else:
                roles[role] = {
                    "name": None,
                    "source": medium[0].get("source"),
                    "confidence": "Conflicting evidence",
                    "candidates": sorted({
                        x["name"] for x in medium if x.get("name")
                    }),
                }
        else:
            roles[role] = {
                "name": None,
                "source": None,
                "confidence": "Not verified",
            }

        item = roles[role]
        print(
            f"  {role}: {item.get('name') or 'Not verified'} "
            f"[{item.get('confidence')}]"
        )

    return {
        "council": council,
        "checked_at": now(),
        "sources_checked": [d["url"] for d in documents],
        "errors": errors,
        "roles": roles,
    }

def detect_changes(previous, current):
    old = {
        x.get("council"): x
        for x in previous.get("councils", [])
        if isinstance(x, dict)
    }

    changes = []

    for council in current:
        previous_council = old.get(council["council"])
        if not previous_council:
            continue

        for role in ROLES:
            before = previous_council.get("roles", {}).get(role, {})
            after = council.get("roles", {}).get(role, {})

            old_name = before.get("name")
            new_name = after.get("name")

            if not old_name or not new_name:
                continue

            if old_name.lower() == new_name.lower():
                continue

            # Only a high-confidence new result can trigger a potential
            # change. Weak evidence must be reviewed manually.
            if after.get("confidence") != "High":
                continue

            changes.append({
                "detected_at": now(),
                "council": council["council"],
                "role": role,
                "previous_officer": old_name,
                "current_officer": new_name,
                "previous_source": before.get("source"),
                "current_source": after.get("source"),
                "status": "Potential change - review source",
            })

    return changes

def main():
    print("=" * 60)
    print("LOCAL AUTHORITY STATUTORY OFFICER MONITOR v5")
    print("=" * 60)

    sources = load_sources()
    selected = load_council_selection()

    # v5 is deliberately limited to the first 10 source definitions.
    if selected:
        sources = [
            row for row in sources
            if row["council"].strip() in selected
        ]

    print(f"\nSource configurations: {len(sources)}")

    current = []

    for row in sources:
        try:
            current.append(scan_council(row))
        except Exception as exc:
            print(f"  ERROR: {exc}")
            current.append({
                "council": row["council"],
                "checked_at": now(),
                "sources_checked": [],
                "errors": [{"error": str(exc)}],
                "roles": {
                    role: {
                        "name": None,
                        "source": None,
                        "confidence": "Error",
                    }
                    for role in ROLES
                },
            })

    previous = load_json(
        OFFICERS_FILE,
        {"generated_at": None, "councils": []},
    )

    changes = detect_changes(previous, current)

    # First run establishes a baseline. It does not call initial differences
    # "changes".
    if not previous.get("councils"):
        changes = []

    save_json(
        OFFICERS_FILE,
        {
            "generated_at": now(),
            "monitor_version": "v5",
            "councils": current,
        },
    )

    save_json(
        RESULTS_FILE,
        {
            "generated_at": now(),
            "monitor_version": "v5",
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
    print(f"Councils searched: {len(current)}")
    print(f"Potential changes: {len(changes)}")
    print("=" * 60)

if __name__ == "__main__":
    main()
