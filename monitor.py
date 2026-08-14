#!/usr/bin/env python3
"""
Local Authority Statutory Officer Monitor
Prototype monitoring program.

Reads councils.csv, checks selected council websites, looks for:
- Chief Executive / Head of Paid Service
- Monitoring Officer
- Section 151 Officer / Chief Finance Officer

It writes:
- data/officers.json       current snapshot
- data/history.json        detected changes
- data/search_results.json evidence from the latest run

This is a prototype. It is deliberately conservative: a different name is
flagged as a potential change rather than automatically confirmed.
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
        "LocalAuthorityOfficerMonitor/1.0 "
        "(public-sector monitoring prototype)"
    )
}

# Terms used to find pages likely to contain senior-officer information.
PRIORITY_WORDS = (
    "chief", "executive", "monitoring", "section 151", "s151",
    "finance", "officer", "management", "structure", "constitution",
    "governance", "organisation", "organizational", "senior"
)

ROLE_PATTERNS = {
    "Chief Executive": [
        r"\bchief executive\b",
        r"\bchief executive officer\b",
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

# Conservative name pattern. It deliberately avoids trying to infer
# complicated names from arbitrary prose.
NAME_PATTERN = re.compile(
    r"\b([A-Z][A-Za-z'’\-]{1,30}"
    r"(?:\s+[A-Z][A-Za-z'’\-]{1,30}){1,3})\b"
)

COMMON_NON_NAMES = {
    "Chief Executive",
    "Chief Finance Officer",
    "Monitoring Officer",
    "Section Officer",
    "Head Paid Service",
    "Local Authority",
    "Council Officer",
    "Senior Management",
    "Management Team",
    "Corporate Management",
    "Executive Director",
    "Finance Director",
    "Director Finance",
}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_councils():
    if not COUNCILS_FILE.exists():
        raise FileNotFoundError(
            f"Could not find {COUNCILS_FILE}. "
            "Upload councils.csv into the same folder as monitor.py."
        )

    with COUNCILS_FILE.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        raise ValueError("councils.csv is empty.")

    return rows


def selected_councils(rows):
    """
    By default, rows marked selected_for_search=yes are searched.
    If the column is absent/blank for every row, all councils with a website
    are considered selected.
    """
    has_selection = any(
        str(r.get("selected_for_search", "")).strip().lower()
        in {"yes", "true", "1"}
        for r in rows
    )

    if has_selection:
        rows = [
            r for r in rows
            if str(r.get("selected_for_search", "")).strip().lower()
            in {"yes", "true", "1"}
        ]

    return [r for r in rows if str(r.get("website", "")).strip()]


def normalise_url(url):
    url = url.strip()
    if not url:
        return ""
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
        return response
    except requests.RequestException as exc:
        print(f"      Could not open {url}: {exc}")
        return None


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


def same_domain(url_a, url_b):
    return urlparse(url_a).netloc.lower() == urlparse(url_b).netloc.lower()


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

        # Skip things that are unlikely to be useful.
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

    for word in PRIORITY_WORDS:
        if word in lower:
            score += 5

    # Pages/PDFs likely to contain formal officer information.
    for word in (
        "constitution", "committee", "agenda", "minutes",
        "senior-management", "management-structure", "organisation",
        "organisational", "governance"
    ):
        if word in lower:
            score += 10

    return score


def crawl_council(council, website, seed_url=""):
    website = normalise_url(website)
    seed_url = normalise_url(seed_url) if seed_url else website

    queue = [seed_url, website]
    seen = set()
    pages = []

    while queue and len(seen) < MAX_PAGES_PER_COUNCIL:
        url = queue.pop(0)

        if url in seen:
            continue

        seen.add(url)
        response = fetch(url)

        if not response:
            continue

        content_type = response.headers.get("content-type", "").lower()

        # This prototype records PDF links as evidence but does not yet
        # extract PDF text. PDF extraction can be added in the next version.
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

        # Visit the most promising links first.
        links.sort(key=link_priority, reverse=True)

        for link in links[:12]:
            if link not in seen:
                queue.append(link)

        time.sleep(SLEEP_BETWEEN_REQUESTS)

    return pages


def name_candidates(text, role):
    """
    Look around each occurrence of the role title and collect plausible
    names from nearby text.

    This is intentionally heuristic. Results need verification before
    being treated as confirmed appointments.
    """
    lines = [
        re.sub(r"\s+", " ", line).strip()
        for line in text.splitlines()
        if line.strip()
    ]

    candidates = []

    role_regex = re.compile(
        "|".join(ROLE_PATTERNS[role]),
        re.IGNORECASE,
    )

    for index, line in enumerate(lines):
        if not role_regex.search(line):
            continue

        window = lines[
            max(0, index - 3): min(len(lines), index + 4)
        ]

        for nearby in window:
            for match in NAME_PATTERN.finditer(nearby):
                candidate = match.group(1).strip()

                if candidate in COMMON_NON_NAMES:
                    continue

                # Avoid obvious organisational phrases.
                words = candidate.lower().split()
                if any(
                    w in {
                        "council", "officer", "executive", "director",
                        "management", "service", "finance", "committee"
                    }
                    for w in words
                ):
                    continue

                candidates.append(candidate)

    return candidates


def extract_role_from_pages(pages, role):
    findings = []

    for page in pages:
        if not page["text"]:
            continue

        candidates = name_candidates(page["text"], role)

        for candidate in candidates:
            findings.append({
                "name": candidate,
                "source": page["url"],
            })

    if not findings:
        return {
            "name": None,
            "source": None,
            "confidence": "Low",
        }

    # Prefer the most frequently observed name.
    counts = {}
    for finding in findings:
        counts[finding["name"]] = counts.get(finding["name"], 0) + 1

    best_name = max(counts, key=counts.get)
    best_source = next(
        x["source"] for x in findings if x["name"] == best_name
    )

    occurrences = counts[best_name]

    if occurrences >= 3:
        confidence = "Medium"
    else:
        confidence = "Low"

    return {
        "name": best_name,
        "source": best_source,
        "confidence": confidence,
    }


def scan_council(row):
    council = row["council"].strip()
    website = row.get("website", "").strip()
    seed_url = row.get("seed_url", "").strip()

    print(f"\nScanning: {council}")

    pages = crawl_council(council, website, seed_url)

    result = {
        "council": council,
        "website": website,
        "checked_at": now_iso(),
        "roles": {},
        "pages_checked": len(pages),
    }

    for role in ROLE_PATTERNS:
        finding = extract_role_from_pages(pages, role)
        result["roles"][role] = finding

        name = finding["name"] or "Not found"
        print(f"  {role}: {name}")

    return result


def load_json(path, default):
    if not path.exists():
        return default

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path, data):
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def compare_with_previous(previous, current):
    """
    Detects name differences. A difference is labelled POTENTIAL CHANGE,
    not confirmed change.
    """
    previous_by_council = {
        x["council"]: x
        for x in previous.get("councils", [])
    }

    changes = []

    for council in current:
        old = previous_by_council.get(council["council"])

        if not old:
            continue

        for role in ROLE_PATTERNS:
            old_role = old.get("roles", {}).get(role, {})
            new_role = council.get("roles", {}).get(role, {})

            old_name = old_role.get("name")
            new_name = new_role.get("name")

            if (
                old_name
                and new_name
                and old_name.lower() != new_name.lower()
            ):
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
    print("LOCAL AUTHORITY STATUTORY OFFICER MONITOR")
    print("=" * 60)

    rows = load_councils()
    selected = selected_councils(rows)

    print(f"\nCouncils in CSV: {len(rows)}")
    print(f"Councils selected for this run: {len(selected)}")

    if not selected:
        print(
            "\nNo councils have a website and are selected for search.\n"
            "Open councils.csv and set selected_for_search to yes."
        )
        return

    current_results = []

    for row in selected:
        try:
            current_results.append(scan_council(row))
        except Exception as exc:
            print(f"  ERROR: {exc}")
            current_results.append({
                "council": row["council"],
                "website": row.get("website", ""),
                "checked_at": now_iso(),
                "roles": {},
                "pages_checked": 0,
                "error": str(exc),
            })

    previous = load_json(
        OFFICERS_FILE,
        {"generated_at": None, "councils": []},
    )

    changes = compare_with_previous(previous, {
        "councils": current_results
    })

    # Save the latest current officer information.
    current_document = {
        "generated_at": now_iso(),
        "councils": current_results,
    }
    save_json(OFFICERS_FILE, current_document)

    # Save a complete copy of this search.
    search_document = {
        "generated_at": now_iso(),
        "councils_searched": len(selected),
        "results": current_results,
        "potential_changes": changes,
    }
    save_json(RESULTS_FILE, search_document)

    # Append potential changes to history.
    history = load_json(
        HISTORY_FILE,
        {"changes": []},
    )

    history.setdefault("changes", []).extend(changes)
    save_json(HISTORY_FILE, history)

    print("\n" + "=" * 60)
    print(f"SEARCH COMPLETE")
    print(f"Potential changes detected: {len(changes)}")
    print(f"Current officers saved to: {OFFICERS_FILE}")
    print(f"Search results saved to: {RESULTS_FILE}")
    print(f"Change history saved to: {HISTORY_FILE}")
    print("=" * 60)

    if changes:
        print("\nPOTENTIAL CHANGES:")
        for change in changes:
            print(
                f"- {change['council']} | "
                f"{change['role']} | "
                f"{change['previous_officer']} -> "
                f"{change['current_officer']}"
            )


if __name__ == "__main__":
    main()
