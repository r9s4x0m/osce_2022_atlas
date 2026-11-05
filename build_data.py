"""Build a historical atlas from validated official OSCE annexes or the legacy sheet.

The primary source is the full Jan–Feb 2022 official OSCE import, when present.
The original February researcher spreadsheet remains a reproducible fallback.
The model distinguishes observer location, reported bearing and observed event.
Coordinates of cameras and settlements are APPROXIMATE; no fire origins are
geocoded as exact points.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
import csv
import hashlib
import json
import math
import re


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "source"
OUT = ROOT / "dist" / "data"


def normalize(value):
    text = re.sub(r"\s+", " ", str(value or "")).strip().lower().replace("ё", "е")
    # Annex OCR occasionally inserts a Latin 'a' in Cyrillic village names,
    # e.g. Жaбуньки. Do not rewrite arbitrary Latin source text.
    return re.sub(r"(?<=[а-я])a(?=[а-я])", "а", text)


# Gazetteer of approximate settlement centres, not reconstructed patrol GPS.
# Coordinates follow [longitude, latitude]. Only vetted names are included;
# never infer an unmapped village from its distance to Donetsk/Luhansk.
TOWNS = [
    ("Станица Луганская", (39.49566, 48.64296), ["станица луганская", "станицы луганской"]),
    ("Старомихайловка", (37.687, 47.999), ["старомихайловка", "старомихайловки"]),
    ("Ясиноватая", (37.858, 48.130), ["ясиноватая", "ясиноватой"]),
    ("Горловка", (38.055, 48.305), ["горловка", "горловки"]),
    ("Кадиевка", (38.645, 48.568), ["кадиевка", "кадиевки", "стаханов"]),
    ("Первомайск", (38.566, 48.629), ["первомайск", "первомайска"]),
    ("Попасная", (38.37770, 48.63227), ["попасная", "попасной"]),
    ("Золотое", (38.51216, 48.69011), ["золотое", "золотого"]),
    ("Счастье", (39.231, 48.736), ["счастье", "счастья"]),
    ("Светлодарск", (38.22349, 48.43803), ["светлодарск", "светлодарска"]),
    ("Авдеевка", (37.74667, 48.13388), ["авдеевка", "авдеевки"]),
    ("Богдановка", (37.78380, 47.62959), ["богдановка", "богдановки"]),
    ("Марьинка", (37.50223, 47.94012), ["марьинка", "марьинки"]),
    ("Гранитное", (37.86698, 47.45939), ["гранитное", "гранитного"]),
    ("Широкино", (37.80810, 47.09186), ["широкино"]),
    ("Березовое", (37.58157, 47.76770), ["березовое", "березового"]),
    ("Петровское", (37.830, 47.640), ["петровское", "петровского"]),
    ("Луганск", (39.30730, 48.566), ["луганск", "луганска"]),
    ("Донецк", (37.80290, 48.01590), ["донецк", "донецка"]),
]

REFERENCE_POINTS = json.loads((SOURCE / "observer_reference_points_2022.json").read_text(encoding="utf-8"))
TOWNS.extend((item["name"], tuple(item["coordinates"]), item["aliases"])
             for item in REFERENCE_POINTS["additionalSettlements"])
TOWN_REFERENCES = {item["name"]: item["sourceUrl"] for item in REFERENCE_POINTS["additionalSettlements"]}
REGISTRY = json.loads((SOURCE / "osce_2022_referenced_settlements.json").read_text(encoding="utf-8"))


def settlement_forms(name):
    """Generate a few predictable Russian case forms for an exact village name.

    These forms are for lexical lookup only. Geography is selected separately
    using the reference distance and bearing actually stated by the OSCE.
    """
    primary = normalize(name.split(" (")[0])
    forms = {primary}
    endings = (("ское", "ского"), ("цкое", "цкого"), ("ое", "ого"),
               ("ая", "ой"), ("яя", "ей"), ("ка", "ки"),
               ("ово", "ова"), ("ево", "ева"), ("о", "а"),
               ("ь", "я"), ("й", "я"))
    for before, after in endings:
        if primary.endswith(before):
            forms.add(primary[:-len(before)] + after)
            break
    return forms


for name, coordinates, katotth, osm_id in REGISTRY["settlements"]:
    aliases = settlement_forms(name) | set(REGISTRY["aliases"].get(name, []))
    TOWNS.append((name, tuple(coordinates), sorted(aliases, key=len, reverse=True)))
    TOWN_REFERENCES[name] = f"https://www.openstreetmap.org/node/{osm_id}"
LANDMARKS = {item["name"]: item for item in REFERENCE_POINTS["landmarks"]}

CAMERA_SOURCE = json.loads((SOURCE / "previous_approximate_locations.geojson").read_text(encoding="utf-8"))
CAMERAS = [f for f in CAMERA_SOURCE["features"] if f["geometry"]["type"] == "Point"]
# OCHA's 2021 EECP coordinates improve on the approximate settlement centre,
# but they are still not the survey coordinates of an SMM camera.
CHECKPOINT_REFINEMENTS = {"smm_camera_maiorsk": [37.97414731, 48.39772106]}
# Explicit OSCE evidence overrides known spreadsheet control-label errors.
# The source "control" field remains untouched for auditability.
SITE_CONTROL_OVERRIDES = {
    "smm_camera_prince_ihor": {
        "control": "Н",
        "reason": "Камера у памятника князю Игорю расположена на неподконтрольной стороне; в таблице указано П",
        "url": "https://www.osce.org/special-monitoring-mission-to-ukraine/379156",
    },
    "smm_camera_oktiabr": {
        "control": "Н",
        "reason": "ОБСЕ явно указывает неподконтрольную территорию; одна строка таблицы ошибочно имеет П",
        "url": "https://www.osce.org/special-monitoring-mission-to-ukraine/491635",
    },
}


def official_control_from_position(position):
    """Infer observer control only from OSCE position text, never sheet column H/I.

    The annex routinely says 'government-controlled' in the observer's
    settlement description. If it omits that status, remain agnostic rather
    than transferring a hand-edited spreadsheet claim to the map.
    """
    text = normalize(position)
    non_government = "неподконтрольн" in text
    government = bool(re.search(r"(?<!не)подконтрольн", text))
    if non_government and not government:
        return "Н", "Текст позиции наблюдения в официальной таблице ОБСЕ"
    if government and not non_government:
        return "П", "Текст позиции наблюдения в официальной таблице ОБСЕ"
    return "?", "Статус в официальном описании позиции не установлен"


def camera_point(feature):
    return CHECKPOINT_REFINEMENTS.get(feature["id"], feature["geometry"]["coordinates"])


CAMERA_BY_ORIGINAL = {
    normalize(position): f
    for f in CAMERAS
    for position in f["properties"].get("matching_source_descriptions", [])
}

TIME_RE = re.compile(
    r"(?P<day>\d{1,2})\.(?P<month>\d{1,2})\s*[,;]?\s*"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})"
    r"(?:\s*[-–—]\s*(?P<endhour>\d{1,2}):(?P<endminute>\d{2}))?"
)

COMPASS = {"С": 0, "ССВ": 22.5, "СВ": 45, "ВСВ": 67.5, "В": 90,
           "ВЮВ": 112.5, "ЮВ": 135, "ЮЮВ": 157.5, "Ю": 180,
           "ЮЮЗ": 202.5, "ЮЗ": 225, "ЗЮЗ": 247.5, "З": 270,
           "ЗСЗ": 292.5, "СЗ": 315, "ССЗ": 337.5}
BEARING_RE = re.compile(r"(?:\bк\s+|\bна\s+)(ССВ|ВСВ|ВЮВ|ЮЮВ|ЮЮЗ|ЗЮЗ|ЗСЗ|ССЗ|СВ|ЮВ|ЮЗ|СЗ|С|В|Ю|З)\b")
DIST_RE = re.compile(r"(\d+(?:[,.]\d+)?)\s*[-–—]\s*(\d+(?:[,.]\d+)?)\s*км")
DIST_METERS_RE = re.compile(r"(\d+(?:[,.]\d+)?)\s*[-–—]\s*(\d+(?:[,.]\d+)?)\s*м(?!\w)")
SINGLE_KM_RE = re.compile(r"(\d+(?:[,.]\d+)?)\s*км")
SINGLE_METERS_RE = re.compile(r"(\d+(?:[,.]\d+)?)\s*м(?!\w)")
OBSERVER_OFFSET_RE = re.compile(
    r"(?<!\w)(?P<distance>\d+(?:[,.]\d+)?)\s*(?P<unit>км|м)\s+"
    r"(?:(?:к|на)\s+)?(?P<bearing>ССВ|ВСВ|ВЮВ|ЮЮВ|ЮЮЗ|ЗЮЗ|ЗСЗ|ССЗ|СВ|ЮВ|ЮЗ|СЗ|С|В|Ю|З|"
    r"северу|югу|востоку|западу|северо-востоку|северо-западу|"
    r"юго-востоку|юго-западу)\s+от\s+(?P<reference>.+)$", re.I
)
WORD_BEARINGS = {"северу": 0, "северо-востоку": 45, "востоку": 90,
                 "юго-востоку": 135, "югу": 180, "юго-западу": 225,
                 "западу": 270, "северо-западу": 315}
CAPITAL_REFERENCE_RE = re.compile(
    r"\b(?P<distance>\d+(?:[,.]\d+)?)\s*км\s+к\s+(?P<bearing>[а-яё-]+)\s+от\s+"
    r"(?P<capital>донецка|луганска)\b"
)
CAPITAL_POINTS = {"донецка": [37.80290, 48.01590], "луганска": [39.30730, 48.566]}
CAPITAL_BEARINGS = {**{k.lower(): v for k, v in COMPASS.items()},
                    "северу": 0, "северо-востоку": 45, "востоку": 90,
                    "юго-востоку": 135, "югу": 180, "юго-западу": 225,
                    "западу": 270, "северо-западу": 315,
                    "северо-северо-востоку": 22.5, "востоко-северо-востоку": 67.5,
                    "востоко-юго-востоку": 112.5, "юго-юго-востоку": 157.5,
                    "юго-юго-западу": 202.5, "западо-юго-западу": 247.5,
                    "западо-северо-западу": 292.5, "северо-северо-западу": 337.5}
EDGE_RE = re.compile(r"(?<!\w)(ССВ|ВСВ|ВЮВ|ЮЮВ|ЮЮЗ|ЗЮЗ|ЗСЗ|ССЗ|СВ|ЮВ|ЮЗ|СЗ|С|В|Ю|З)\s+(?:окраин[аые]|част[ьи])\b", re.I)
TOWN_EDGE_ESTIMATES_KM = {"Попасная": 1.4, "Первомайск": 1.6, "Кадиевка": 2.3,
                           "Станица Луганская": 0.9, "Горловка": 3.5,
                           "Авдеевка": 1.4, "Донецк": 5.0}


def parse_time(text):
    match = TIME_RE.search(text)
    if not match:
        raise ValueError(f"Unexpected time format {text!r}")
    g = match.groupdict()
    start = datetime(2022, int(g["month"]), int(g["day"]), int(g["hour"]), int(g["minute"]))
    end = start
    if g["endhour"] is not None:
        end = start.replace(hour=int(g["endhour"]), minute=int(g["endminute"]))
        if end < start:
            end += timedelta(days=1)
    return start.strftime("%Y-%m-%dT%H:%M"), end.strftime("%Y-%m-%dT%H:%M")


def read_direction(text):
    b = BEARING_RE.search(text)
    if not b:
        return None
    scale = 1.0
    interval = DIST_RE.search(text)
    if interval is None:
        interval = DIST_METERS_RE.search(text)
        if interval:
            scale = 0.001
    if interval:
        near, far = [float(x.replace(",", ".")) * scale for x in interval.groups()]
    else:
        single = SINGLE_KM_RE.search(text)
        if single is None:
            single = SINGLE_METERS_RE.search(text)
            if single:
                scale = 0.001
        if single is None:
            return None
        centre = float(single.group(1).replace(",", ".")) * scale
        tolerance = max(0.05, centre * 0.15)
        near, far = max(0, centre - tolerance), centre + tolerance
    if not 0 <= near <= far <= 100:
        return None
    return {"near_km": near, "far_km": far,
            "bearing_deg": COMPASS[b.group(1)],
            "half_angle_deg": 11.25 if len(b.group(1)) == 3 else 22.5}


# Territorial classification uses a dated 2021 cartographic polygon and the
# western contact trace of the SAME polygon. The OSCE 2022 overview line is
# kept only for comparison because its map projection is schematic.
# "Definite" means robust within the reconstruction, not OSCE proof.
def destination(lonlat, bearing_deg, distance_km):
    lon, lat = lonlat
    a = math.radians(lat)
    l = math.radians(lon)
    br = math.radians(bearing_deg)
    d = distance_km / 6371.0
    p = math.asin(math.sin(a) * math.cos(d) + math.cos(a) * math.sin(d) * math.cos(br))
    lng = l + math.atan2(math.sin(br) * math.sin(d) * math.cos(a),
                         math.cos(d) - math.sin(a) * math.sin(p))
    return [math.degrees(lng), math.degrees(p)]


def distance_km(a, b):
    lat1, lat2 = map(math.radians, (a[1], b[1]))
    dlat, dlon = lat2 - lat1, math.radians(b[0] - a[0])
    h = math.sin(dlat / 2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2)**2
    return 12742 * math.asin(min(1, math.sqrt(h)))


def bearing_deg(start, end):
    lat1, lat2 = map(math.radians, (start[1], end[1]))
    delta_lon = math.radians(end[0] - start[0])
    return math.degrees(math.atan2(math.sin(delta_lon) * math.cos(lat2),
                                  math.cos(lat1) * math.sin(lat2) -
                                  math.sin(lat1) * math.cos(lat2) * math.cos(delta_lon))) % 360


def matches_reported_reference_distance(position, reference_point):
    """Reject a namesake whose place-to-capital distance or bearing conflicts.

    The OSCE's '(45 km SE of Donetsk)' describes the *reference settlement*,
    not a camera/patrol displaced from it. Thus compare with the anchor, not
    with an offset observer. Allow generous differences for approximate data.
    """
    parenthetical = " ".join(re.findall(r"\(([^)]*)\)", normalize(position)))
    match = CAPITAL_REFERENCE_RE.search(parenthetical)
    if not match:
        return True
    distance = float(match["distance"].replace(",", "."))
    actual = distance_km(reference_point, CAPITAL_POINTS[match["capital"]])
    if abs(actual - distance) > max(12.0, 0.12 * distance):
        return False
    # Eight-direction OSCE descriptions are approximate (sometimes far coarser
    # than the 16-point bearings in incident sectors). Reject a clearly wrong
    # quadrant, but retain a generous allowance for those coarse descriptions.
    expected = CAPITAL_BEARINGS.get(match["bearing"])
    if expected is not None:
        measured = bearing_deg(CAPITAL_POINTS[match["capital"]], reference_point)
        if abs((measured - expected + 180) % 360 - 180) > 48:
            return False
    return True


def named_town(text, full_position=None):
    """Match an exact toponym; use OSCE geography to reject namesakes."""
    matches = {}
    longest_alias = 0
    for title, coordinates, aliases in TOWNS:
        found = max((len(alias) for alias in aliases
                     if re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", text)), default=0)
        if found:
            if found > longest_alias:
                matches.clear()
                longest_alias = found
            if found == longest_alias:
                matches[title] = (title, coordinates)
    if full_position is not None:
        matches = {key: town for key, town in matches.items()
                   if matches_reported_reference_distance(full_position, town[1])}
    return next(iter(matches.values())) if len(matches) == 1 else None


def site_key(kind, *parts):
    canonical = "|".join(str(part) for part in parts)
    return kind + "_" + hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:11]


def observer_offset(primary, full_position=None):
    """Locate an observer relative to the ACTUAL landmark in the first clause.

    In '3 km N of Pervomaisk (58 km W of Luhansk)' the 58 km refers to
    Pervomaisk, not to the patrol. Never use a secondary distance as an offset.
    Unresolved railway stations/checkpoints are not replaced with town centres.
    """
    match = OBSERVER_OFFSET_RE.search(primary)
    if not match:
        return None
    reference = normalize(match["reference"])
    distance = float(match["distance"].replace(",", "."))
    if match["unit"] == "м":
        distance /= 1000
    if not 0 < distance <= 30:
        return None
    key = match["bearing"].upper()
    bearing = COMPASS.get(key, WORD_BEARINGS.get(normalize(match["bearing"])))
    if bearing is None:
        return None
    anchor = None
    if any(part in reference for part in ["вокзал", "ж/д станци", "железнодорожн"]):
        if "ясиноват" in reference:
            anchor = LANDMARKS["Пассажирский ж/д вокзал Ясиноватой"]
        elif "донецк" in reference:
            anchor = LANDMARKS["Ж/д вокзал Донецка"]
    elif "мост" in reference and "станиц" in reference:
        # OSCE described the Prince Ihor monument about 1 km SSE of the bridge.
        # Reverse that direction to estimate a bridge reference, not its GPS.
        anchor = {"name": "мост у Станицы Луганской",
                  "coordinates": destination([39.4995, 48.62194], 337.5, 1.0),
                  "uncertaintyKm": 0.65,
                  "sourceUrl": "https://www.osce.org/special-monitoring-mission-to-ukraine/424916"}
    elif "кпвв" in reference and "счасть" in reference:
        anchor = LANDMARKS["КПВВ Счастье"]
    elif "кпвв" in reference and "золот" in reference:
        anchor = LANDMARKS["КПВВ Золотое"]
    elif not any(token in reference for token in ["кпвв", "блокпост", "вокзал", "станци", "мост"]):
        town = named_town(reference, full_position or primary)
        if town:
            title, coordinates = town
            anchor = {"name": title, "coordinates": coordinates,
                      "uncertaintyKm": (1.4 if title in {"Донецк", "Горловка", "Первомайск"} else 0.9),
                      "sourceUrl": TOWN_REFERENCES.get(title)}
    if anchor is None:
        return None
    # Research assumption requested for relative patrol positions: the radius
    # is 5% of the reported offset, i.e. a circle with diameter 10% of the
    # distance (100 m at 1 km). It is NOT a calibrated OSCE confidence interval.
    # Uncertainty in selecting the settlement's map anchor and rounding its
    # compass direction is separate and is NOT covered by this small circle.
    accuracy = 0.05 * distance
    return {"point": destination(anchor["coordinates"], bearing, distance),
            "accuracy_km": round(accuracy, 3),
            "anchor_uncertainty_km": anchor["uncertaintyKm"],
            "title": anchor["name"], "reference": anchor["coordinates"],
            "referenceUrl": anchor.get("sourceUrl"), "distanceKm": distance,
            "bearingDeg": bearing,
            "site_key": site_key("relative", anchor["name"], round(distance, 3), bearing),
            "basis": (f"По тексту ОБСЕ: {distance:g} км от ориентира «{anchor['name']}» "
                      f"по азимуту {bearing:g}°. Модельный диаметр допуска по дальности — "
                      f"10% ({100 * distance:g} м). Исходная точка — картографический "
                      "ориентир, а не GPS-позиция патруля; неопределённость ориентира "
                      "и округление направления в этот допуск не входят")}


def observer_town_edge(primary, town):
    edge = EDGE_RE.search(primary)
    if not edge:
        return None
    bearing = COMPASS[edge.group(1).upper()]
    distance = TOWN_EDGE_ESTIMATES_KM.get(town[0], 1.0)
    return {"point": destination(town[1], bearing, distance),
            "accuracy_km": round(max(1.5, distance * 0.9 + 0.8), 2),
            "site_key": site_key("edge", town[0], bearing),
            "basis": (f"Указана окраина/часть населённого пункта «{town[0]}»; "
                      f"показан условный сдвиг {distance:g} км от центра по азимуту "
                      f"{bearing:g}°. Расстояние до окраины ОБСЕ не указывала")}


def in_ring(point, ring):
    x, y = point
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def in_polygon(point, rings):
    return in_ring(point, rings[0]) and not any(in_ring(point, hole) for hole in rings[1:])


def line_distance_and_side(point, line):
    """Distance in km, side for a south-to-north line (east/right => NGCA)."""
    x, y = point
    cos_lat = math.cos(math.radians(y))
    best = (float("inf"), "unknown")
    for (x1, y1), (x2, y2) in zip(line, line[1:]):
        a, b = (x - x1) * cos_lat, y - y1
        dx, dy = (x2 - x1) * cos_lat, y2 - y1
        denom = dx * dx + dy * dy
        t = max(0.0, min(1.0, (a * dx + b * dy) / denom)) if denom > 0 else 0
        distance = math.hypot(a - t * dx, b - t * dy) * 111.2
        side = "ngca" if dx * b - dy * a < 0 else "gca"
        if distance < best[0]:
            best = (distance, side)
    return best


def event_uncertainty_radius_km(direction, observer_accuracy_km):
    """Conservative circular envelope used by BOTH classifier and map.

    It encloses the compass sector plus a modelled 15% extra range tolerance,
    then adds the observer site's positional uncertainty. Neither is a
    measured confidence interval supplied by the OSCE.
    """
    near, far = direction["near_km"], direction["far_km"]
    half = math.radians(direction["half_angle_deg"])
    center = (2 / 3) * (far**3 - near**3) / (far**2 - near**2) if far > near else near
    spread = max(
        math.sqrt(max(0, center**2 + distance**2 - 2 * center * distance * math.cos(half)))
        for distance in (0.85 * near, 1.15 * far)
    )
    return round(max(0.5, spread) + (observer_accuracy_km or 0), 3)


def polygon_boundary_distance_km(point, rings):
    return min(line_distance_and_side(point, ring)[0] for ring in rings)


def classify_event_area(event, polygon_rings, line):
    point = event["coordinates"]
    direction = event["direction"]
    if not point or not direction:
        return None, "unknown", None

    near, far = direction["near_km"], direction["far_km"]
    bearing, halfangle = direction["bearing_deg"], direction["half_angle_deg"]
    center_radius = (2 / 3) * (far**3 - near**3) / (far**2 - near**2) if far > near else near
    center = destination(point, bearing, center_radius)
    samples = []
    for i in range(7):
        radius = math.sqrt(near * near + (far * far - near * near) * ((i + 0.5) / 7))
        for j in range(13):
            azimuth = bearing - halfangle + (j + 0.5) * (2 * halfangle / 13)
            sample = destination(point, azimuth, radius)
            polygon_ngca = in_polygon(sample, polygon_rings)
            line_distance, line_side = line_distance_and_side(sample, line)
            samples.append((polygon_ngca, line_distance, line_side))

    total = len(samples)
    ngca_share = sum(v[0] for v in samples) / total
    min_line_distance = min(v[1] for v in samples)
    target = "ngca" if ngca_share > 0.5 else "gca"
    target_share = max(ngca_share, 1 - ngca_share)
    line_support = sum(v[2] == target for v in samples) / total
    # A narrow 10%-diameter halo describes only the reported patrol offset.
    # For EVENT territorial confidence, also cover uncertainty in locating
    # the settlement reference on the modern map.
    anchor_error = event.get("referenceAnchorUncertaintyKm") or 0
    circle_radius = event_uncertainty_radius_km(
        direction, (event["accuracyKm"] or 0) + anchor_error)
    edge_clearance = polygon_boundary_distance_km(center, polygon_rings)
    center_in_ngca = in_polygon(center, polygon_rings)
    # A circle is fully on one side iff its centre is on that side and its
    # radius is smaller than the distance to ANY polygon boundary (including
    # inner rings). Testing a handful of sample points is not sufficient.
    whole_circle_on_one_side = edge_clearance > circle_radius + 0.02
    if whole_circle_on_one_side:
        classification = "ngca_definite" if center_in_ngca else "gca_definite"
    elif target_share >= 0.72:
        classification = f"{target}_likely"
    else:
        classification = "unknown"

    evidence = {
        "method": "full uncertainty-circle containment for definite; uniform-area sector sample for likely; April-2021 polygon",
        "ngcaAreaShare": round(ngca_share, 3),
        "minDistanceFromContactLineKm": round(min_line_distance, 1),
        "lineSideSupport": round(line_support, 3),
        "uncertaintyCircleRadiusKm": circle_radius,
        "referenceAnchorUncertaintyIncludedKm": anchor_error,
        "distanceToPolygonBoundaryKm": round(edge_clearance, 3),
        "entireUncertaintyCircleOneSide": whole_circle_on_one_side,
        "historicalBorderUncertaintyKm": 3.5,
        "independentCorroboration": False,
        "sourceControlLabelIsObserverOnly": True,
    }
    return [round(v, 5) for v in center], classification, evidence


def classify_method(text):
    n = normalize(text)
    if "зафикс" in n:
        return "recorded"
    if "видел" in n or "видели" in n:
        return "both"
    if "слыш" in n:
        return "heard"
    return "other"


def location_of(position):
    n = normalize(position)
    primary = n.split("(")[0].strip()
    # Fragments of a parenthetical note should have been reattached by the
    # importer. Never misread '... от Донецка)' as a Donetsk observer.
    if primary.startswith(("правительству,", "неподконтрольный ", "подконтрольный ")):
        return None
    # The named Zolote-2/3/4/5 neighbourhoods have separate references in
    # observer_reference_points_2022.json. Never use general Zolote as a
    # fallback for a numbered district if its own anchor is missing.
    if re.search(r"золотое\s*[-–]?\s*[2345]\b", primary) and not named_town(primary, n):
        return None

    offset = observer_offset(primary, n)
    if offset and not matches_reported_reference_distance(n, offset["reference"]):
        return None
    camera = CAMERA_BY_ORIGINAL.get(n)
    if camera is None and primary.startswith("камера смм"):
        # Explicitly reconciled spelling variants in the official annexes.
        hints = [
            ("шахте", "октябрьск", "smm_camera_oktiabr"),
            ("чермалык", None, "smm_camera_chermalyk"),
            ("гнутово", "кпвв", "smm_camera_hnutove"),
            ("золотое", None, "smm_camera_zolote"),
        ]
        for first, second, camera_id in hints:
            if first in primary and (second is None or second in primary):
                if camera_id == "smm_camera_zolote" and "части участка разведения" in primary:
                    continue
                camera = next(f for f in CAMERAS if f["id"] == camera_id)
                break
    if camera:
        props = camera["properties"]
        if not matches_reported_reference_distance(n, offset["reference"] if offset else camera_point(camera)):
            return None
        return {
            "site_id": camera["id"], "title": props["name"].removeprefix("Камера СММ: "),
            "point": offset["point"] if offset else camera_point(camera), "type": "camera",
            "accuracy_km": max(props["positional_uncertainty_km"], offset["accuracy_km"]) if offset else props["positional_uncertainty_km"],
            "anchor_uncertainty_km": offset["anchor_uncertainty_km"] if offset else None,
            "basis": (offset["basis"] if offset else
                      "Координата КПВВ по OCHA 2021, не самой камеры"
                      if camera["id"] in CHECKPOINT_REFINEMENTS else
                      "Историческая камера: примерная позиция по населённому пункту/ориентиру"),
            "referenceUrl": offset["referenceUrl"] if offset else None,
        }

    if offset:
        distance = offset["distanceKm"]
        is_camera = primary.startswith("камера смм")
        return {"site_id": site_key("smm_camera" if is_camera else "patrol", offset["site_key"]),
                "title": f"≈ {distance:g} км / {offset['bearingDeg']:g}° от {offset['title']}",
                "point": offset["point"], "type": "camera" if is_camera else "patrol",
                "accuracy_km": offset["accuracy_km"],
                "anchor_uncertainty_km": offset["anchor_uncertainty_km"],
                "relative_offset_km": distance,
                "basis": offset["basis"],
                "referenceUrl": offset["referenceUrl"]}

    # Never silently fall back to a settlement centre after seeing a
    # relative location whose landmark could not be identified reliably.
    if OBSERVER_OFFSET_RE.search(primary) or primary.startswith("камера смм"):
        return None

    # Match only explicitly validated checkpoints with standalone coordinates.
    # Their map anchor does not come from the settlement's approximate centre.
    if re.search(r"\bкпвв\b", primary) and "золот" in primary:
        anchor = LANDMARKS["КПВВ Золотое"]
        return {"site_id": site_key("checkpoint", anchor["name"]),
                "title": anchor["name"], "point": anchor["coordinates"],
                "type": "patrol", "accuracy_km": anchor["uncertaintyKm"],
                "basis": ("Приблизительные координаты именно КПВВ по OCHA (2021), "
                          "не GPS-позиция конкретного патруля"),
                "referenceUrl": anchor["sourceUrl"]}
    if re.search(r"\bкпвв\b", primary) and ("оленовк" in primary or "еленовк" in primary):
        anchor = LANDMARKS["КПВВ Оленовка"]
        if not matches_reported_reference_distance(n, anchor["coordinates"]):
            return None
        return {"site_id": site_key("checkpoint", anchor["name"]),
                "title": anchor["name"], "point": anchor["coordinates"],
                "type": "patrol", "accuracy_km": anchor["uncertaintyKm"],
                "basis": ("Примерное положение исторического блокпоста на Н-20, по "
                          "геопривязке 2016 года. Официальное сообщение ОБСЕ "
                          "подтверждает ориентир: 650 м южнее места происшествия "
                          "на ул. Ленина. Не GPS конкретного патруля февраля 2022 года"),
                "referenceUrl": anchor["sourceUrl"]}
    if re.search(r"\bблокпост\b", primary) and "кременец" in primary:
        anchor = LANDMARKS["Блокпост Кременец (Н-15)"]
        if not matches_reported_reference_distance(n, anchor["coordinates"]):
            return None
        return {"site_id": site_key("checkpoint", anchor["name"]),
                "title": anchor["name"], "point": anchor["coordinates"],
                "type": "patrol", "accuracy_km": anchor["uncertaintyKm"],
                "basis": ("Гипотеза: пересечение трассы Н-15 с дорогой на север к "
                          "комбинату «Каргилл». ОБСЕ подтверждает блокпост на Н-15 "
                          "возле комбината, но не сам перекрёсток. Модельный радиус "
                          "неопределённости 800 м; не GPS патруля февраля 2022 года"),
                "referenceUrl": anchor["sourceUrl"]}
    # A checkpoint is not the settlement itself: its location must be verified
    # as a distinct landmark before mapping, even if the settlement is known.
    if re.search(r"\b(?:кпвв|блокпост)\b", primary):
        return None
    town = named_town(primary, n)
    if town:
        title, coordinates = town
        if not matches_reported_reference_distance(n, coordinates):
            return None
        edge = observer_town_edge(primary, town)
        if edge:
            return {"site_id": site_key("patrol", edge["site_key"]),
                    "title": f"Окраина / часть · {title}", "point": edge["point"],
                    "type": "patrol", "accuracy_km": edge["accuracy_km"],
                    "basis": edge["basis"], "referenceUrl": TOWN_REFERENCES.get(title)}
        return {"site_id": site_key("patrol", title), "title": title,
                "point": list(coordinates), "type": "patrol", "accuracy_km": 6.0,
                "basis": "Примерный центр населённого пункта, а не GPS патруля",
                "referenceUrl": TOWN_REFERENCES.get(title)}
    return None


def diagnose_unmapped_position(position):
    """Explain an omitted patrol marker without silently inventing coordinates.

    The categories are an audit/worklist, not evidence of position or control.
    """
    n = normalize(position)
    primary = n.split("(")[0].strip()
    if primary.startswith(("правительству,", "неподконтрольный ", "подконтрольный ")):
        return "Обрыв описания позиции в приложении ОБСЕ"
    if re.search(r"золотое\s*[-–]?\s*[2345]\b", primary) and not named_town(primary, n):
        return "Отдельный район Золотого: нет проверенного ориентира"
    offset_match = OBSERVER_OFFSET_RE.search(primary)
    if offset_match:
        ref = normalize(offset_match["reference"])
        special = any(word in ref for word in ("кпвв", "блокпост", "вокзал", "станци", "мост"))
        if special and not observer_offset(primary, n):
            return "Не определена точка специального ориентира (КПВВ / станция / мост)"
        anchor = observer_offset(primary, n)
        if anchor and not matches_reported_reference_distance(n, anchor["reference"]):
            return "Картографический одноимённый ориентир не совпадает с удалённостью в отчёте"
        if not anchor:
            unqualified = observer_offset(primary)
            if unqualified and not matches_reported_reference_distance(n, unqualified["reference"]):
                return "Картографический одноимённый ориентир не совпадает с удалённостью в отчёте"
            return "Не найден проверенный ориентир для указанного смещения"
        return "Не удалось привязать относительную позицию"
    town = named_town(primary, n)
    if town and not matches_reported_reference_distance(n, town[1]):
        return "Картографический одноимённый ориентир не совпадает с удалённостью в отчёте"
    if any(word in primary for word in ("кпвв", "блокпост", "вокзал", "станци", "мост")):
        return "Не определена точка специального ориентира (КПВВ / станция / мост)"
    if "н. п." in primary or "посел" in primary:
        return "Населённый пункт отсутствует в проверенном справочнике"
    return "Не разобрано описание позиции патруля"


# Manually traced from OSCE SMM map created on 20 February 2022, report
# 40/2022, p. 3, at a 1273x1800 rendition. Approximate vertices only.
LINE_PIXELS_2022 = [
    (521, 1215), (508, 1191), (510, 1162), (513, 1143), (520, 1121),
    (527, 1101), (529, 1081), (528, 1066), (520, 1048), (516, 1033),
    (512, 1021), (501, 1006), (493, 997), (474, 982), (465, 971),
    (464, 954), (460, 937), (452, 921), (445, 907), (440, 888),
    (444, 877), (457, 866), (478, 854), (495, 839), (512, 822),
    (526, 806), (530, 786), (531, 761), (542, 736), (537, 722),
    (548, 718), (567, 724), (586, 727), (610, 728), (629, 737),
    (644, 735), (655, 721), (671, 714), (678, 693), (678, 675),
    (682, 650), (688, 630), (701, 625), (718, 620), (736, 620),
    (755, 622), (779, 624), (781, 598), (795, 604), (805, 615),
    (816, 642), (835, 649), (852, 646), (865, 626), (876, 611),
    (894, 591), (906, 602), (916, 627), (920, 650), (945, 662),
    (967, 676), (988, 676), (1010, 665),
]
GCP = [
    (37.8029, 48.0159, 500, 894),  # Donetsk city marker
    (39.3073, 48.5660, 897, 673),  # Luhansk
    (37.5500, 47.0970, 454, 1197),  # Mariupol
    (38.4930, 48.9460, 686, 538),  # Sievierodonetsk
    (37.5320, 48.7230, 466, 614),  # Kramatorsk
]


def contact_line_2022():
    import numpy as np
    from scipy.optimize import least_squares

    a = np.array([[1, lon - 38, lat - 48] for lon, lat, x, y in GCP])
    b = np.array([[x, y] for lon, lat, x, y in GCP])
    transform = np.linalg.lstsq(a, b, rcond=None)[0]
    rms = float(np.sqrt(np.mean(np.sum((a @ transform - b) ** 2, axis=1))))
    coordinates = []
    for x, y in LINE_PIXELS_2022:
        result = least_squares(lambda lnglat: np.array([1, lnglat[0] - 38, lnglat[1] - 48]) @ transform - [x, y], (38, 48))
        coordinates.append([round(float(v), 5) for v in result.x])
    return coordinates, round(rms, 1)


def contact_line_from_area(polygon_ring):
    """Extract the historical contact edge, excluding international border.

    For this identified Apr-2021 polygon, ring indices 455..568 and 0..177
    trace Azov coast -> Avdiivka -> Stanytsia Luhanska. Indices 178..454
    instead follow Ukraine's exterior boundary and coast.
    """
    return polygon_ring[455:] + polygon_ring[:178]


def build():
    official_path = SOURCE / "osce_2022_official_rows.json"
    official = official_path.exists()
    source = json.loads((official_path if official else SOURCE / "osce_sheet_rows.json").read_text(encoding="utf-8"))
    if official:
        quality = source["quality"]
        if (quality["failedReports"] or quality["rejectedRows"] or
            quality["foundReports"] != list(range(1, 41)) or
            source["start"] != "2022-01-01" or source["endExclusive"] != "2022-02-21"):
            raise ValueError("Unvalidated or incomplete OSCE annex import")
    rows = source["rows"] if official else source["rows"][1:]
    events = []
    observer = ""
    sources = set()
    for row in rows:
        v = row["v"] + [None] * (12 - len(row["v"]))
        observer = v[0] or observer
        start, end = parse_time(v[7])
        raw_quantity = str(v[3] or "").strip()
        digits = raw_quantity.replace(" ", "")
        count = int(digits) if digits.isdigit() else None
        geo = location_of(observer)
        site_id = geo["site_id"] if geo else None
        if geo:
            sources.add(site_id)
        official_control, control_source = official_control_from_position(observer)
        if site_id in SITE_CONTROL_OVERRIDES:
            official_control = SITE_CONTROL_OVERRIDES[site_id]["control"]
            control_source = "Дополнительное подтверждение в опубликованном материале ОБСЕ"
        relative_observer = bool(OBSERVER_OFFSET_RE.search(normalize(observer).split("(")[0]))
        control_scope = "not_specified" if official_control == "?" else "observer_position"
        if relative_observer and official_control in {"П", "Н"} and site_id not in SITE_CONTROL_OVERRIDES:
            # Working assumption: an SMM patrol described relative to a named
            # controlled settlement was on the same side as that settlement.
            # This is an inference from the OSCE text, not a patrol GPS fix.
            control_scope = "inferred_from_reference_settlement"
            control_source = ("По указанной в отчёте ОБСЕ стороне населённого пункта-ориентира; "
                              "для патруля принята та же сторона (модельное допущение)")
        events.append({
            "id": row["r"], "sourceRow": None if official else row["r"],
            "reportNumber": row.get("reportNumber"),
            "reportUrl": row.get("reportUrl"), "annexUrl": row.get("annexUrl"),
            "annexPage": row.get("annexPage"), "annexRow": row.get("annexRow"),
            "sourceKind": "official_osce_pdf" if official else "researcher_sheet",
            "position": re.sub(r"\s+", " ", observer).strip(),
            "site": site_id, "siteName": geo["title"] if geo else None,
            "coordinates": geo["point"] if geo else None,
            "siteType": geo["type"] if geo else "unmapped",
            "accuracyKm": geo["accuracy_km"] if geo else None,
            "referenceAnchorUncertaintyKm": geo.get("anchor_uncertainty_km") if geo else None,
            "relativeOffsetKm": geo.get("relative_offset_km") if geo else None,
            "locationBasis": geo["basis"] if geo else "Место наблюдателя не геопривязано",
            "locationDiagnosis": diagnose_unmapped_position(observer) if not geo else None,
            "locationReferenceUrl": geo.get("referenceUrl") if geo else None,
            "relativeObserver": relative_observer,
            "control": None if official else v[8],
            "observerControl": official_control,
            "observerControlScope": control_scope,
            "observerControlSource": control_source,
            "observerControlCorrection": SITE_CONTROL_OVERRIDES.get(site_id),
            "where": re.sub(r"\s+", " ", v[1] or "").strip(),
            "direction": read_direction(v[1] or ""), "methodOriginal": v[2],
            "method": classify_method(v[2]), "quantity": count,
            "quantityOriginal": raw_quantity,
            "incident": v[4], "description": re.sub(r"\s+", " ", v[5] or "").strip(),
            "weapon": v[6], "rawDate": v[7], "start": start, "end": end,
        })

    _, by_method = Counter(), Counter(e["method"] for e in events)
    overview_coords, rms = contact_line_2022()
    polygon = next(f for f in CAMERA_SOURCE["features"] if f["geometry"]["type"] == "Polygon")
    polygon_rings = polygon["geometry"]["coordinates"]
    coords = contact_line_from_area(polygon_rings[0])
    actual_camera_sites = {e["site"]: e for e in events if e["siteType"] == "camera" and e["site"]}
    camera_sites = []
    for feature in CAMERAS:
        event = actual_camera_sites.pop(feature["id"], None)
        camera_sites.append({
            "site_id": feature["id"],
            "title": feature["properties"]["name"].removeprefix("Камера СММ: "),
            "coordinates": event["coordinates"] if event else camera_point(feature),
            "accuracyKm": event["accuracyKm"] if event else feature["properties"]["positional_uncertainty_km"],
            "locationBasis": event["locationBasis"] if event else
                             "Координата КПВВ по OCHA 2021, не самой камеры"
                             if feature["id"] in CHECKPOINT_REFINEMENTS else
                             "Ориентир по населённому пункту / объекту",
            "controlOverride": SITE_CONTROL_OVERRIDES.get(feature["id"]),
        })
    # Distinct cameras with explicit offsets, but absent from the old
    # approximate camera list, must not be confused with a town-centre camera.
    camera_sites.extend({
        "site_id": e["site"], "title": e["siteName"],
        "coordinates": e["coordinates"], "accuracyKm": e["accuracyKm"],
        "locationBasis": e["locationBasis"], "controlOverride": None,
    } for e in sorted(actual_camera_sites.values(), key=lambda x: x["site"]))
    geography = {
        "cameraSites": camera_sites,
        "contactLine": {
            "geometry": {"type": "LineString", "coordinates": coords},
            "date": "2021-04", "sourceUrl": polygon["properties"]["source_url"],
            "method": "Западная граница векторного контура неподконтрольной территории (апрель 2021), проверенная по статусам пунктов в отчётах ОБСЕ",
            "uncertaintyKm": 3.5,
            "temporalWarning": "Не является точной геометрией линии на каждый день февраля 2022 года",
        },
        "osceSchematic": {
            "geometry": {"type": "LineString", "coordinates": overview_coords},
            "date": "2022-02-20", "sourceUrl": "https://www.osce.org/files/2022-02-20-21%20Daily%20Report_ENG.pdf",
            "method": "Схематическая линия, вручную оцифрованная с обзорной карты ОБСЕ",
            "uncertaintyKm": 7, "controlPointRmsPixels": rms,
            "warning": "В районе Майорска проходит по неверную сторону КПВВ; оставлена только для сравнения, выключена по умолчанию",
        },
        "territory": {
            "geometry": polygon["geometry"], "date": "2021-04",
            "sourceUrl": polygon["properties"]["source_url"],
            "warning": "Контур площади взят из апреля 2021 года как ориентировочная подложка; не точный полигон на февраль 2022 года",
        },
    }
    for event in events:
        center, classification, evidence = classify_event_area(event, polygon_rings, coords)
        event["incidentPoint"] = center
        event["incidentTerritory"] = classification
        event["territoryEvidence"] = evidence
        event["uncertaintyRadiusKm"] = (evidence["uncertaintyCircleRadiusKm"]
                                         if evidence else None)
    reference_points = {
        "Майорский КПВВ": CHECKPOINT_REFINEMENTS["smm_camera_maiorsk"],
        "Авдеевка": [37.74667, 48.13388],
        "Горловка": [38.055, 48.305],
    }
    assert {name: in_polygon(pt, polygon_rings) for name, pt in reference_points.items()} == {
        "Майорский КПВВ": False, "Авдеевка": False, "Горловка": True,
    }, "Historical NGCA polygon conflicts with known settlement status"
    body = {
        "source": source["source_url"], "sourceSheet": None if official else "Лист1",
        "sourceKind": "official_osce_pdf" if official else "researcher_sheet", "sourceYear": 2022,
        "timezone": "Eastern European Time (UTC+02:00), as in OSCE report 40/2022",
        "period": ["2022-01-01T00:00" if official else "2022-02-14T00:00", "2022-02-21T00:00"],
        "events": events,
        "summary": {
            "rows": len(events), "quantity": sum(e["quantity"] or 0 for e in events),
            "unquantifiedRows": sum(e["quantity"] is None for e in events),
            "mappedRows": sum(e["coordinates"] is not None for e in events),
            "mappedQuantity": sum((e["quantity"] or 0) for e in events if e["coordinates"] is not None),
            "methods": dict(by_method), "incidentTypes": sorted({e["incident"] for e in events}),
            "distinctSites": len(sources),
            "mappedIncidentRows": sum(e["incidentPoint"] is not None for e in events),
            "relativeObserverRows": sum(e["relativeObserver"] for e in events),
            "mappedRelativeObserverRows": sum(e["relativeObserver"] and e["coordinates"] is not None for e in events),
            "unmappedPatrolRows": sum(not e["coordinates"] and not normalize(e["position"]).startswith("камера смм") for e in events),
            "distinctUnmappedPatrolPositions": len({e["position"] for e in events
                if not e["coordinates"] and not normalize(e["position"]).startswith("камера смм")}),
            "incidentTerritoryCounts": dict(Counter(e["incidentTerritory"] for e in events)),
        },
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "events.json").write_text(json.dumps(body, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (OUT / "geography.json").write_text(json.dumps(geography, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    by_position = {}
    for event in events:
        key = event["position"]
        if key not in by_position:
            by_position[key] = {"event": event, "rows": 0, "quantity": 0,
                                "first": event["start"], "last": event["start"]}
        by_position[key]["rows"] += 1
        by_position[key]["quantity"] += event["quantity"] or 0
        by_position[key]["first"] = min(by_position[key]["first"], event["start"])
        by_position[key]["last"] = max(by_position[key]["last"], event["start"])
    with (OUT / "observer_locations.csv").open("w", encoding="utf-8-sig", newline="") as audit:
        writer = csv.writer(audit, delimiter=";", lineterminator="\n")
        writer.writerow(["observer_description", "rows", "osce_quantity", "location_status",
                         "site_type", "site_name", "longitude", "latitude", "uncertainty_km",
                         "observer_control", "control_scope", "control_evidence", "location_basis", "reference_url"])
        for info in sorted(by_position.values(), key=lambda item: (-item["rows"], item["event"]["position"])):
            event = info["event"]
            point = event["coordinates"]
            writer.writerow([event["position"], info["rows"], info["quantity"],
                             "relative" if event["relativeObserver"] and point else
                             "unlocated" if not point else "approximate",
                             event["siteType"], event["siteName"],
                             round(point[0], 6) if point else "", round(point[1], 6) if point else "",
                             event["accuracyKm"] if point else "", event["observerControl"],
                             event["observerControlScope"],
                             event["observerControlSource"], event["locationBasis"],
                             event["locationReferenceUrl"] or ""])
    # A focused, reviewable worklist. Do not include the single deliberately
    # unmapped camera here: it is not a patrol.
    missing = [item for item in by_position.values()
               if item["event"]["coordinates"] is None and
               not normalize(item["event"]["position"]).startswith("камера смм")]
    with (OUT / "unmapped_patrols.csv").open("w", encoding="utf-8-sig", newline="") as audit:
        writer = csv.writer(audit, delimiter=";", lineterminator="\n")
        writer.writerow(["Позиция в отчёте ОБСЕ", "Причина отсутствия на карте",
                         "Сторона по тексту ОБСЕ", "Число строк", "Сумма К-во",
                         "Первое событие EET", "Последнее событие EET", "Пример источника"])
        for item in sorted(missing, key=lambda i: (-i["rows"], i["event"]["position"])):
            event = item["event"]
            citation = (f"{event['annexUrl']}#page={event['annexPage']}" if event["annexUrl"] else "")
            writer.writerow([event["position"], event["locationDiagnosis"],
                             event["observerControl"], item["rows"], item["quantity"],
                             item["first"], item["last"], citation])
    print(json.dumps(body["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    build()
