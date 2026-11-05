"""Download and audit official OSCE SMM ceasefire annexes for Jan–Feb 2022.

Daily report issue dates differ from observation dates. Strict validation
rejects missing reports, malformed rows and unavailable annexes before build.

Usage: python3 scripts/import_osce_2022.py --cache-dir /tmp/osce-pdfs
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urljoin

import lxml.html
import pdfplumber


BASE = "https://www.osce.org"
ROOT = Path(__file__).resolve().parents[1]
MIN_EVENT = datetime(2022, 1, 1)
MAX_EVENT = datetime(2022, 2, 21)  # Exclusive; includes observations on 20 Feb.
EXPECTED_REPORTS = set(range(1, 41))  # Report 40/2022 was issued on 21 Feb.
MONTHS = {"January": 1, "February": 2, "March": 3}
REPORT_RE = re.compile(r"Daily Report\s+(\d+)/2022 issued on\s+(\d+)\s+(January|February|March)\s+2022", re.I)
OBS_FIELDS = [0, 3, 6, 9, 12, 15, 18, 21]
DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\s*[,;]?\s*(\d{1,2}):(\d{2})")


def table_columns(table: list[list]) -> list[int] | None:
    """Map several PDF grid geometries to the eight OSCE logical columns.

    This is keyed by header layout, not by a report number or guessed values.
    Some annexes split the eight semantic columns into 10, 12 or 24 cells.
    """
    if not table:
        return None
    header = next((row for row in table if any("Месторасположение" in clean(x) for x in row)), table[0])
    width = len(header)
    if width >= 22:
        return OBS_FIELDS
    if width == 8:
        return list(range(8))
    if width == 12 and "Способ" in clean(header[2]):
        return [0, 1, 2, 3, 6, 7, 8, 9]
    if width == 10:
        if "Способ" in clean(header[4]):
            return [0, 1, 4, 5, 6, 7, 8, 9]
        if "Способ" in clean(header[2]):
            return [0, 1, 2, 3, 4, 5, 6, 7]
    return None


def get(url: str) -> bytes:
    """Use the configured HTTPS transport, with no cookies or credentials."""
    command = ["curl", "-L", "-sS", "--fail", "--retry", "3", "--retry-all-errors", "--retry-delay", "2", "--max-time", "80", url]
    result = subprocess.run(command, capture_output=True, check=True)
    return result.stdout


def clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def observer_fragment_is_continuation(fragment: str) -> bool:
    """Recognize a PDF-wrapped *tail*, not a new observation location.

    PDF tables sometimes drop a closing parenthesis at the end of one row.
    Appending the next complete site description in that case silently merges
    two different observers. Only a syntactically recognizable parenthetical
    tail may complete the previous position (including a page-break tail such
    as 'правительству, 58 км к западу от Луганска)').
    """
    fragment = clean(fragment)
    return bool(re.fullmatch(
        r"(?:[А-Яа-яЁё-]+|\d+(?:[,.]\d+)?\s*км\s+к\s+[А-Яа-яЁё-]+\s+от\s+[А-Яа-яЁё-]+|"
        r"правительству,?\s*\d+(?:[,.]\d+)?\s*км\s+к\s+[А-Яа-яЁё-]+\s+от\s+[А-Яа-яЁё-]+)\)|"
        r"\((?:не)?подконтрольн[А-Яа-яЁё-]+\s+правительству(?:,\s*\d+(?:[,.]\d+)?\s*км\s+к\s+"
        r"[А-Яа-яЁё-]+\s+от\s+(?:центра\s+)?(?:Донецка|Луганска))?\)",
        fragment,
    ))


def discover_reports(first_page: int = 5, last_page: int = 20) -> list[dict]:
    def fetch_index(page: int) -> list[dict]:
        url = BASE + f"/ukraine-smm/reports?filters=&page={page}&rows=50&solrsort=ds_date+desc"
        doc = lxml.html.fromstring(get(url))
        found = []
        for a in doc.xpath("//a[contains(concat(' ',normalize-space(@class),' '),' teaser ')]"):
            heading = clean(" ".join(a.xpath(".//h2//text()")))
            match = REPORT_RE.search(heading)
            if match:
                number, day, month = match.groups()
                if int(number) not in EXPECTED_REPORTS:
                    continue
                issued = datetime(2022, MONTHS[month.title()], int(day))
                found.append({"reportNumber": int(number), "issued": issued.strftime("%Y-%m-%d"),
                              "reportUrl": urljoin(BASE, a.get("href")), "title": heading})
        return found

    reports = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(fetch_index, page) for page in range(first_page, last_page + 1)]
        for result in as_completed(futures):
            for report in result.result():
                n = report["reportNumber"]
                if n in reports and reports[n]["reportUrl"] != report["reportUrl"]:
                    raise ValueError(f"Conflicting source URLs for report {n}/2022")
                reports[n] = report
    output = sorted(reports.values(), key=lambda x: x["reportNumber"])
    missing = EXPECTED_REPORTS - reports.keys()
    if missing:
        raise ValueError(f"Incomplete official archive: missing reports {sorted(missing)}")
    print(f"Discovered {len(output)} OSCE reports, {output[0]['issued']}–{output[-1]['issued']}", flush=True)
    return output


def annex_url(report: dict) -> str:
    doc = lxml.html.fromstring(get(report["reportUrl"]))
    heading = doc.xpath("//h3[contains(normalize-space(.), 'Table of ceasefire violations')]")
    if not heading:
        raise ValueError("No ceasefire-violations table heading")
    for el in heading:
        hrefs = el.xpath("ancestor::div[contains(@class,'document__download')][1]//a[@title='Русский']/@href")
        if hrefs:
            return urljoin(BASE, hrefs[0])
    raise ValueError("No Russian-language annex PDF link")


def parse_annex(report: dict, pdf: bytes) -> tuple[list[dict], dict]:
    if not pdf.startswith(b"%PDF"):
        raise ValueError("The downloaded annex is not a PDF")
    events = []
    rejected = []
    observer = ""
    raw_rows = 0
    unquantified = 0
    pending = None  # Event with date split across a page break.

    def append_row(v, event_page, event_row):
        nonlocal raw_rows, unquantified
        time = DATE_RE.search(v[7])
        if not time:
            rejected.append({"page": event_page, "row": event_row,
                             "reason": "missing or malformed event time", "values": v})
            return
        day, month, hour, minute = map(int, time.groups())
        try:
            date = datetime(2022, month, day, hour, minute)
        except ValueError as exc:
            rejected.append({"page": event_page, "row": event_row, "reason": str(exc), "date": v[7]})
            return
        raw_rows += 1
        if not MIN_EVENT <= date < MAX_EVENT:
            return
        if not v[0] or not v[4]:
            rejected.append({"page": event_page, "row": event_row,
                             "reason": "missing observer or incident", "values": v})
            return
        if not v[3].replace(" ", "").isdigit():
            unquantified += 1
        events.append({"v": v, "reportNumber": report["reportNumber"],
                       "reportUrl": report["reportUrl"], "annexUrl": report["annexUrl"],
                       "annexPage": event_page, "annexRow": event_row,
                       "eventDate": date.strftime("%Y-%m-%dT%H:%M")})

    with pdfplumber.open(io.BytesIO(pdf)) as document:
        for page_index, page in enumerate(document.pages, 1):
            tables = page.extract_tables()
            for table in tables:
                if not table:
                    continue
                columns = table_columns(table)
                for row_index, row in enumerate(table, 1):
                    if columns is None or len(row) <= max(columns):
                        if DATE_RE.search(" ".join(clean(value) for value in row)):
                            rejected.append({"page": page_index, "row": row_index,
                                             "reason": "unexpected column count", "columns": len(row)})
                        continue
                    v = [clean(row[i]) for i in columns]
                    # Ignore repeated table headers, empty cells and footers.
                    if (any("Месторасположение СММ" in clean(x) for x in row) or
                        ("к-во" in v[3].casefold() and "время" in v[7].casefold())):
                        continue
                    if not any(v[1:]) or (not v[7] and not v[4]):
                        if v[0] and not v[0].startswith("Месторасположение"):
                            observer = (observer + " " + v[0] if observer and not observer.endswith(")") and
                                        observer_fragment_is_continuation(v[0]) else v[0])
                        continue
                    event_page, event_row = page_index, row_index
                    if pending is not None:
                        prev, prev_page, prev_row = pending
                        pending = None
                        if not any(v[:7]) and v[7]:
                            prev[7] += " " + v[7]
                            v = prev
                            event_page, event_row = prev_page, prev_row
                        else:
                            append_row(prev, prev_page, prev_row)
                    if v[0] and v[0] != observer:
                        observer = (observer + " " + v[0] if observer and not observer.endswith(")") and
                                    observer_fragment_is_continuation(v[0]) else v[0])
                    v[0] = observer
                    if not DATE_RE.search(v[7]) and re.fullmatch(r"\d{1,2}\.\d{1,2}\s*[,;]?", v[7]):
                        pending = (v, event_page, event_row)
                        continue
                    if DATE_RE.search(v[7]) or v[4] or v[3].replace(" ", "").isdigit():
                        append_row(v, event_page, event_row)
    if pending is not None:
        append_row(*pending)
    meta = {"rawExtractedRows": raw_rows, "selectedRows": len(events),
            "selectedQuantity": sum(int(row["v"][3].replace(" ", "")) for row in events
                                    if row["v"][3].replace(" ", "").isdigit()),
            "unquantifiedRows": unquantified,
            "rejected": rejected, "pages": len(document.pages)}
    return events, meta


def process_report(report: dict, cache_dir: Path):
    result = dict(report)
    try:
        result["annexUrl"] = report.get("annexUrl") or annex_url(report)
        cache_file = cache_dir / f"table-{report['reportNumber']:02d}-ru.pdf"
        if cache_file.exists() and cache_file.stat().st_size > 1000:
            pdf = cache_file.read_bytes()
        else:
            pdf = get(result["annexUrl"])
            cache_file.write_bytes(pdf)
        result["sha256"] = hashlib.sha256(pdf).hexdigest()
        rows, info = parse_annex(result, pdf)
        result.update(info)
        if not info["rawExtractedRows"]:
            raise ValueError("No dated data rows found in PDF")
        result["status"] = "parsed" if not info["rejected"] else "review_required"
        return rows, result
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
        return [], result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--report-index", type=Path, help="Previously saved official report manifest (offline replay)")
    parser.add_argument("--max-reports", type=int, default=0, help="Test only earliest N reports")
    args = parser.parse_args()
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    if args.report_index:
        reports = json.loads(args.report_index.read_text(encoding="utf-8"))["reports"]
        found = [r["reportNumber"] for r in reports]
        if sorted(found) != sorted(EXPECTED_REPORTS) or any(not r.get("annexUrl") for r in reports):
            raise ValueError("Offline report manifest must contain all 40 numbered official annex links")
    else:
        reports = discover_reports()
    if args.max_reports:
        reports = reports[:args.max_reports]
    all_rows = []
    indexed = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(process_report, report, args.cache_dir): report for report in reports}
        for future in as_completed(futures):
            rows, meta = future.result()
            all_rows.extend(rows)
            indexed.append(meta)
            print(f"Report {meta['reportNumber']:02d}: {meta['status']}, {meta.get('selectedRows','?')} rows, {meta.get('selectedQuantity','?')} violations", flush=True)
    indexed.sort(key=lambda x: x["reportNumber"])
    all_rows.sort(key=lambda x: (x["eventDate"], x["reportNumber"], x["annexPage"], x["annexRow"]))
    for i, row in enumerate(all_rows, 1):
        row["r"] = 1000000 + i
    target = ROOT / "source" / "osce_2022_official_rows.json"
    index = ROOT / "source" / "osce_reports_2022_index.json"
    failed = [r for r in indexed if r["status"] != "parsed"]
    quality = {"expectedReports": sorted(EXPECTED_REPORTS),
               "foundReports": [r["reportNumber"] for r in indexed],
               "failedReports": [r["reportNumber"] for r in failed],
               "selectedRows": len(all_rows),
               "selectedQuantity": sum(r.get("selectedQuantity", 0) for r in indexed),
               "unquantifiedRows": sum(r.get("unquantifiedRows", 0) for r in indexed),
               "rejectedRows": sum(len(r.get("rejected", [])) for r in indexed),
               "period": ["2022-01-01T00:00", "2022-02-21T00:00"]}
    index.write_text(json.dumps({"reports": indexed, "quality": quality}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(quality, ensure_ascii=False), flush=True)
    if failed or set(quality["foundReports"]) != EXPECTED_REPORTS or args.max_reports:
        print("Quality gate failed; official dataset not published. Review the report index.", file=sys.stderr)
        return 1
    staging = target.with_suffix(".json.tmp")
    staging.write_text(json.dumps({"start": "2022-01-01", "endExclusive": "2022-02-21",
                                   "source_url": BASE + "/ukraine-smm/reports", "rows": all_rows,
                                   "quality": quality}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    staging.replace(target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
