"""Offline tests: GitHub runners never need network access to run these."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import import_osce_2022 as importer
from scripts.analyze_osce_2022 import summarize
import build_data as builder
from build_data import official_control_from_position, parse_time


def pdf_row(position, bearing, method, quantity, kind, when):
    fields = [position, bearing, method, quantity, kind, "Н/Д", "Н/Д", when]
    return [cell for field in fields for cell in [field, "", ""]]


class MockPdf:
    def __init__(self, rows):
        self.pages = [MockPage(rows)]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None


class MockPage:
    def __init__(self, rows):
        self.rows = rows

    def extract_tables(self):
        return [self.rows]


class ImportTests(unittest.TestCase):
    def test_incomplete_parenthesis_does_not_merge_distinct_observers(self):
        report = {"reportNumber": 10, "reportUrl": "https://www.osce.org/report/10",
                  "annexUrl": "https://www.osce.org/example.pdf"}
        first = "примерно 7,5 км к ЗСЗ от н. п. Горловка (неподконтрольный правительству"
        second = "камера СММ в н. п. Золотое (подконтрольный правительству, 60 км к З от Луганска)"
        rows = [pdf_row(first, "2–3 км к З", "слышали", "1", "очередь", "16.01, 11:00"),
                pdf_row(second, "2–3 км к Ю", "зафиксировали", "4", "взрыв", "15.01, 19:33"),
                pdf_row("", "2–3 км к Ю", "зафиксировали", "2", "взрыв", "15.01, 23:55")]
        with patch.object(importer.pdfplumber, "open", return_value=MockPdf(rows)):
            parsed, info = importer.parse_annex(report, b"%PDF-mock")
        self.assertEqual(info["rejected"], [])
        self.assertEqual([r["v"][0] for r in parsed], [first, second, second])

    def test_only_short_pdf_tail_continues_observer_position(self):
        self.assertTrue(importer.observer_fragment_is_continuation("Луганска)"))
        self.assertTrue(importer.observer_fragment_is_continuation("60 км к З от Луганска)"))
        self.assertTrue(importer.observer_fragment_is_continuation(
            "правительству, 58 км к западу от Луганска)"))
        self.assertTrue(importer.observer_fragment_is_continuation(
            "(неподконтрольная правительству, 9 км к СЗ от центра Донецка)"))
        self.assertFalse(importer.observer_fragment_is_continuation(
            "камера СММ в н. п. Золотое (подконтрольный правительству)"))

    def test_parenthetical_tail_at_page_break_attaches_to_previous_observer(self):
        report = {"reportNumber": 7, "reportUrl": "https://www.osce.org/report/7",
                  "annexUrl": "https://www.osce.org/test.pdf"}
        begin = "З окраина н. п. Первомайск (неподконтрольный"
        tail = "правительству, 58 км к западу от Луганска)"
        mock = MockPdf([pdf_row(begin, "2–3 км к С", "слышали", "3", "взрыв", "12.01, 12:20")])
        mock.pages.append(MockPage([
            pdf_row(tail, "3–4 км к С", "слышали", "2", "взрыв", "12.01, 12:22"),
            pdf_row("", "3–4 км к С", "слышали", "1", "взрыв", "12.01, 12:23")]))
        with patch.object(importer.pdfplumber, "open", return_value=mock):
            parsed, info = importer.parse_annex(report, b"%PDF-mock")
        self.assertEqual((len(parsed), info["rejected"]), (3, []))
        self.assertEqual([row["v"][0] for row in parsed], [begin, begin + " " + tail, begin + " " + tail])

    def test_10_and_12_cell_pdf_table_layouts(self):
        report = {"reportNumber": 8, "reportUrl": "https://www.osce.org/report/8",
                  "annexUrl": "https://www.osce.org/test.pdf"}
        header10 = ["Месторасположение СММ", "Место события", "Способ", "К-во",
                    "Наблюдение", "Описание", "Вооружение", "", "Дата,", "время"]
        row10 = ["камера (подконтрольная правительству)", "1–2 км к С", "слышали",
                 "4", "взрыв", "Н/Д", "Н/Д", "13.01, 17:22", "", ""]
        header12 = ["Месторасположение СММ", "Место события", "Способ", "", "К-", "во",
                    "Наблюдение", "Описание", "Вооружение", "", "Дата,", "время"]
        row12 = ["камера (подконтрольная правительству)", "3–5 км к С", "зафиксировала", "2",
                 "", "", "взрыв", "Н/Д", "Н/Д", "13.01, 17:23", "", ""]
        mock = MockPdf([header10, row10])
        mock.pages.append(MockPage([header12, row12]))
        with patch.object(importer.pdfplumber, "open", return_value=mock):
            rows, info = importer.parse_annex(report, b"%PDF-mock")
        self.assertEqual((len(rows), info["selectedQuantity"], info["rejected"]), (2, 6, []))
        self.assertEqual(rows[0]["v"][3], "4")
        self.assertEqual(rows[1]["v"][3], "2")

    def test_time_continued_on_next_pdf_page(self):
        report = {"reportNumber": 19, "reportUrl": "https://www.osce.org/report/19",
                  "annexUrl": "https://www.osce.org/test.pdf"}
        head = pdf_row("Месторасположение СММ", "Место события", "Способ", "К-во", "Наблюдение", "Дата, время")
        start = pdf_row("Чермалык (подконтрольный правительству)", "8–10 км к Ю",
                        "слышали", "15", "взрыв", "26.01,")
        continuation = [""] * 24
        continuation[21] = "11:41-11:44"
        mock = MockPdf([head, start])
        mock.pages.append(MockPage([head, continuation]))
        with patch.object(importer.pdfplumber, "open", return_value=mock):
            rows, info = importer.parse_annex(report, b"%PDF-mock")
        self.assertEqual((len(rows), info["selectedQuantity"], info["rejected"]), (1, 15, []))
        self.assertEqual(rows[0]["eventDate"], "2022-01-26T11:41")

    def test_reports_are_contiguous(self):
        html = "<html>" + "".join(
            f'<a class="teaser" href="/report/{i}"><h2>Daily Report {i}/2022 issued on 4 January 2022</h2></a>'
            for i in range(1, 41)) + "</html>"
        with patch.object(importer, "get", return_value=html.encode()):
            items = importer.discover_reports(0, 0)
        self.assertEqual([row["reportNumber"] for row in items], list(range(1, 41)))

    def test_missing_report_fails(self):
        html = b'<a class="teaser" href="/report/1"><h2>Daily Report 1/2022 issued on 4 January 2022</h2></a>'
        with patch.object(importer, "get", return_value=html):
            with self.assertRaisesRegex(ValueError, "missing reports"):
                importer.discover_reports(0, 0)

    def test_event_timestamp_not_issue_date_and_unquantified_rows_preserved(self):
        rows = [pdf_row("Позиция", "Место", "Метод", "К-во", "Тип", "Время"),
                pdf_row("камера СММ в Широкино (подконтрольный правительству)",
                        "2–4 км к С", "зафиксировала", "3", "взрыв", "31.12, 23:40"),
                pdf_row("", "2–4 км к С", "зафиксировала", "2", "взрыв", "01.01, 00:07"),
                pdf_row("", "2–4 км к С", "слышали", "Н/Д", "взрыв", "01.01, 00:08")]
        report = {"reportNumber": 1, "reportUrl": "https://www.osce.org/report/1",
                  "annexUrl": "https://www.osce.org/test.pdf"}
        with patch.object(importer.pdfplumber, "open", return_value=MockPdf(rows)):
            parsed, info = importer.parse_annex(report, b"%PDF-mock")
        self.assertEqual(len(parsed), 2)
        self.assertEqual(info["selectedQuantity"], 2)
        self.assertEqual(info["unquantifiedRows"], 1)
        self.assertEqual(info["rejected"], [])
        self.assertEqual(parsed[0]["eventDate"], "2022-01-01T00:07")
        self.assertIn("подконтрольный", parsed[1]["v"][0])

    def test_malformed_event_is_not_silently_ignored(self):
        rows = [pdf_row("камера", "2–4 км к С", "слышали", "4", "взрыв", "yesterday")]
        report = {"reportNumber": 1, "reportUrl": "https://www.osce.org/report/1",
                  "annexUrl": "https://www.osce.org/test.pdf"}
        with patch.object(importer.pdfplumber, "open", return_value=MockPdf(rows)):
            parsed, info = importer.parse_annex(report, b"%PDF-mock")
        self.assertFalse(parsed)
        self.assertEqual(len(info["rejected"]), 1)


class BuilderTests(unittest.TestCase):
    def test_patrol_coordinates_apply_offset_from_reference_settlement(self):
        position = "примерно 3 км к С от н. п. Первомайск (неподконтрольный правительству, 58 км к З от Луганска)"
        location = builder.location_of(position)
        expected = builder.destination([38.566, 48.629], 0, 3)
        self.assertIsNotNone(location)
        self.assertEqual(location["type"], "patrol")
        self.assertAlmostEqual(location["point"][0], expected[0], places=5)
        self.assertAlmostEqual(location["point"][1], expected[1], places=5)
        self.assertGreater(location["point"][1], 48.65)
        self.assertIn("3 км", location["basis"])
        self.assertAlmostEqual(location["accuracy_km"], .15, places=3)

    def test_talakivka_patrol_is_mapped_with_ten_percent_diameter(self):
        position = ("примерно 1,5 км к СВ от н. п. Талаковка "
                    "(подконтрольный правительству, 90 км к Ю от Донецка)")
        location = builder.location_of(position)
        self.assertIsNotNone(location)
        self.assertEqual(location["type"], "patrol")
        expected = builder.destination([37.72167, 47.18583], 45, 1.5)
        self.assertAlmostEqual(location["point"][0], expected[0], places=5)
        self.assertAlmostEqual(location["point"][1], expected[1], places=5)
        self.assertAlmostEqual(location["accuracy_km"], .075, places=3)
        self.assertAlmostEqual(location["anchor_uncertainty_km"], .9, places=3)
        self.assertAlmostEqual(builder.location_of(position.replace("1,5 км", "1 км"))["accuracy_km"], .05)

    def test_camera_coordinates_apply_bearings_and_preserve_camera_identity(self):
        near_shyrokyne = builder.location_of(
            "камера СММ в 1 км к ЮЗ от н. п. Широкино (подконтрольный правительству, 100 км к Ю от Донецка)")
        east_bohdanivka = builder.location_of(
            "камера СММ в 2 км к В от н. п. Богдановка (подконтрольный правительству, 41 км к ЮЗ от Донецка)")
        self.assertEqual(near_shyrokyne["site_id"], "smm_camera_shyrokyne")
        self.assertLess(near_shyrokyne["point"][1], 47.09186)
        self.assertLess(near_shyrokyne["point"][0], 37.80810)
        self.assertEqual(east_bohdanivka["site_id"], "smm_camera_bohdanivka")
        self.assertGreater(east_bohdanivka["point"][0], 37.80)

    def test_village_and_station_offsets_are_not_conflated(self):
        station = builder.location_of(
            "примерно 700 м к ЗЮЗ от железнодорожного вокзала в н. п. Ясиноватая "
            "(неподконтрольный правительству, 16 км к СВ от Донецка)")
        p = builder.destination([37.84524, 48.12198], 247.5, .7)
        self.assertAlmostEqual(station["point"][0], p[0], places=5)
        self.assertAlmostEqual(station["point"][1], p[1], places=5)
        self.assertIn("вокзал", station["basis"])
        self.assertIsNone(builder.location_of("примерно 2 км к В от КПВВ в неизвестном н. п."))
        rodina = builder.location_of(
            "н. п. Золотое-4 (Родина; подконтрольный правительству, 62 км к З от Луганска)")
        self.assertIsNotNone(rodina)
        self.assertAlmostEqual(rodina["point"][0], 38.5373, places=4)
        self.assertIn("Родина", rodina["title"])

    def test_numbered_zolote_districts_have_separate_verified_anchors(self):
        descriptions = [
            "Золотое-2/Карбонит (подконтрольный правительству, 62 км к З от Луганска)",
            "Золотое-3/Стахановец (подконтрольный правительству, 61 км к З от Луганска)",
            "Золотое-4/Родина (подконтрольный правительству, 62 км к З от Луганска)",
            "Золотое-5/Михайловка (неподконтрольный правительству, 60 км к З от Луганска)",
        ]
        points = [builder.location_of(position) for position in descriptions]
        self.assertTrue(all(points))
        self.assertEqual(len({tuple(point["point"]) for point in points}), 4)
        self.assertGreater(builder.distance_km(points[0]["point"], points[3]["point"]), 5)
        location = builder.location_of("примерно 2 км ВЮВ от Золотое 5 "
                                      "(неподконтрольный правительству, 61 км к СЗ от Луганска)")
        self.assertIsNotNone(location)
        self.assertAlmostEqual(location["accuracy_km"], 0.1)

    def test_verified_namesake_disambiguation_and_checkpoint(self):
        northern = builder.location_of("Василевка (неподконтрольный правительству, 20 км к С от Донецка)")
        southern = builder.location_of("Василевка (неподконтрольный правительству, 47 км к Ю от Донецка)")
        self.assertIsNotNone(northern)
        self.assertIsNotNone(southern)
        self.assertGreater(builder.distance_km(northern["point"], southern["point"]), 50)
        district = builder.location_of("3 км к ССВ от Петровский "
                                       "(неподконтрольный правительству, 15 км к ЮЗ от Донецка)")
        self.assertIsNotNone(district)
        self.assertIn("Петровский район", district["title"])
        checkpoint = builder.location_of("2 км к В от КПВВ в н. п. Счастье "
                                         "(подконтрольный правительству, 20 км к С от Луганска)")
        self.assertIsNotNone(checkpoint)
        self.assertIn("КПВВ Счастье", checkpoint["title"])
        zolote_checkpoint = builder.location_of(
            "КПВВ на Ю окраине н. п. Золотое (подконтрольный правительству, 60 км к З от Луганска)")
        self.assertIsNotNone(zolote_checkpoint)
        self.assertAlmostEqual(zolote_checkpoint["point"][0], 38.51311668)
        # Unverified checkpoints must never be replaced with town centres.
        self.assertIsNone(builder.location_of("КПВВ в н. п. Оленовка "
                                              "(неподконтрольный правительству, 23 км к ЮЗ от Донецка)"))

    def test_identically_named_villages_checked_against_reported_capital_distance(self):
        # The airport-side Vesele is 9 km NW of Donetsk, whereas the February
        # annexes also refer to another Vesele 45 km SE of Donetsk.
        near = builder.location_of(
            "н. п. Веселое (неподконтрольный правительству, 9 км к СЗ от Донецка)")
        far = builder.location_of(
            "примерно в 1,5 км к ССЗ от н. п. Веселое "
            "(неподконтрольный правительству, 45 км к ЮВ от Донецка)")
        self.assertIsNotNone(near)
        self.assertIsNotNone(far)
        self.assertGreater(builder.distance_km(near["point"], far["point"]), 30)
        self.assertIsNone(builder.location_of("правительству, 84 км к В от Донецка)"))

    def test_two_different_hnutove_cameras_remain_distinct(self):
        northeast = builder.location_of(
            "камера СММ 1,5 км к СВ от н. п. Гнутово (подконтрольный правительству, 90 км к Ю от Донецка)")
        checkpoint = builder.location_of(
            "камера СММ на КПВВ в н. п. Гнутово (подконтрольный правительству, 90 км к Ю от Донецка)")
        self.assertEqual(northeast["type"], "camera")
        self.assertEqual(checkpoint["type"], "camera")
        self.assertNotEqual(northeast["site_id"], checkpoint["site_id"])
        self.assertGreater(northeast["point"][1], checkpoint["point"][1])

    def test_definite_control_follows_entire_visible_uncertainty_circle(self):
        ring = [[37.5, 47.5], [38.5, 47.5], [38.5, 48.5],
                [37.5, 48.5], [37.5, 47.5]]
        event = {"coordinates": [38.0, 48.0], "accuracyKm": .25,
                 "direction": {"near_km": 2, "far_km": 3,
                               "bearing_deg": 90, "half_angle_deg": 22.5}}
        centre, classification, evidence = builder.classify_event_area(event, [ring], ring)
        self.assertEqual(classification, "ngca_definite")
        self.assertTrue(evidence["entireUncertaintyCircleOneSide"])
        self.assertGreater(evidence["distanceToPolygonBoundaryKm"], evidence["uncertaintyCircleRadiusKm"])
        anchored = {**event, "referenceAnchorUncertaintyKm": .9}
        anchored_evidence = builder.classify_event_area(anchored, [ring], ring)[2]
        self.assertAlmostEqual(anchored_evidence["referenceAnchorUncertaintyIncludedKm"], .9)
        self.assertAlmostEqual(anchored_evidence["uncertaintyCircleRadiusKm"] - evidence["uncertaintyCircleRadiusKm"], .9, places=2)
        outside = {**event, "coordinates": [39.1, 48.0]}
        self.assertEqual(builder.classify_event_area(outside, [ring], ring)[1], "gca_definite")
        near_border = {**event, "coordinates": [38.48, 48.0]}
        self.assertFalse(builder.classify_event_area(near_border, [ring], ring)[2]["entireUncertaintyCircleOneSide"])
        self.assertNotIn("definite", builder.classify_event_area(near_border, [ring], ring)[1])

    def test_builder_prefers_validated_official_annexes(self):
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            (temp / "source").mkdir()
            quality = {"failedReports": [], "rejectedRows": 0,
                       "foundReports": list(range(1, 41))}
            payload = {"start": "2022-01-01", "endExclusive": "2022-02-21",
                       "source_url": "https://www.osce.org/ukraine-smm/reports", "quality": quality,
                       "rows": [{"r": 1000001, "v": ["Широкино (подконтрольный правительству)",
                                                         "2–4 км к С", "слышали", "2", "взрыв",
                                                         "неопределенного происхождения", "Н/Д", "01.01, 00:07"],
                                 "reportNumber": 1, "reportUrl": "https://www.osce.org/example",
                                 "annexUrl": "https://www.osce.org/example.pdf", "annexPage": 2, "annexRow": 7},
                                {"r": 1000002, "v": ["примерно 3 км к С от н. п. Первомайск "
                                                         "(неподконтрольный правительству, 58 км к З от Луганска)",
                                                         "2–3 км к С", "слышали", "3", "взрыв",
                                                         "неопределенного происхождения", "Н/Д", "01.01, 00:08"],
                                 "reportNumber": 1, "reportUrl": "https://www.osce.org/example",
                                 "annexUrl": "https://www.osce.org/example.pdf", "annexPage": 2, "annexRow": 8}]}
            (temp / "source" / "osce_2022_official_rows.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with patch.object(builder, "SOURCE", temp / "source"), patch.object(builder, "OUT", temp / "dist"):
                builder.build()
            result = json.loads((temp / "dist" / "events.json").read_text(encoding="utf-8"))
            self.assertEqual(result["sourceKind"], "official_osce_pdf")
            self.assertEqual(result["period"], ["2022-01-01T00:00", "2022-02-21T00:00"])
            self.assertEqual(result["events"][0]["annexPage"], 2)
            self.assertEqual(result["events"][0]["observerControl"], "П")
            self.assertEqual(result["events"][0]["observerControlScope"], "observer_position")
            self.assertEqual(result["events"][1]["observerControl"], "Н")
            self.assertEqual(result["events"][1]["observerControlScope"], "inferred_from_reference_settlement")
            self.assertIn("модельное допущение", result["events"][1]["observerControlSource"])
            self.assertAlmostEqual(result["events"][1]["accuracyKm"], .15)

    def test_unmapped_patrol_worklist_has_explicit_reason(self):
        self.assertIn("справочнике", builder.diagnose_unmapped_position(
            "н. п. Неизвестное (подконтрольный правительству)"))
        self.assertIn("район Золотого", builder.diagnose_unmapped_position(
            "н. п. Золотое-4 (Родина; подконтрольный правительству, 10 км к ЮЗ от Луганска)"))
        self.assertIsNone(builder.location_of(
            "примерно 1,5 км к ССЗ от н. п. Веселое "
            "(неподконтрольный правительству, 95 км к СЗ от Донецка)"))

    def test_observer_control_only_from_explicit_osce_description(self):
        self.assertEqual(official_control_from_position("Майорск (подконтрольный правительству)")[0], "П")
        self.assertEqual(official_control_from_position("Горловка (неподконтрольная правительству)")[0], "Н")
        self.assertEqual(official_control_from_position("вблизи Донецка")[0], "?")

    def test_time_range(self):
        start, end = parse_time("01.01, 16:14–16:15")
        self.assertEqual((start, end), ("2022-01-01T16:14", "2022-01-01T16:15"))

    def test_journal_and_popup_both_use_ascending_chronology(self):
        app = (builder.ROOT / "dist" / "app.js").read_text(encoding="utf-8")
        self.assertIn("const chronological = (a,b) => a.startMs - b.startMs || a.id - b.id", app)
        self.assertIn("filtered.sort(chronological)", app)
        self.assertIn("[...group.events].sort(chronological)", app)
        self.assertIn("[...site.events].sort(chronological)", app)

    def test_aggregates_balance(self):
        row = {"start": "2022-01-01T12:00", "quantity": 4, "incidentPoint": None,
               "incidentTerritory": "unknown", "incident": "взрыв", "method": "heard"}
        dataset = {"period": ["2022-01-01T00:00", "2022-01-03T00:00"], "sourceKind": "test",
                   "summary": {"rows": 2, "quantity": 7},
                   "events": [row, {**row, "start": "2022-01-02T12:00", "quantity": 3,
                                    "incidentPoint": [37.7, 48.2], "incidentTerritory": "gca_likely"}]}
        result = summarize(dataset)
        self.assertEqual(result["daily"]["2022-01-01"]["quantity"], 4)
        self.assertEqual(result["daily"]["2022-01-02"]["quantity"], 3)
        self.assertEqual(sum(x["quantity"] for x in result["daily"].values()), 7)


if __name__ == "__main__":
    unittest.main()
