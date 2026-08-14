#!/usr/bin/env python3
"""
Local Authority Statutory Officer Monitor - v3

Main improvements:
- Uses council seed pages instead of relying on the homepage.
- Discovers likely pages through sitemap.xml when available.
- Reads PDF documents using pypdf.
- Uses stronger role/name matching.
- Records access errors without stopping the run.
- Correctly handles the first run when no baseline exists.
- Saves source URLs for every extracted officer.
"""

import csv, json, re, time, io
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

BASE=Path(__file__).resolve().parent
DATA=BASE/"data"; DATA.mkdir(exist_ok=True)
COUNCILS=BASE/"councils.csv"
OFFICERS=DATA/"officers.json"
HISTORY=DATA/"history.json"
RESULTS=DATA/"search_results.json"

HEADERS={
    "User-Agent":"Mozilla/5.0 (compatible; LocalAuthorityOfficerMonitor/3.0)",
    "Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language":"en-GB,en;q=0.9"
}
TIMEOUT=25
MAX_PAGES=35

ROLES={
"Chief Executive":[r"\bchief executive\b",r"\bhead of paid service\b"],
"Monitoring Officer":[r"\bmonitoring officer\b"],
"Section 151 Officer":[r"\bsection\s*151\s*officer\b",r"\bsection\s*151\b",r"\bs\.?\s*151\s*officer\b",r"\bchief finance officer\b",r"\bchief financial officer\b"]
}
NAME=r"[A-Z][A-Za-z'’\-]{1,30}(?:\s+[A-Z][A-Za-z'’\-]{1,30}){1,3}"
BAD={
"Chief Executive","Monitoring Officer","Section 151","Section 151 Officer",
"Chief Finance Officer","Chief Financial Officer","Head Paid Service",
"Executive Director","Senior Management","Senior Leadership","Management Team",
"Corporate Management","Borough Councillor","District Councillor","City Council",
"County Council","Local Authority","Worthing Councils","Council Offices",
"Current Officer","Potential New Officer"
}
PRIORITY=("chief","executive","monitoring","section","s151","finance","officer",
          "management","structure","constitution","governance","organisation",
          "organisational","committee","senior","leadership","pay-policy",
          "statement-of-accounts","annual-governance")

def now(): return datetime.now(timezone.utc).isoformat(timespec="seconds")

def save(path,obj):
    path.write_text(json.dumps(obj,indent=2,ensure_ascii=False),encoding="utf-8")

def load(path,default):
    if not path.exists(): return default
    try:return json.loads(path.read_text(encoding="utf-8"))
    except Exception:return default

def get(url):
    try:
        r=requests.get(url,headers=HEADERS,timeout=TIMEOUT,allow_redirects=True)
        r.raise_for_status()
        return r,None
    except requests.RequestException as e:
        return None,str(e)

def text_html(raw):
    s=BeautifulSoup(raw,"html.parser")
    for x in s(["script","style","noscript","svg"]): x.decompose()
    return "\n".join(re.sub(r"\s+"," ",x).strip() for x in s.get_text("\n").splitlines() if x.strip())

def text_pdf(content):
    try:
        reader=PdfReader(io.BytesIO(content))
        return "\n".join((p.extract_text() or "") for p in reader.pages[:30])
    except Exception:
        return ""

def is_pdf(url,response):
    return "pdf" in response.headers.get("content-type","").lower() or url.lower().endswith(".pdf")

def sitemap_urls(website):
    base=website.rstrip("/")+"/"
    candidates=[urljoin(base,"sitemap.xml"),urljoin(base,"robots.txt")]
    found=[]
    for u in candidates:
        r,e=get(u)
        if not r: continue
        if u.endswith("robots.txt"):
            for line in r.text.splitlines():
                if line.lower().startswith("sitemap:"):
                    found.append(line.split(":",1)[1].strip())
        else:
            try:
                soup=BeautifulSoup(r.text,"xml")
                for loc in soup.find_all("loc"):
                    found.append(loc.get_text(strip=True))
            except Exception: pass
    # If the first sitemap is a sitemap index, fetch it too.
    expanded=[]
    for u in found:
        if "sitemap" in u.lower() and u not in expanded:
            r,e=get(u)
            if r:
                try:
                    soup=BeautifulSoup(r.text,"xml")
                    locs=[x.get_text(strip=True) for x in soup.find_all("loc")]
                    expanded.extend(locs)
                except Exception: pass
    found.extend(expanded)
    return list(dict.fromkeys(found))

def priority(url):
    u=url.lower()
    return sum(10 for x in PRIORITY if x in u)

def links(url,raw):
    s=BeautifulSoup(raw,"html.parser"); host=urlparse(url).netloc.lower(); out=[]
    for a in s.find_all("a",href=True):
        u=urljoin(url,a["href"]); p=urlparse(u)
        if p.scheme in ("http","https") and p.netloc.lower()==host:
            if not any(x in u.lower() for x in ("login","logout","privacy","cookie")):
                out.append(u)
    return list(dict.fromkeys(out))

