#!/usr/bin/env python3
"""
Local Authority Statutory Officer Monitor - v2

This version fixes the first-run comparison error and is more conservative
about extracting names. It will NOT treat generic phrases such as
"borough councillor" or "Worthing Councils" as an officer name.

It also records HTTP 403/other errors rather than stopping the whole run.

Files created:
  data/officers.json
  data/search_results.json
  data/history.json
"""

import csv
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
DATA.mkdir(exist_ok=True)

COUNCILS_FILE = BASE / "councils.csv"
OFFICERS_FILE = DATA / "officers.json"
HISTORY_FILE = DATA / "history.json"
RESULTS_FILE = DATA / "search_results.json"

MAX_PAGES_PER_COUNCIL = 20
REQUEST_TIMEOUT = 20
SLEEP_BETWEEN_REQUESTS = 0.25

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; LocalAuthorityOfficerMonitor/2.0; "
        "+https://github.com/)"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

ROLE_PATTERNS = {
    "Chief Executive": [
        r"\bchief executive\b",
        r"\bhead of paid service\b",
    ],
    "Monitoring Officer": [
        r"\bmonitoring officer\b",
    ],
    "Section 151 Officer": [
        r"\bsection\s*151\s*officer\b",
        r"\bsection\s*151\b",
        r"\bs\.?\s*151\s*officer\b",
        r"\bs\.?\s*151\b",
        r"\bchief finance officer\b",
        r"\bchief financial officer\b",
    ],
}

# Names generally have 2-4 words beginning with capitals. This is only a
# candidate detector; the surrounding role wording is what gives confidence.
NAME = r"[A-Z][A-Za-z'’\-]{1,30}(?:\s+[A-Z][A-Za-z'’\-]{1,30}){1,3}"

BAD_PHRASES = {
    "Worthing Councils",
    "Borough Councillor",
    "District Councillor",
    "City Council",
    "County Council",
    "Local Authority",
    "Senior Leadership",
    "Senior Management",
    "Management Team",
    "Corporate Leadership",
    "Corporate Management",
    "Executive Director",
    "Chief Executive Officer",
    "Chief Executive",
    "Monitoring Officer",
    "Section Officer",
    "Section 151",
    "Chief Finance Officer",
    "Chief Financial Officer",
    "Head Paid Service",
    "Council Offices",
    "Contact Us",
    "Current Officer",
    "Potential New Officer",
}

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path, data):
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def load_councils():
    if not COUNCILS_FILE.exists():
        raise FileNotFoundError(
            "councils.csv is missing. Put it in the same folder as monitor.py."
        )

    with COUNCILS_FILE.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        raise ValueError("councils.csv is empty.")

    return rows


def selected_councils(rows):
    selected_values = {"yes", "true", "1"}

    explicitly_selected = [
        row for row in rows
        if str(row.get("selected_for_search", "")).strip().lower()
        in selected_values
    ]

    # If the CSV has selections, honour them.
    if explicitly_selected:
        return [
            row for row in explicitly_selected
            if row.get("website", "").strip()
        ]

    # Otherwise search every council with a website.
    return [
        row for row in rows
        if row.get("website", "").strip()
    ]


def normalise_url(url):
    if not url:
        return ""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url.rstrip("/") + "/"


def fetch(url):
    try:
        response = requests.get(
            url,
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        response.raise_for_status()
        return response, None
    except requests.RequestException as exc:
        return None, str(exc)


def html_to_text(html):
    soup = BeautifulSoup(html, "html.parser")

    for element in soup(["script", "style", "noscript", "svg"]):
        element.decompose()

    lines = []
    for line in soup.get_text("\n").splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line)

    return "\n".join(lines)


def same_domain(a, b):
    return urlparse(a).netloc.lower() == urlparse(b).netloc.lower()


def get_links(page_url, html):
    soup = BeautifulSoup(html, "html.parser")
    links = []

    for tag in soup.find_all("a", href=True):
        href = tag.get("href", "").strip()
        if not href:
            continue

        absolute = urljoin(page_url, href)
        parsed = urlparse(absolute)

        if parsed.scheme not in {"http", "https"}:
            continue
        if not same_domain(absolute, page_url):
            continue

        lower = absolute.lower()

        if any(x in lower for x in (
            "login", "logout", "cookie", "privacy",
            "facebook", "twitter", "instagram", "youtube"
        )):
            continue

        links.append(absolute)

    return list(dict.fromkeys(links))


def link_priority(url):
    lower = url.lower()
    score = 0

    keywords = (
        "chief", "executive", "monitoring", "section",
        "s151", "finance", "officer", "management",
        "structure", "constitution", "governance",
        "organisation", "organisational", "committee",
        "senior", "leadership"
    )

    for word in keywords:
        if word in lower:
            score += 5

    return score


