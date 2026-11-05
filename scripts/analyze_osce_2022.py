"""Summarize the published OSCE atlas by date, method, event type and territory.

These are *reported counts* from OSCE tables, not independently deduplicated
physical explosions or proof of which side fired. An individual source row
with quantity 8 adds eight to the sum and one to the row count.
"""

from __future__ import annotations

from collections import Counter
import csv
from datetime import date, timedelta
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "dist" / "data"
TERRITORIES = ["gca_definite", "gca_likely", "unknown", "ngca_likely", "ngca_definite"]


def summarize(dataset: dict, report_index: dict | None = None) -> dict:
    period = dataset["period"]
    start = date.fromisoformat(period[0][:10])
    end = date.fromisoformat(period[1][:10])
    if not start < end:
        raise ValueError("Invalid data period")
    days = {}
    day = start
    while day < end:
        days[day.isoformat()] = {"rows": 0, "quantity": 0, "unquantifiedRows": 0,
                                 "mappedQuantity": 0, "territories": dict.fromkeys(TERRITORIES, 0),
                                 "types": Counter(), "methods": Counter()}
        day += timedelta(days=1)
    for event in dataset["events"]:
        key = event["start"][:10]
        if key not in days:
            raise ValueError(f"Observation outside configured period: {key}")
        quantity = event["quantity"] or 0
        entry = days[key]
        entry["rows"] += 1
        entry["quantity"] += quantity
        entry["unquantifiedRows"] += event["quantity"] is None
        entry["mappedQuantity"] += quantity if event["incidentPoint"] else 0
        territory = event["incidentTerritory"]
        if territory not in entry["territories"]:
            raise ValueError(f"Unknown territory: {territory}")
        entry["territories"][territory] += quantity
        entry["types"][event["incident"]] += quantity
        entry["methods"][event["method"]] += quantity

    total = sum(x["quantity"] for x in days.values())
    rows = sum(x["rows"] for x in days.values())
    if total != dataset["summary"]["quantity"] or rows != dataset["summary"]["rows"]:
        raise ValueError("Daily analysis totals do not equal source-event totals")
    result = {"period": period, "sourceKind": dataset["sourceKind"],
              "summary": dataset["summary"], "territoryOrder": TERRITORIES,
              "interpretation": "OSCE annex quantity sums; not independently deduplicated events or fire attribution",
              "daily": days}
    if report_index is not None:
        quality = report_index["quality"]
        if quality["failedReports"] or quality["rejectedRows"] or quality["foundReports"] != list(range(1, 41)):
            raise ValueError("OSCE report provenance contains unvalidated sources")
        if quality["selectedRows"] != rows or quality["selectedQuantity"] != total:
            raise ValueError("Totals differ between official PDF extraction and atlas build")
        result["provenance"] = {"archive": "https://www.osce.org/ukraine-smm/reports",
                                "reports": [{"number": item["reportNumber"], "issued": item["issued"],
                                             "reportUrl": item["reportUrl"], "annexUrl": item["annexUrl"],
                                             "sha256": item["sha256"], "rows": item["selectedRows"],
                                             "quantity": item["selectedQuantity"]}
                                            for item in report_index["reports"]],
                                "quality": quality}
    return result


def write_outputs(result: dict):
    DATA.mkdir(parents=True, exist_ok=True)
    with (DATA / "daily.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["date", "rows", "quantity", "unquantified_rows", "mapped_quantity", *TERRITORIES])
        for date_key, entry in result["daily"].items():
            writer.writerow([date_key, entry["rows"], entry["quantity"], entry["unquantifiedRows"],
                             entry["mappedQuantity"], *(entry["territories"][key] for key in TERRITORIES)])
    (DATA / "analysis.json").write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    if "provenance" in result:
        (DATA / "provenance.json").write_text(json.dumps(result["provenance"], ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"days": len(result["daily"]), "rows": result["summary"]["rows"],
                      "quantity": result["summary"]["quantity"],
                      "reports": len(result.get("provenance", {}).get("reports", []))}, ensure_ascii=False))


if __name__ == "__main__":
    events = json.loads((DATA / "events.json").read_text(encoding="utf-8"))
    report_path = ROOT / "source" / "osce_reports_2022_index.json"
    index = json.loads(report_path.read_text(encoding="utf-8")) if events["sourceKind"] == "official_osce_pdf" else None
    write_outputs(summarize(events, index))