def crawl(row):
    website=row["website"].strip()
    seed=row.get("seed_url","").strip() or website
    queue=[]
    for u in (seed,website):
        if u and u not in queue: queue.append(u)

    # Sitemap discovery is particularly important because senior-management
    # pages are often several clicks away from the homepage.
    for u in sitemap_urls(website):
        if urlparse(u).netloc.lower()==urlparse(website).netloc.lower():
            queue.append(u)

    queue=sorted(dict.fromkeys(queue),key=priority,reverse=True)
    seen=set(); pages=[]; errors=[]

    while queue and len(seen)<MAX_PAGES:
        u=queue.pop(0)
        if u in seen: continue
        seen.add(u)
        r,e=get(u)
        if e:
            errors.append({"url":u,"error":e})
            continue
        if is_pdf(u,r):
            t=text_pdf(r.content); typ="pdf"
        else:
            t=text_html(r.text); typ="html"
        pages.append({"url":u,"text":t,"type":typ})
        if typ=="html":
            new=links(u,r.text)
            new.sort(key=priority,reverse=True)
            queue.extend(x for x in new[:15] if x not in seen)
        time.sleep(.12)
    return pages,errors

def clean_name(n):
    n=re.sub(r"\s+"," ",n).strip(" ,:;.-")
    if n in BAD:return None
    if any(n.lower()==x.lower() for x in BAD):return None
    forbidden={"council","councillor","officer","executive","director","service",
               "finance","monitoring","section","management","leadership",
               "authority","committee","department","team","town","hall",
               "corporate","governance"}
    if any(w in forbidden for w in n.lower().split()):return None
    return n

def extract_role(pages,role):
    role_re="|".join(ROLES[role]); findings=[]
    patterns=[
        rf"(?:{role_re})\s+(?:is|:|-|–|—)\s+({NAME})",
        rf"({NAME})\s+(?:is\s+)?(?:-|–|—|:|,)\s*(?:{role_re})",
        rf"({NAME}),\s*(?:who\s+is\s+)?(?:the\s+)?(?:{role_re})",
        rf"({NAME})\s*\((?:{role_re})\)",
    ]
    for p in pages:
        t=p["text"]
        if not t: continue
        lines=[re.sub(r"\s+"," ",x).strip() for x in t.splitlines() if x.strip()]
        for i,line in enumerate(lines):
            if not re.search(role_re,line,re.I): continue
            for pat in patterns:
                m=re.search(pat,line,re.I)
                if m:
                    n=clean_name(m.group(1))
                    if n:
                        findings.append((n,p["url"],"same-line"))
                        break
            # Adjacent short line: e.g. heading "Chief Executive" followed
            # by a person's name.
            if not findings or findings[-1][1]!=p["url"]:
                for near in lines[max(0,i-2):min(len(lines),i+3)]:
                    if near==line or len(near.split())>4: continue
                    m=re.fullmatch(NAME,near)
                    if m:
                        n=clean_name(m.group(1))
                        if n: findings.append((n,p["url"],"adjacent-line")); break
    if not findings:
        return {"name":None,"source":None,"confidence":"Not found"}
    counts={}
    for n,_,_ in findings: counts[n]=counts.get(n,0)+1
    best=max(counts,key=counts.get)
    item=next(x for x in findings if x[0]==best)
    return {"name":best,"source":item[1],"confidence":"Medium" if item[2]=="same-line" else "Low"}

def scan(row):
    print("\nScanning:",row["council"])
    pages,errors=crawl(row)
    result={"council":row["council"],"website":row["website"],"checked_at":now(),
            "pages_checked":len(pages),"errors":errors,"roles":{}}
    for role in ROLES:
        result["roles"][role]=extract_role(pages,role)
        x=result["roles"][role]
        print(" ",role,":",x["name"] or "Not found","[",x["confidence"],"]")
    return result

def main():
    print("="*60);print("LOCAL AUTHORITY STATUTORY OFFICER MONITOR v3");print("="*60)
    with COUNCILS.open("r",encoding="utf-8-sig",newline="") as f:
        rows=list(csv.DictReader(f))
    selected=[r for r in rows if r.get("website","").strip() and r.get("selected_for_search","").strip().lower() in {"yes","true","1"}]
    if not selected:
        selected=[r for r in rows if r.get("website","").strip()]
    print("\nCouncils in CSV:",len(rows));print("Councils selected:",len(selected))

    current=[scan(r) for r in selected]
    previous=load(OFFICERS,{"councils":[]})
    old={x.get("council"):x for x in previous.get("councils",[]) if isinstance(x,dict)}

    changes=[]
    for c in current:
        o=old.get(c["council"])
        if not o: continue
        for role in ROLES:
            a=o.get("roles",{}).get(role,{}).get("name")
            b=c.get("roles",{}).get(role,{}).get("name")
            conf=c.get("roles",{}).get(role,{}).get("confidence")
            if a and b and a.lower()!=b.lower() and conf=="Medium":
                changes.append({"detected_at":now(),"council":c["council"],"role":role,
                                 "previous_officer":a,"current_officer":b,
                                 "previous_source":o.get("roles",{}).get(role,{}).get("source"),
                                 "current_source":c.get("roles",{}).get(role,{}).get("source"),
                                 "confidence":"Potential change"})

    # First run creates a baseline and does NOT report changes.
    save(OFFICERS,{"generated_at":now(),"councils":current})
    save(RESULTS,{"generated_at":now(),"councils_searched":len(selected),
                  "results":current,"potential_changes":changes})
    hist=load(HISTORY,{"changes":[]});hist.setdefault("changes",[]).extend(changes);save(HISTORY,hist)

    print("\nSEARCH COMPLETE")
    print("Potential changes:",len(changes))
    print("Current officer data:",OFFICERS)
    print("Search results:",RESULTS)

if __name__=="__main__": main()
