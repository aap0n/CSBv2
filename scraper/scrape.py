#!/usr/bin/env python3
"""Scrape Carleton Central's public course search (bwysched) into per-term JSON.

Flow (all endpoints under https://central.carleton.ca/prod/):
  1. GET  bwysched.p_select_term?wsea_code=EXT      -> session_id + term list
  2. POST bwysched.p_search_fields                  -> search form (subject list)
  3. POST bwysched.p_course_search per subject      -> results table

The search form is serialized generically (hidden "dummy" fields + visible
defaults); the seven day checkboxes m/t/w/r/f/s/u must all be submitted or
the search silently returns "No courses meet the search criteria".

Output (matching the schema the app consumes):
  data/courses-<termCode>.json  {term, termCode, scraped, source, sections[]}
  data/terms.json               {updated, terms: [{code, name, sections}]}

Stdlib only. Usage: python3 scraper/scrape.py [--terms 202630,202710] [--out-dir data]
"""

import argparse
import datetime
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html import unescape
from http.cookiejar import CookieJar
from pathlib import Path

BASE = "https://central.carleton.ca/prod/"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
SOURCE = "Carleton Central public course search (bwysched)"
REQUEST_DELAY = 0.4
RETRIES = 3
MIN_KEEP_RATIO = 0.5  # refuse to overwrite if new count < 50% of old


def log(msg):
    print(msg, flush=True)


class Session:
    """One cookie-carrying browsing session against bwysched."""

    def __init__(self):
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar())
        )
        self.opener.addheaders = [("User-Agent", USER_AGENT)]

    def request(self, url, data=None):
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        last_err = None
        for attempt in range(RETRIES):
            try:
                with self.opener.open(urllib.request.Request(url, data=body), timeout=60) as r:
                    return r.read().decode("utf-8", "replace")
            except (urllib.error.URLError, OSError) as e:
                last_err = e
                wait = 2 ** attempt
                log(f"  request failed ({e}); retrying in {wait}s")
                time.sleep(wait)
        raise RuntimeError(f"request failed after {RETRIES} attempts: {url}: {last_err}")


def get_terms(session):
    """Term-select page -> (session_id, [(code, name)])."""
    page = session.request(BASE + "bwysched.p_select_term?wsea_code=EXT")
    sid = re.search(r'name="session_id" value="(\d+)"', page)
    if not sid:
        raise RuntimeError("no session_id on p_select_term page")
    terms = re.findall(r'<option value="(\d{6})"[^>]*>([^<]+)</option>', page)
    if not terms:
        raise RuntimeError("no term options on p_select_term page")
    return sid.group(1), [(code, name.strip()) for code, name in terms]


def get_search_form(session, term_code, session_id):
    """POST term selection -> (fresh session_id, subject codes, base form pairs).

    Serializes every input/select of the search form generically so hidden
    "dummy" fields and visible defaults survive future form changes.
    """
    page = session.request(
        BASE + "bwysched.p_search_fields",
        {"wsea_code": "EXT", "term_code": term_code, "session_id": session_id},
    )
    sid = re.search(r'name="session_id" value="(\d+)"', page)
    if not sid:
        raise RuntimeError("no session_id on p_search_fields page")

    form_match = re.search(r'<form action="bwysched.p_course_search".*?</form>', page, re.S)
    if not form_match:
        raise RuntimeError("no search form on p_search_fields page")
    form = form_match.group(0)

    pairs = []
    for tag in re.findall(r"<input[^>]*>", form, re.I):
        typ = (re.search(r'type="([^"]+)"', tag, re.I) or [None, "text"])[1].lower()
        name = re.search(r'name="([^"]+)"', tag)
        if not name or typ in ("submit", "reset", "checkbox"):
            continue  # checkboxes handled explicitly below
        value = re.search(r'value="([^"]*)"', tag)
        pairs.append((name.group(1), value.group(1) if value else ""))
    for sel in re.finditer(r'<select[^>]*name="([^"]+)"[^>]*>(.*?)</select>', form, re.I | re.S):
        name, body = sel.group(1), sel.group(2)
        chosen = re.search(r'<option value="([^"]*)"\s*selected', body, re.I)
        first = re.search(r'<option value="([^"]*)"', body, re.I)
        pairs.append((name, (chosen or first).group(1) if (chosen or first) else ""))
    # The gotcha: all seven day checkboxes must be checked or nothing matches.
    pairs += [("sel_day", d) for d in "mtwrfsu"]

    subj_select = re.search(r'<select[^>]*name="sel_subj"[^>]*>(.*?)</select>', form, re.I | re.S)
    subjects = re.findall(r'<option value="([A-Z]{3,4})"', subj_select.group(1)) if subj_select else []
    if not subjects:
        raise RuntimeError("no subjects in sel_subj select")

    pairs = [(k, sid.group(1)) if k == "session_id" else (k, v) for k, v in pairs]
    return sid.group(1), subjects, pairs


TEXT_TAG = re.compile(r"<[^>]+>")