def crawl_council(website, seed_url):
    website = normalise_url(website)
    seed_url = normalise_url(seed_url) if seed_url else website

    queue = []
    for url in (seed_url, website):
        if url and url not in queue:
            queue.append(url)

    seen = set()
    pages = []
    errors = []

    while queue and len(seen) < MAX_PAGES_PER_COUNCIL:
        url = queue.pop(0)

        if url in seen:
            continue

        seen.add(url)
        response, error = fetch(url)

        if error:
            errors.append({
                "url": url,
                "error": error,
            })
            print(f"      Could not open {url}: {error}")
            continue

        content_type = response.headers.get("content-type", "").lower()

        # PDF discovery is retained as evidence. PDF text extraction is a
        # separate enhancement; the program does not pretend to have read it.
        if "pdf" in content_type or url.lower().endswith(".pdf"):
            pages.append({
                "url": url,
                "text": "",
                "type": "pdf",
            })
            continue

        text = html_to_text(response.text)

        pages.append({
            "url": url,
            "text": text,
            "type": "html",
        })

        links = get_links(url, response.text)
        links.sort(key=link_priority, reverse=True)

        for link in links[:12]:
            if link not in seen:
                queue.append(link)

        time.sleep(SLEEP_BETWEEN_REQUESTS)

    return pages, errors


def clean_candidate(candidate):
    candidate = re.sub(r"\s+", " ", candidate).strip(" ,:;.-")

    if candidate in BAD_PHRASES:
        return None

    if any(candidate.lower() == phrase.lower() for phrase in BAD_PHRASES):
        return None

    # Don't accept a candidate containing obvious job/organisation words.
    forbidden = {
        "council", "councillor", "officer", "executive", "director",
        "service", "finance", "monitoring", "section", "management",
        "leadership", "borough", "district", "authority", "committee",
        "department", "team", "town", "hall"
    }

    if any(word in forbidden for word in candidate.lower().split()):
        return None

    return candidate


def candidates_from_line(line, role):
    """
    Strong patterns only:
      "Our Chief Executive is Jane Smith"
      "Jane Smith - Chief Executive"
      "Chief Executive: Jane Smith"
      "Jane Smith, Chief Executive"
    """

    role_regex = "|".join(ROLE_PATTERNS[role])

    patterns = [
        rf"\b(?:our\s+)?(?:{role_regex})\s+(?:is|:|-)\s+({NAME})\b",
        rf"\b({NAME})\s+(?:is\s+)?(?:-|–|—|:|,)\s*(?:{role_regex})\b",
        rf"\b({NAME}),\s*(?:who\s+is\s+)?(?:also\s+)?(?:the\s+)?(?:{role_regex})\b",
        rf"\b({NAME})\s*\((?:{role_regex})\)\b",
    ]

    for pattern in patterns:
        match = re.search(pattern, line, re.IGNORECASE)
        if match:
            candidate = clean_candidate(match.group(1))
            if candidate:
                return candidate

    return None


def extract_role(pages, role):
    findings = []

    for page in pages:
        text = page.get("text", "")
        if not text:
            continue

        lines = [
            re.sub(r"\s+", " ", line).strip()
            for line in text.splitlines()
            if line.strip()
        ]

        role_regex = re.compile(
            "|".join(ROLE_PATTERNS[role]),
            re.IGNORECASE,
        )

        for i, line in enumerate(lines):
            if not role_regex.search(line):
                continue

            # First: exact same-line extraction.
            candidate = candidates_from_line(line, role)
            if candidate:
                findings.append({
                    "name": candidate,
                    "source": page["url"],
                    "method": "same-line role/name match",
                })
                continue

            # Second: inspect adjacent lines, but only where the adjacent
            # line is itself a short, plausible name.
            for nearby in lines[max(0, i - 2): min(len(lines), i + 3)]:
                if nearby == line:
                    continue

                if len(nearby.split()) > 5:
                    continue

                match = re.fullmatch(NAME, nearby)
                if match:
                    candidate = clean_candidate(match.group(1))
                    if candidate:
                        findings.append({
                            "name": candidate,
                            "source": page["url"],
                            "method": "adjacent-line role/name match",
                        })

    if not findings:
        return {
            "name": None,
            "source": None,
            "confidence": "Not found",
        }

    counts = {}
    for item in findings:
        counts[item["name"]] = counts.get(item["name"], 0) + 1

    best_name = max(counts, key=counts.get)
    best = next(x for x in findings if x["name"] == best_name)

    # One strong same-line hit is more useful than a random nearby name.
    if best["method"] == "same-line role/name match":
        confidence = "Medium"
    else:
        confidence = "Low"

    return {
        "name": best_name,
        "source": best["source"],
        "confidence": confidence,
        "method": best["method"],
    }


