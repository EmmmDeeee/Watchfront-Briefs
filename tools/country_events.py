#!/usr/bin/env python3
"""Validate and index the Country Event Map feed published in Watchfront-Briefs.

Feed layout (relative to the published site root):

    country-events/manifest.json
    country-events/<ISO3>.json      one file per covered country

Commands:

    python Backend/country_events.py validate PATH/country-events [--previous OLD/country-events] [--check-links]
    python Backend/country_events.py manifest PATH/country-events [--generated-at 2026-11-10T18:00:00Z]

Standard library only. The validator enforces Watchfront's publication rules:
confirmed events only, no X-only reporting (X collection is not authorised),
attributed rather than assumed motives, symmetric ideology labels, independent
corroboration for serious claims, coordinates no more precise than the
reporting, stable event IDs, original 2-4 sentence summaries and HTTPS sources.
It cannot establish that a source is truthful; human review remains required.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
GEOMETRY = Path(os.environ.get("COUNTRY_EVENTS_GEOMETRY") or ROOT / "Watchfront/world-countries.geojson")

SCHEMA_VERSION = 1
LAUNCH_COUNTRIES = ("USA", "CAN", "GBR", "AUS", "NZL", "DEU", "FRA", "UKR")
CATEGORIES = {
    "civil-unrest", "protest", "political-violence", "terrorism", "security-operation",
    "armed-conflict", "politics", "economy", "infrastructure-cyber", "disaster", "other",
}
# Security categories use a closed vocabulary so labels stay consistent across ideologies.
SUBCATEGORIES = {
    "civil-unrest": {"riot", "violent-demonstration", "looting", "civil-disorder"},
    "protest": {"mass-protest", "strike", "blockade", "occupation"},
    "political-violence": {"assassination", "assassination-attempt", "attack-on-official",
                           "threat-against-official", "politically-motivated-killing", "political-attack"},
    "terrorism": {"attack", "suspected-attack", "foiled-plot", "threat", "arrests"},
    "security-operation": {"counterterrorism-operation", "raid", "deployment", "emergency-measures", "arrests"},
    "armed-conflict": {"air-strike", "missile-strike", "drone-strike", "ground-fighting", "insurgency", "mobilization"},
}
SERIOUS = {"terrorism", "political-violence"}
SOURCE_TYPES = {"national", "local", "official", "x"}
STATUSES = {"active", "updated", "withdrawn"}
PRECISION_DECIMALS = {"exact": 5, "locality": 4, "city": 3, "region": 2}
BOUNDARY_EXCEPTIONS = {"disputed-or-occupied", "offshore"}
MOTIVE_STATUSES = {"confirmed", "attributed", "under-investigation", "unknown", "not-applicable"}
IDEOLOGIES = {"islamist", "far-right", "far-left", "anti-government", "ethno-nationalist",
              "separatist", "single-issue", "other", "unknown", "not-applicable"}
TERRORISM_STATUSES = {"none", "investigated-as", "charged-as", "officially-designated"}
MAX_EVENTS = 400
ABBREVIATIONS = ("U.S.", "U.K.", "U.N.", "E.U.", "D.C.", "Mr.", "Mrs.", "Ms.", "Dr.", "St.", "Gen.",
                 "Lt.", "Sgt.", "Col.", "Gov.", "Sen.", "Rep.", "No.", "a.m.", "p.m.", "Jr.", "Sr.", "approx.")


class FeedError(ValueError):
    pass


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, where: str, message: str) -> None:
        self.errors.append(f"{where}: {message}")

    def warn(self, where: str, message: str) -> None:
        self.warnings.append(f"{where}: {message}")

    def raise_if_errors(self) -> None:
        if self.errors:
            raise FeedError("\n".join(self.errors))


# ---------------------------------------------------------------- helpers

def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)


def timestamp(value) -> dt.datetime | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
        return None
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def day(value) -> dt.date | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def day_or_timestamp(value) -> dt.date | None:
    stamp = timestamp(value)
    return stamp.date() if stamp else day(value)


def text(value, limit: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit and value == value.strip()


def member(value, allowed) -> bool:
    return isinstance(value, str) and value in allowed


def decimals(value) -> int:
    exponent = Decimal(str(value)).as_tuple().exponent
    return max(0, -exponent) if isinstance(exponent, int) else 99


def normalized_url(url: str) -> str:
    parts = urlsplit(url)
    query = "&".join(p for p in parts.query.split("&") if p and not p.lower().startswith(("utm_", "fbclid", "gclid")))
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), query, ""))


def secure_url(url) -> bool:
    if not isinstance(url, str) or len(url) > 2000:
        return False
    parts = urlsplit(url)
    return parts.scheme == "https" and bool(parts.hostname) and parts.username is None and parts.password is None


def sentences(summary: str) -> int:
    protected = summary
    for abbreviation in ABBREVIATIONS:
        protected = protected.replace(abbreviation, abbreviation.replace(".", "\u2024"))
    protected = re.sub(r"\b([A-Z])\.", "\\1\u2024", protected)  # initials
    parts = [p for p in re.split(r"(?<=[.!?])[\"'\u201d\u2019)]?\s+(?=[A-Z0-9\"'\u201c\u2018(])", protected.strip()) if p.strip()]
    return len(parts)


def tokens(value: str) -> set[str]:
    stop = {"the", "a", "an", "in", "of", "on", "at", "to", "and", "for", "after", "as", "with", "by", "over"}
    return {t for t in re.findall(r"[a-z0-9]+", value.lower()) if t not in stop and len(t) > 2}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- geometry

class Geometry:
    def __init__(self, path: Path = GEOMETRY) -> None:
        self.countries: dict[str, list[list[list[tuple[float, float]]]]] = {}
        self.names: dict[str, str] = {}
        # Without the border file (e.g. in Watchfront-Briefs) border checks are skipped, with a warning.
        self.available = path.exists()
        if not self.available:
            self.countries = {c: [] for c in LAUNCH_COUNTRIES}
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        for feature in data["features"]:
            props, geometry = feature["properties"], feature["geometry"]
            polygons = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
            self.countries[props["id"]] = [[[(float(x), float(y)) for x, y, *_ in ring] for ring in polygon] for polygon in polygons]
            self.names[props["id"]] = props.get("name_long") or props.get("name")

    @staticmethod
    def _inside(lon: float, lat: float, ring) -> bool:
        inside = False
        for i in range(len(ring)):
            x1, y1 = ring[i]; x2, y2 = ring[i - 1]
            if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
                inside = not inside
        return inside

    @staticmethod
    def _edge_distance(lon: float, lat: float, ring) -> float:
        best = float("inf")
        for i in range(len(ring)):
            (x1, y1), (x2, y2) = ring[i - 1], ring[i]
            dx, dy = x2 - x1, y2 - y1
            t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((lon - x1) * dx + (lat - y1) * dy) / (dx * dx + dy * dy)))
            best = min(best, ((lon - x1 - t * dx) ** 2 + (lat - y1 - t * dy) ** 2) ** 0.5)
        return best

    def contains(self, country: str, lat: float, lon: float, tolerance: float = 0.12) -> bool:
        polygons = self.countries.get(country, [])
        for polygon in polygons:
            if self._inside(lon, lat, polygon[0]) and not any(self._inside(lon, lat, hole) for hole in polygon[1:]):
                return True
        # Generalised coastlines and borders: accept points within ~10 km of the outline.
        return any(self._edge_distance(lon, lat, polygon[0]) <= tolerance for polygon in polygons)


# ---------------------------------------------------------------- validation

def validate_event(event, country: str, generated: dt.datetime, window_days: int,
                   geometry: Geometry | None, report: Report, allow_x: bool = False) -> None:
    where = f"{country}/{event.get('id', '?') if isinstance(event, dict) else '?'}"
    if not isinstance(event, dict):
        report.error(where, "event must be an object"); return
    required = {"id", "title", "summary", "location", "geo", "category", "occurredOn", "firstReportedAt",
                "publishedAt", "updatedAt", "significance", "verification", "primarySource", "sources", "status"}
    missing = sorted(required - event.keys())
    if missing:
        report.error(where, f"missing fields {missing}"); return

    occurred = day(event["occurredOn"])
    event_id = event["id"]
    if not isinstance(event_id, str) or len(event_id) > 100 or not re.fullmatch(
            rf"{country.lower()}-\d{{4}}-\d{{2}}-\d{{2}}-[a-z0-9]+(-[a-z0-9]+)*", event_id):
        report.error(where, "id must be '<iso3>-<occurredOn>-<slug>' in lowercase, max 100 characters")
    elif occurred and not event_id.startswith(f"{country.lower()}-{event['occurredOn']}-"):
        report.error(where, "id date must match occurredOn (IDs are permanent; correct the date with a withdrawal and new event)")

    title, summary = event["title"], event["summary"]
    if not text(title, 110) or len(title) < 8 or title.endswith("."):
        report.error(where, "title must be 8-110 characters, trimmed, without a trailing period")
    if not text(summary, 700) or len(summary) < 120:
        report.error(where, "summary must be 120-700 characters")
    elif not 2 <= sentences(summary) <= 4:
        report.error(where, f"summary must be 2-4 sentences (found {sentences(summary)})")
    for field in (title, summary):
        if isinstance(field, str) and (re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", field) or re.search(r"\+?\d[\d ()-]{8,}\d", field)):
            report.error(where, "title/summary must not contain email addresses or phone numbers")
        if isinstance(field, str) and re.search(r"https?://|\[\d+\]", field):
            report.error(where, "title/summary must not contain URLs or citation markers")

    location = event["location"]
    if not isinstance(location, dict) or not text(location.get("country"), 80):
        report.error(where, "location.country is required")
        location = {}
    for key in ("city", "region"):
        if location.get(key) is not None and not text(location.get(key), 80):
            report.error(where, f"location.{key} must be a trimmed string or null")
    if set(location) - {"city", "region", "country"}:
        report.error(where, "location supports only city, region and country")

    geo = event["geo"]
    if geo is not None:
        if not isinstance(geo, dict) or set(geo) - {"latitude", "longitude", "precision", "boundaryException"}:
            report.error(where, "geo must contain latitude, longitude, precision and optional boundaryException")
        else:
            lat, lon, precision = geo.get("latitude"), geo.get("longitude"), geo.get("precision")
            if not all(isinstance(v, (int, Decimal)) and not isinstance(v, bool) for v in (lat, lon)) or not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
                report.error(where, "geo latitude/longitude must be numbers in range")
            elif not member(precision, PRECISION_DECIMALS):
                report.error(where, f"geo.precision must be one of {sorted(PRECISION_DECIMALS)}; use geo: null for national developments")
            else:
                if max(decimals(lat), decimals(lon)) > PRECISION_DECIMALS[precision]:
                    report.error(where, f"{precision}-precision coordinates allow at most {PRECISION_DECIMALS[precision]} decimal places (no fabricated precision)")
                if precision in ("city", "locality", "exact") and not location.get("city"):
                    report.error(where, f"{precision}-precision coordinates require location.city")
                if precision == "region" and not location.get("region"):
                    report.error(where, "region-precision coordinates require location.region")
                exception = geo.get("boundaryException")
                if exception is not None and not member(exception, BOUNDARY_EXCEPTIONS):
                    report.error(where, f"geo.boundaryException must be one of {sorted(BOUNDARY_EXCEPTIONS)}")
                if geometry and geometry.available and not geometry.contains(country, float(lat), float(lon)):
                    if exception:
                        report.warn(where, f"coordinates lie outside {country} map geometry ({exception}); confirm in review")
                    else:
                        report.error(where, f"coordinates fall outside {country}; correct them or set geo.boundaryException")

    category = event["category"] if isinstance(event["category"], str) else ""
    if category not in CATEGORIES:
        report.error(where, f"category must be one of {sorted(CATEGORIES)}")
    subcategory = event.get("subcategory")
    if category in SUBCATEGORIES:
        if not member(subcategory, SUBCATEGORIES[category]):
            report.error(where, f"{category} requires subcategory in {sorted(SUBCATEGORIES[category])}")
    elif subcategory is not None and not (isinstance(subcategory, str) and re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", subcategory)):
        report.error(where, "subcategory must be a lowercase slug")
    if not member(event["significance"], {"national", "major-regional"}):
        report.error(where, "significance must be 'national' or 'major-regional' (routine local incidents are out of scope)")

    # firstReportedAt may be a date when the reporting does not establish a time.
    first_reported = day_or_timestamp(event["firstReportedAt"])
    if first_reported is None:
        report.error(where, "firstReportedAt must be a date or ISO-8601 UTC timestamp")
    stamps = {k: timestamp(event[k]) for k in ("publishedAt", "updatedAt")}
    for key, value in stamps.items():
        if value is None:
            report.error(where, f"{key} must be an ISO-8601 UTC timestamp (YYYY-MM-DDTHH:MM:SSZ)")
    until = event.get("occurredUntil")
    last = occurred
    if occurred is None:
        report.error(where, "occurredOn must be YYYY-MM-DD")
    else:
        if occurred > generated.date():
            report.error(where, "occurredOn is in the future")
        if until is not None:
            last = day(until)
            if last is None or last < occurred:
                report.error(where, "occurredUntil must be a date on or after occurredOn"); last = occurred
        if first_reported and first_reported < occurred - dt.timedelta(days=1):
            report.error(where, "firstReportedAt precedes occurredOn")
        if event["status"] != "withdrawn" and last and last < generated.date() - dt.timedelta(days=window_days):
            report.error(where, f"event is older than the {window_days}-day window; remove it from the feed")
    if all(stamps.values()):
        if (first_reported and first_reported > stamps["updatedAt"].date()) or not stamps["publishedAt"] <= stamps["updatedAt"] <= generated:
            report.error(where, "dates must satisfy firstReportedAt <= updatedAt and publishedAt <= updatedAt <= generatedAt")

    status = event["status"]
    if not member(status, STATUSES):
        report.error(where, f"status must be one of {sorted(STATUSES)}")
    elif status in ("updated", "withdrawn"):
        correction = event.get("correction")
        if not isinstance(correction, dict) or not text(correction.get("note"), 600) or timestamp(correction.get("correctedAt")) is None:
            report.error(where, f"{status} events require correction.note and correction.correctedAt")

    # Sources.
    sources = event["sources"]
    if not isinstance(sources, list) or not 1 <= len(sources) <= 12:
        report.error(where, "sources must list 1-12 reports"); sources = []
    seen_urls, publishers, types = set(), set(), []
    for index, source in enumerate(sources):
        here = f"{where} source[{index}]"
        if not isinstance(source, dict):
            report.error(here, "source must be an object"); continue
        if not text(source.get("publisher"), 80) or not text(source.get("title"), 300):
            report.error(here, "publisher and title are required")
        if not secure_url(source.get("url")):
            report.error(here, "url must be HTTPS without credentials")
        else:
            key = normalized_url(source["url"])
            if key in seen_urls:
                report.error(here, "duplicate source URL")
            seen_urls.add(key)
        kind = source.get("type")
        if not member(kind, SOURCE_TYPES):
            report.error(here, f"type must be one of {sorted(SOURCE_TYPES)}")
        elif kind == "x" and not allow_x:
            report.error(here, "X sources are not authorised; automated X collection is disabled")
        types.append(kind)
        published = day_or_timestamp(source.get("publishedAt"))
        if published is None or published > generated.date():
            report.error(here, "publishedAt must be a past date or timestamp")
        if source.get("retrievedAt") is not None and timestamp(source.get("retrievedAt")) is None:
            report.error(here, "retrievedAt must be an ISO-8601 UTC timestamp")
        if kind != "x" and isinstance(source.get("publisher"), str):
            publishers.add(source["publisher"].strip().lower())
    primary = event["primarySource"]
    if not isinstance(primary, int) or isinstance(primary, bool) or not 0 <= primary < len(sources):
        report.error(where, "primarySource must index sources")
    elif types and types[primary] == "x":
        report.error(where, "the displayed source must be an established report, not an X post")
    if types and all(t == "x" for t in types):
        report.error(where, "events need at least one non-X source")

    # Verification.
    verification = event["verification"]
    if not isinstance(verification, dict):
        report.error(where, "verification must be an object"); return
    if verification.get("eventConfirmed") is not True:
        report.error(where, "only confirmed events may be published (verification.eventConfirmed: true)")
    reviewed = timestamp(verification.get("reviewedAt"))
    if reviewed is None or reviewed > generated:
        report.error(where, "verification.reviewedAt must be a timestamp no later than generatedAt")
    independent = verification.get("independentSources")
    if not isinstance(independent, int) or isinstance(independent, bool) or independent < 1:
        report.error(where, "verification.independentSources must be a positive integer"); independent = 0
    elif independent > len(publishers):
        report.error(where, "independentSources exceeds the number of distinct non-X publishers cited")
    official = "official" in types
    disputed = verification.get("disputed", False)
    if not isinstance(disputed, bool):
        report.error(where, "verification.disputed must be a boolean")
    if (category in SERIOUS or disputed) and independent < 2 and not official:
        report.error(where, "serious or disputed claims need two independent sources or an official source")

    def attribution(key: str, allowed: set[str]):
        value = verification.get(key)
        if value is None:
            if category in SERIOUS:
                report.error(where, f"verification.{key} is required for {category}")
            return None
        if not isinstance(value, dict) or not member(value.get("status"), allowed):
            report.error(where, f"verification.{key}.status must be one of {sorted(allowed)}")
            return None
        return value

    motive = attribution("motive", MOTIVE_STATUSES)
    if motive:
        if motive["status"] in ("confirmed", "attributed") and not text(motive.get("attributedTo"), 200):
            report.error(where, "a confirmed or attributed motive must name who established or stated it (attributedTo)")
        if motive["status"] == "confirmed" and not official and independent < 2:
            report.error(where, "a confirmed motive needs an official source or two independent sources")
    ideology = verification.get("ideology")
    if ideology is not None or category in SERIOUS:
        if not isinstance(ideology, dict) or not member(ideology.get("label"), IDEOLOGIES) or not member(ideology.get("status"), MOTIVE_STATUSES):
            report.error(where, f"verification.ideology needs label in {sorted(IDEOLOGIES)} and status in {sorted(MOTIVE_STATUSES)}")
        elif ideology["label"] not in ("unknown", "not-applicable"):
            # The same evidentiary rule applies to every ideology label.
            if ideology["status"] not in ("confirmed", "attributed") or not text(ideology.get("attributedTo"), 200):
                report.error(where, "an ideology label must be confirmed or attributed to a named authority or source")
    terrorism = attribution("terrorism", TERRORISM_STATUSES)
    if category == "terrorism" and (terrorism is None or terrorism.get("status") == "none"):
        report.error(where, "the terrorism category requires authorities to be investigating, charging or designating the act as terrorism")
    if terrorism and terrorism["status"] != "none":
        if not text(terrorism.get("authority"), 200):
            report.error(where, "a terrorism status must name the authority (terrorism.authority)")
        if not official and independent < 2:
            report.error(where, "a terrorism status needs an official source or two independent sources")
    if (terrorism is None or terrorism.get("status") == "none") and isinstance(title, str) and isinstance(summary, str):
        if re.search(r"\bterror", f"{title} {summary}", re.IGNORECASE):
            report.error(where, "do not describe an event as terrorism unless an authority is investigating, charging or designating it as such")
    if motive and motive["status"] == "under-investigation" and isinstance(summary, str) and not re.search(r"investigat|inquir|unclear|unknown|not (yet )?(been )?(established|confirmed)", summary, re.IGNORECASE):
        report.warn(where, "motive is under investigation; the summary should say so")


def validate_country_file(data, country: str, geometry: Geometry | None, report: Report, allow_x: bool = False):
    where = f"{country}.json"
    if not isinstance(data, dict):
        report.error(where, "file must be an object"); return None
    keys = {"schemaVersion", "countryID", "revision", "generatedAt", "windowDays", "events"}
    if set(data) != keys:
        report.error(where, f"top-level fields must be exactly {sorted(keys)}"); return None
    if data["schemaVersion"] != SCHEMA_VERSION:
        report.error(where, f"schemaVersion must be {SCHEMA_VERSION}")
    if data["countryID"] != country:
        report.error(where, "countryID must match the file name")
    if not isinstance(data["revision"], int) or data["revision"] < 1:
        report.error(where, "revision must be a positive integer")
    generated = timestamp(data["generatedAt"])
    if generated is None:
        report.error(where, "generatedAt must be an ISO-8601 UTC timestamp"); return None
    if generated > dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10):
        report.error(where, "generatedAt is in the future")
    window = data["windowDays"]
    if window != 30:
        report.error(where, "windowDays must be 30")
        window = 30
    events = data["events"]
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        report.error(where, f"events must be a list of at most {MAX_EVENTS}"); return None
    ids = [e.get("id") for e in events if isinstance(e, dict)]
    for duplicate in sorted({i for i in ids if ids.count(i) > 1}):
        report.error(where, f"duplicate event id {duplicate}")
    for event in events:
        validate_event(event, country, generated, window, geometry, report, allow_x)
    find_duplicates(events, country, report)
    return data


def find_duplicates(events, country: str, report: Report) -> None:
    live = [e for e in events if isinstance(e, dict) and isinstance(e.get("id"), str)
            and e.get("status") != "withdrawn" and day(e.get("occurredOn"))]
    primaries: dict[str, str] = {}
    for event in live:
        sources = event.get("sources") if isinstance(event.get("sources"), list) else []
        index = event.get("primarySource")
        if isinstance(index, int) and 0 <= index < len(sources) and isinstance(sources[index], dict) and isinstance(sources[index].get("url"), str):
            key = normalized_url(sources[index]["url"])
            if key in primaries:
                report.error(f"{country}/{event['id']}", f"shares its displayed source with {primaries[key]}; merge duplicate reports")
            primaries[key] = event["id"]
    for i, a in enumerate(live):
        for b in live[i + 1:]:
            if abs((day(a["occurredOn"]) - day(b["occurredOn"])).days) > 2:
                continue
            place_a = ((a.get("location") or {}).get("city"), (a.get("location") or {}).get("region"))
            place_b = ((b.get("location") or {}).get("city"), (b.get("location") or {}).get("region"))
            if place_a != place_b:
                continue
            ta, tb = tokens(str(a.get("title"))), tokens(str(b.get("title")))
            if ta and tb and len(ta & tb) / len(ta | tb) >= 0.5:
                report.error(f"{country}/{b['id']}", f"appears to duplicate {a['id']} (same place, dates and headline terms)")


def visible_count(data) -> int:
    return sum(1 for e in data["events"] if isinstance(e, dict) and e.get("status") != "withdrawn")


def validate_feed(directory: Path, previous: Path | None = None, geometry: Geometry | None = None,
                  allow_x: bool = False) -> Report:
    report = Report()
    geometry = geometry or Geometry()
    if not geometry.available:
        report.warn("geometry", "country border file not found; marker border checks skipped")
    manifest_path = directory / "manifest.json"
    if not manifest_path.exists():
        report.error("manifest.json", "missing"); return report
    manifest = load_json(manifest_path)
    keys = {"schemaVersion", "revision", "generatedAt", "windowDays", "countries"}
    if not isinstance(manifest, dict) or set(manifest) != keys:
        report.error("manifest.json", f"top-level fields must be exactly {sorted(keys)}"); return report
    if manifest["schemaVersion"] != SCHEMA_VERSION or manifest["windowDays"] != 30:
        report.error("manifest.json", "schemaVersion must be 1 and windowDays 30")
    if not isinstance(manifest["revision"], int) or manifest["revision"] < 1:
        report.error("manifest.json", "revision must be a positive integer")
    manifest_generated = timestamp(manifest["generatedAt"])
    if manifest_generated is None:
        report.error("manifest.json", "generatedAt must be an ISO-8601 UTC timestamp")
    entries = manifest["countries"] if isinstance(manifest["countries"], list) else []
    listed = set()
    for entry in entries:
        country = entry.get("countryID") if isinstance(entry, dict) else None
        where = f"manifest.json/{country}"
        if not isinstance(country, str) or not re.fullmatch(r"[A-Z]{3}", country) or country not in geometry.countries:
            report.error(where, "countryID must be a mapped ISO 3166-1 alpha-3 code"); continue
        if country in listed:
            report.error(where, "listed twice")
        listed.add(country)
        if set(entry) != {"countryID", "path", "sha256", "revision", "updatedAt", "eventCount"}:
            report.error(where, "entry fields must be countryID, path, sha256, revision, updatedAt, eventCount")
            continue
        if entry["path"] != f"{country}.json":
            report.error(where, f"path must be '{country}.json'"); continue
        path = directory / entry["path"]
        if not path.exists():
            report.error(where, "country file missing"); continue
        raw = path.read_bytes()
        if entry["sha256"] != sha256(raw):
            report.error(where, "sha256 does not match the country file; rebuild the manifest")
        data = validate_country_file(load_json(path), country, geometry, report, allow_x)
        if data is None:
            continue
        if entry["revision"] != data["revision"]:
            report.error(where, "revision does not match the country file")
        if entry["updatedAt"] != data["generatedAt"]:
            report.error(where, "updatedAt must equal the country file's generatedAt")
        if entry["eventCount"] != visible_count(data):
            report.error(where, "eventCount must equal the number of non-withdrawn events")
        if manifest_generated and timestamp(data["generatedAt"]) and timestamp(data["generatedAt"]) > manifest_generated:
            report.error(where, "country file is newer than the manifest")
        if previous:
            compare_previous(previous / entry["path"], raw, data, country, report)
    for path in sorted(directory.glob("*.json")):
        if path.name != "manifest.json" and path.stem not in listed:
            report.error(path.name, "country file is not listed in the manifest")
    missing = [c for c in LAUNCH_COUNTRIES if c not in listed]
    if missing:
        report.warn("manifest.json", f"launch countries without a feed: {', '.join(missing)}")
    if previous and (previous / "manifest.json").exists():
        old = load_json(previous / "manifest.json")
        changed = (previous / "manifest.json").read_bytes() != manifest_path.read_bytes()
        if changed and isinstance(old, dict) and isinstance(old.get("revision"), int) and manifest["revision"] <= old["revision"]:
            report.error("manifest.json", "revision must increase when the manifest changes")
        dropped = {e.get("countryID") for e in old.get("countries", []) if isinstance(e, dict)} - listed
        if dropped:
            report.warn("manifest.json", f"countries removed from coverage: {', '.join(sorted(map(str, dropped)))}")
    return report


def compare_previous(path: Path, raw: bytes, data, country: str, report: Report) -> None:
    if not path.exists():
        return
    old_raw = path.read_bytes()
    old = load_json(path)
    if old_raw != raw and data["revision"] <= old.get("revision", 0):
        report.error(f"{country}.json", "revision must increase when the file changes")
    generated = timestamp(data["generatedAt"])
    current = {e["id"]: e for e in data["events"] if isinstance(e, dict) and "id" in e}
    for event in old.get("events", []):
        if not isinstance(event, dict) or event.get("status") == "withdrawn":
            continue
        last = day(event.get("occurredUntil") or event.get("occurredOn"))
        if event.get("id") not in current and generated and last and last >= generated.date() - dt.timedelta(days=data["windowDays"]):
            report.error(f"{country}/{event.get('id')}", "published event disappeared inside the 30-day window; mark it withdrawn with a correction note")
        elif event.get("id") in current:
            new = current[event["id"]]
            if new != event and new.get("updatedAt") == event.get("updatedAt"):
                report.error(f"{country}/{event['id']}", "event changed without a new updatedAt")
            if new.get("summary") != event.get("summary") and new.get("status") == "active":
                report.warn(f"{country}/{event['id']}", "published summary changed while status is active; use status 'updated' with a correction note for material changes")


def unchanged_events(directory: Path, previous: Path | None) -> set[str]:
    """Ids of events identical to their version in the previous (published) feed."""
    if not previous:
        return set()
    old = {}
    for path in previous.glob("*.json"):
        if path.name != "manifest.json":
            old.update({e["id"]: e for e in load_json(path).get("events", []) if isinstance(e, dict) and "id" in e})
    same = set()
    for path in directory.glob("*.json"):
        if path.name != "manifest.json":
            same.update(e["id"] for e in load_json(path).get("events", []) if isinstance(e, dict) and old.get(e.get("id")) == e)
    return same


def check_links(directory: Path, report: Report, timeout: float = 15.0, skip: set[str] = frozenset()) -> None:
    for path in sorted(directory.glob("*.json")):
        if path.name == "manifest.json":
            continue
        for event in load_json(path).get("events", []):
            if event.get("id") in skip:
                continue   # published and unchanged: its links were checked when it was published
            for source in event.get("sources", []):
                url = source.get("url")
                if not secure_url(url):
                    continue
                request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; Watchfront link check)"})
                try:
                    with urllib.request.urlopen(request, timeout=timeout) as response:
                        if response.status >= 400:
                            report.error(f"{path.stem}/{event['id']}", f"source returned HTTP {response.status}: {url}")
                except urllib.error.HTTPError as error:
                    if error.code in (401, 403, 429, 451):
                        report.warn(f"{path.stem}/{event['id']}", f"source refused automated check (HTTP {error.code}); verify manually: {url}")
                    else:
                        report.error(f"{path.stem}/{event['id']}", f"source returned HTTP {error.code}: {url}")
                except (urllib.error.URLError, TimeoutError, OSError) as error:
                    report.warn(f"{path.stem}/{event['id']}", f"source could not be reached ({error}); verify manually: {url}")


# ---------------------------------------------------------------- manifest

def build_manifest(directory: Path, generated_at: str | None = None) -> dict:
    manifest_path = directory / "manifest.json"
    previous = load_json(manifest_path) if manifest_path.exists() else None
    countries = []
    for path in sorted(directory.glob("*.json")):
        if path.name == "manifest.json":
            continue
        raw = path.read_bytes()
        data = json.loads(raw)
        countries.append({"countryID": data["countryID"], "path": path.name, "sha256": sha256(raw),
                          "revision": data["revision"], "updatedAt": data["generatedAt"], "eventCount": visible_count(data)})
    newest = max((c["updatedAt"] for c in countries), default=None)
    generated_at = generated_at or newest or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = {"schemaVersion": SCHEMA_VERSION, "revision": 1, "generatedAt": generated_at, "windowDays": 30, "countries": countries}
    if isinstance(previous, dict):
        unchanged = previous.get("countries") == countries
        manifest["revision"] = previous.get("revision", 0) if unchanged else previous.get("revision", 0) + 1
        if unchanged:
            manifest["generatedAt"] = previous.get("generatedAt", generated_at)
    return manifest


def write_json(path: Path, value) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    v = sub.add_parser("validate", help="validate a country-events directory")
    v.add_argument("directory", type=Path)
    v.add_argument("--previous", type=Path, help="the currently published country-events directory")
    v.add_argument("--check-links", action="store_true", help="request source URLs of new or changed events (network)")
    v.add_argument("--all-links", action="store_true", help="with --check-links, also request links of unchanged published events")
    m = sub.add_parser("manifest", help="rebuild manifest.json from the country files")
    m.add_argument("directory", type=Path)
    m.add_argument("--generated-at")
    args = parser.parse_args(argv)
    if args.command == "manifest":
        write_json(args.directory / "manifest.json", build_manifest(args.directory, args.generated_at))
        print(f"Wrote {args.directory / 'manifest.json'}")
        return 0
    report = validate_feed(args.directory, args.previous)
    if args.check_links:
        skip = set() if args.all_links else unchanged_events(args.directory, args.previous)
        check_links(args.directory, report, skip=skip)
        if skip:
            print(f"Link check: {len(skip)} published, unchanged event(s) skipped (use --all-links to include them)")
    for warning in report.warnings:
        print(f"WARNING {warning}")
    for error in report.errors:
        print(f"ERROR   {error}")
    print(f"{len(report.errors)} error(s), {len(report.warnings)} warning(s)")
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