def cell_text(html):
    return " ".join(unescape(TEXT_TAG.sub(" ", html)).replace("\xa0", " ").split())


def parse_results(page, subj):
    """Parse one subject's results table into section dicts."""
    sections = []
    # A "main" row links its CRN via p_display_course. (Full sections have no
    # select_action checkbox, so the checkbox is not a reliable marker.)
    rows = re.split(r"<tr[^>]*>", page)
    current = None
    for row in rows:
        if "bwysched.p_display_course" in row and re.search(r"crn=\d+", row):
            if current:
                sections.append(current)
            cells = [cell_text(c) for c in re.findall(r"<td[^>]*>(.*?)(?:</td>|$)", row, re.S)]
            # cells: [checkbox, status, crn, course, section, title, credit,
            #         type, has-more-info, ?, instructor]
            if len(cells) < 11:
                current = None
                continue
            current = {
                "status": cells[1],
                "crn": cells[2],
                "course": cells[3],
                "section": cells[4],
                "title": cells[5],
                "credit": cells[6],
                "type": cells[7],
                "instructor": cells[10],
                "meetings": [],
                "also": "",
                "info": "",
                "subj": subj,
            }
        elif current is not None and "<b>Meeting Date:</b>" in row:
            text = cell_text(row)
            m = re.search(r"Meeting Date:\s*(.*?)\s*Days:\s*(.*?)\s*Time:\s*(.*)$", text)
            if m:
                current["meetings"].append(
                    {"dates": m.group(1).strip(), "days": m.group(2).strip(), "time": m.group(3).strip()}
                )
        elif current is not None and "Also Register in:" in row:
            text = cell_text(row)
            current["also"] = re.sub(r"^.*Also Register in:\s*", "", text).strip()
        elif current is not None and "<b>Section Information:</b>" in row:
            text = cell_text(row)
            current["info"] = re.sub(r"^.*Section Information:\s*", "", text).strip()
    if current:
        sections.append(current)
    return sections


def scrape_term(term_code, term_name):
    log(f"== {term_name} ({term_code})")
    session = Session()
    sid, _ = get_terms(session)
    sid, subjects, base_pairs = get_search_form(session, term_code, sid)
    log(f"  {len(subjects)} subjects")
    sections = []
    for i, subj in enumerate(subjects, 1):
        pairs = [("sel_subj", subj) if (k, v) == ("sel_subj", "") else (k, v) for k, v in base_pairs]
        page = session.request(BASE + "bwysched.p_course_search", pairs)
        found = parse_results(page, subj)
        sections.extend(found)
        if found:
            log(f"  [{i}/{len(subjects)}] {subj}: {len(found)} sections")
        time.sleep(REQUEST_DELAY)
    log(f"  total: {len(sections)} sections")
    return {
        "term": term_name,
        "termCode": term_code,
        "scraped": datetime.date.today().isoformat(),
        "source": SOURCE,
        "sections": sections,
    }


def write_term(out_dir, data):
    path = out_dir / f"courses-{data['termCode']}.json"
    if path.exists():
        try:
            old = len(json.loads(path.read_text())["sections"])
        except (json.JSONDecodeError, KeyError):
            old = 0
        if old and len(data["sections"]) < old * MIN_KEEP_RATIO:
            log(f"  SANITY GUARD: new count {len(data['sections'])} < {MIN_KEEP_RATIO:.0%} of old {old}; keeping old file")
            return False
    path.write_text(json.dumps(data, separators=(",", ":"), ensure_ascii=False))
    log(f"  wrote {path} ({len(data['sections'])} sections)")
    return True


def write_index(out_dir, term_list):
    """terms.json reflects every term file present in out_dir, newest last."""
    names = {code: name for code, name in term_list}
    terms = []
    for path in sorted(out_dir.glob("courses-*.json")):
        data = json.loads(path.read_text())
        terms.append(
            {
                "code": data["termCode"],
                "name": names.get(data["termCode"], data["term"]),
                "sections": len(data["sections"]),
            }
        )
    index = {"updated": datetime.date.today().isoformat(), "terms": terms}
    (out_dir / "terms.json").write_text(json.dumps(index, indent=1))
    log(f"wrote {out_dir / 'terms.json'} ({len(terms)} terms)")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--terms", help="comma-separated term codes (default: all listed)")
    ap.add_argument("--out-dir", default="data", help="output directory (default: data)")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    _, term_list = get_terms(Session())
    log("available terms: " + ", ".join(f"{c} ({n})" for c, n in term_list))
    if args.terms:
        wanted = args.terms.split(",")
        term_list = [(c, n) for c, n in term_list if c in wanted]
        missing = set(wanted) - {c for c, _ in term_list}
        if missing:
            log(f"WARNING: requested terms not offered: {', '.join(sorted(missing))}")

    failed = False
    for code, name in term_list:
        try:
            data = scrape_term(code, name)
        except RuntimeError as e:
            log(f"  FAILED {code}: {e}")
            failed = True
            continue
        if not write_term(out_dir, data):
            failed = True
    write_index(out_dir, term_list)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