def scan_council(row):
    council = row["council"].strip()

    print(f"\nScanning: {council}")

    pages, errors = crawl_council(
        row.get("website", ""),
        row.get("seed_url", ""),
    )

    result = {
        "council": council,
        "website": row.get("website", ""),
        "checked_at": now_iso(),
        "pages_checked": len(pages),
        "errors": errors,
        "roles": {},
    }

    for role in ROLE_PATTERNS:
        finding = extract_role(pages, role)
        result["roles"][role] = finding

        print(
            f"  {role}: "
            f"{finding['name'] or 'Not found'} "
            f"[{finding['confidence']}]"
        )

    if errors:
        print(f"  Website access warnings: {len(errors)}")

    return result


def compare_with_previous(previous, current):
    """
    IMPORTANT FIX:
    The previous version accidentally iterated over the dictionary
    {"councils": [...]} rather than over its "councils" list. That caused:

      TypeError: string indices must be integers

    This version correctly uses current.get("councils", []).
    """

    previous_by_council = {
        item["council"]: item
        for item in previous.get("councils", [])
        if isinstance(item, dict) and item.get("council")
    }

    changes = []

    for council in current.get("councils", []):
        if not isinstance(council, dict):
            continue

        old = previous_by_council.get(council.get("council"))

        if not old:
            continue

        for role in ROLE_PATTERNS:
            old_role = old.get("roles", {}).get(role, {})
            new_role = council.get("roles", {}).get(role, {})

            old_name = old_role.get("name")
            new_name = new_role.get("name")

            # Never treat "not found" as a change.
            if not old_name or not new_name:
                continue

            if old_name.lower() == new_name.lower():
                continue

            # Low-confidence extraction should not automatically become
            # a potential appointment change.
            if new_role.get("confidence") == "Low":
                continue

            changes.append({
                "detected_at": now_iso(),
                "council": council["council"],
                "role": role,
                "previous_officer": old_name,
                "current_officer": new_name,
                "previous_source": old_role.get("source"),
                "current_source": new_role.get("source"),
                "confidence": "Potential change",
            })

    return changes


def main():
    print("=" * 60)
    print("LOCAL AUTHORITY STATUTORY OFFICER MONITOR v2")
    print("=" * 60)

    rows = load_councils()
    selected = selected_councils(rows)

    print(f"\nCouncils in CSV: {len(rows)}")
    print(f"Councils selected for this run: {len(selected)}")

    if not selected:
        print(
            "\nNo councils are selected and have a website address.\n"
            "Open councils.csv and set selected_for_search to yes."
        )
        return

    current_results = []

    for row in selected:
        try:
            current_results.append(scan_council(row))
        except Exception as exc:
            print(f"  ERROR scanning {row.get('council')}: {exc}")
            current_results.append({
                "council": row.get("council"),
                "website": row.get("website", ""),
                "checked_at": now_iso(),
                "pages_checked": 0,
                "errors": [{"error": str(exc)}],
                "roles": {},
            })

    previous = load_json(
        OFFICERS_FILE,
        {"generated_at": None, "councils": []},
    )

    current_document_for_compare = {
        "councils": current_results
    }

    changes = compare_with_previous(
        previous,
        current_document_for_compare,
    )

    current_document = {
        "generated_at": now_iso(),
        "councils": current_results,
    }

    save_json(OFFICERS_FILE, current_document)

    search_document = {
        "generated_at": now_iso(),
        "councils_searched": len(selected),
        "results": current_results,
        "potential_changes": changes,
    }

    save_json(RESULTS_FILE, search_document)

    history = load_json(
        HISTORY_FILE,
        {"changes": []},
    )

    history.setdefault("changes", []).extend(changes)
    save_json(HISTORY_FILE, history)

    print("\n" + "=" * 60)
    print("SEARCH COMPLETE")
    print(f"Potential changes detected: {len(changes)}")
    print(f"Current officers saved to: {OFFICERS_FILE}")
    print(f"Search results saved to: {RESULTS_FILE}")
    print(f"Change history saved to: {HISTORY_FILE}")
    print("=" * 60)

    if changes:
        print("\nPOTENTIAL CHANGES:")
        for change in changes:
            print(
                f"- {change['council']} | {change['role']} | "
                f"{change['previous_officer']} -> "
                f"{change['current_officer']}"
            )


if __name__ == "__main__":
    main()
