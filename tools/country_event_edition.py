#!/usr/bin/env python3
"""Build Country Event Map editions from the published feed, deterministically.

Three commands, all standard-library Python:

  expire   Remove events whose last day is older than the 30-day window, and
           nothing else. Every remaining event is copied byte-for-byte; only the
           files that lose an event get a new revision; the manifest is rebuilt.
           Used by the daily expiry job in Watchfront-Briefs.

               python country_event_edition.py expire PUBLISHED_DIR OUT_DIR [--date YYYY-MM-DD]

  build    Apply a weekly editorial changes file (additions, corrections,
           withdrawals) on top of the published feed, then expire old events.
           Used by the weekly research session; output goes to the private
           candidate folder for review, never straight to publication.

               python country_event_edition.py build FOLDER --published PUBLISHED_DIR [--at ISO]

           FOLDER/changes.json:
             {"add":      [<complete event objects; publishedAt/updatedAt/reviewedAt are filled in>],
              "update":   [{"id": "...", "set": {<top-level fields to replace>},
                            "correction": "note for a material change, or null for a minor fix"}],
              "withdraw": [{"id": "...", "note": "why the report was withdrawn"}],
              "hold":     [{"id": "...", "title": "...", "reason": "..."}]}

  review   Write a Markdown review of every addition, modification, withdrawal,
           expiry and hold between the published feed and an edition.

               python country_event_edition.py review EDITION_DIR --published PUBLISHED_DIR [--changes changes.json] [--out REVIEW.md]

The output of expire and build is validated with country_events.py against the
published feed before anything is proposed.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import country_events as ce  # noqa: E402

PREFIX = {"usa": "USA", "can": "CAN", "gbr": "GBR", "fra": "FRA", "ukr": "UKR", "deu": "DEU", "aus": "AUS", "nzl": "NZL"}


def now_stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def last_day(event: dict) -> dt.date:
    return dt.date.fromisoformat(event.get("occurredUntil") or event["occurredOn"])


def expired(event: dict, on: dt.date, window: int = 30) -> bool:
    """Same rule as the validator: out of the window once the last day is before on - window."""
    return last_day(event) < on - dt.timedelta(days=window)


def country_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.glob("*.json") if p.name != "manifest.json")


def dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def finish(out: Path, published: Path, stamp: str, changed: set[str]) -> None:
    """Write the manifest; unchanged country files stay byte-identical to the published ones."""
    shutil.copyfile(published / "manifest.json", out / "manifest.json")
    if changed:
        ce.write_json(out / "manifest.json", ce.build_manifest(out, stamp))


def cmd_expire(published: Path, out: Path, on: dt.date, stamp: str) -> list[tuple[str, str, str]]:
    out.mkdir(parents=True, exist_ok=True)
    removed: list[tuple[str, str, str]] = []
    changed: set[str] = set()
    for path in country_files(published):
        raw = path.read_bytes()
        data = json.loads(raw)
        keep = [e for e in data["events"] if not expired(e, on, data["windowDays"])]
        gone = [e for e in data["events"] if expired(e, on, data["windowDays"])]
        if not gone:
            (out / path.name).write_bytes(raw)
            continue
        removed += [(data["countryID"], e["id"], e.get("occurredUntil") or e["occurredOn"]) for e in gone]
        new = dict(data, revision=data["revision"] + 1, generatedAt=stamp, events=keep)
        (out / path.name).write_text(dump(new), encoding="utf-8")
        changed.add(data["countryID"])
    finish(out, published, stamp, changed)
    verify_expiry(published, out, on)
    return removed


def verify_expiry(published: Path, out: Path, on: dt.date) -> None:
    """Independent check: only expired events left, nothing else changed."""
    for path in country_files(published):
        old = json.loads(path.read_bytes())
        new = json.loads((out / path.name).read_bytes())
        new_ids = {e["id"]: e for e in new["events"]}
        for e in old["events"]:
            if expired(e, on, old["windowDays"]):
                assert e["id"] not in new_ids, f"{e['id']} should have expired"
            else:
                assert new_ids.get(e["id"]) == e, f"{e['id']} changed or disappeared"
        assert len(new["events"]) == sum(1 for e in old["events"] if not expired(e, on, old["windowDays"]))
        if new["events"] == old["events"]:
            assert (out / path.name).read_bytes() == path.read_bytes(), f"{path.name} rewritten without a change"


def cmd_build(folder: Path, published: Path, stamp: str) -> int:
    changes = json.loads((folder / "changes.json").read_text())
    out = folder / "country-events"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    on = dt.date.fromisoformat(stamp[:10])
    docs = {p.stem: json.loads(p.read_bytes()) for p in country_files(published)}
    originals = {k: p.read_bytes() for k, p in ((p.stem, p) for p in country_files(published))}
    touched: set[str] = set()

    def find(eid: str) -> tuple[str, dict]:
        iso = PREFIX.get(eid.split("-", 1)[0])
        for event in docs.get(iso, {}).get("events", []):
            if event["id"] == eid:
                return iso, event
        raise SystemExit(f"{eid}: not in the published feed")

    for item in changes.get("update", []):
        iso, event = find(item["id"])
        for key, value in item["set"].items():
            if key in ("id", "publishedAt", "updatedAt", "status", "correction"):
                raise SystemExit(f"{item['id']}: '{key}' cannot be set directly")
            event[key] = value
        event["updatedAt"] = stamp
        event.setdefault("verification", {})["reviewedAt"] = stamp
        if item.get("correction"):
            event["status"] = "updated"
            event["correction"] = {"note": item["correction"], "correctedAt": stamp}
        touched.add(iso)
    for item in changes.get("withdraw", []):
        iso, event = find(item["id"])
        event["status"] = "withdrawn"
        event["correction"] = {"note": item["note"], "correctedAt": stamp}
        event["updatedAt"] = stamp
        touched.add(iso)
    for event in changes.get("add", []):
        event = copy.deepcopy(event)
        iso = PREFIX.get(event["id"].split("-", 1)[0])
        if iso not in docs:
            raise SystemExit(f"{event['id']}: id must start with a launch-country prefix {sorted(PREFIX)}")
        if any(e["id"] == event["id"] for e in docs[iso]["events"]):
            raise SystemExit(f"{event['id']}: already published; use update")
        event["publishedAt"] = event["updatedAt"] = stamp
        event.setdefault("verification", {})["reviewedAt"] = stamp
        event.setdefault("status", "active")
        docs[iso]["events"].append(event)
        touched.add(iso)

    changed: set[str] = set()
    for iso, data in sorted(docs.items()):
        before = len(data["events"])
        data["events"] = [e for e in data["events"] if not expired(e, on, data["windowDays"])]
        if iso in touched or len(data["events"]) != before:
            data["events"].sort(key=lambda e: (e["occurredOn"], e["id"]), reverse=True)
            data["revision"] += 1
            data["generatedAt"] = stamp
            (out / f"{iso}.json").write_text(dump(data), encoding="utf-8")
            changed.add(iso)
        else:
            (out / f"{iso}.json").write_bytes(originals[iso])
    finish(out, published, stamp, changed)
    print(f"Built {out}: {len(changes.get('add', []))} added, {len(changes.get('update', []))} updated, "
          f"{len(changes.get('withdraw', []))} withdrawn; files changed: {', '.join(sorted(changed)) or 'none'}")
    return 0


def describe(event: dict) -> list[str]:
    loc = ", ".join(x for x in (event["location"].get("city"), event["location"].get("region")) if x) or "National (no map marker)"
    if event.get("geo"):
        g = event["geo"]
        loc += f" (marker {g['latitude']}, {g['longitude']}, {g['precision']} precision)"
    date = event["occurredOn"] + (f" to {event['occurredUntil']}" if event.get("occurredUntil") else "")
    lines = [f"- Date: {date}", f"- Location: {loc}",
             f"- Category: {event['category']}" + (f" / {event['subcategory']}" if event.get("subcategory") else ""),
             f"- Description: {event['summary']}"]
    for i, s in enumerate(event["sources"]):
        tag = "Primary source" if i == event["primarySource"] else "Source"
        lines.append(f"- {tag}: {s['publisher']}, \"{s['title']}\" ({s['publishedAt']}): {s['url']}")
    return lines


def cmd_review(edition: Path, published: Path, changes_path: Path | None) -> str:
    old = {e["id"]: (p.stem, e) for p in country_files(published) for e in json.loads(p.read_bytes())["events"]}
    new = {e["id"]: (p.stem, e) for p in country_files(edition) for e in json.loads(p.read_bytes())["events"]}
    manifest = json.loads((edition / "manifest.json").read_text())
    added = [new[i] for i in new if i not in old]
    removed = [old[i] for i in old if i not in new]
    modified = [(old[i], new[i]) for i in new if i in old and new[i][1] != old[i][1]]
    withdrawn = [m for m in modified if m[1][1]["status"] == "withdrawn"]
    modified = [m for m in modified if m[1][1]["status"] != "withdrawn"]
    holds = json.loads(changes_path.read_text()).get("hold", []) if changes_path and changes_path.exists() else []
    counts = {c["countryID"]: c["eventCount"] for c in manifest["countries"]}
    out = [f"# Country Event Map — proposed revision {manifest['revision']}", "",
           f"Built {manifest['generatedAt']}. **Not published.** Nothing reaches Watchfront-Briefs until the owner approves.", "",
           f"- Added: {len(added)}", f"- Modified: {len(modified)}", f"- Withdrawn: {len(withdrawn)}",
           f"- Removed (out of the 30-day window): {len(removed)}", f"- Held: {len(holds)}",
           f"- Unchanged: {len(new) - len(added) - len(modified) - len(withdrawn)}", "",
           "| Country | Events after this update |", "|---|---|"]
    out += [f"| {c} | {n} |" for c, n in sorted(counts.items())] + [f"| **Total** | **{sum(counts.values())}** |", ""]
    out += ["## Added", ""]
    for iso, e in sorted(added, key=lambda x: (x[0], x[1]["occurredOn"])):
        out += [f"### {iso}: {e['title']}", f"`{e['id']}`", ""] + describe(e) + [""]
    out += ["## Modified", ""]
    for (iso, a), (_, b) in modified:
        out += [f"### {iso}: {b['title']}", f"`{b['id']}` — status {b['status']}" +
                (f"; correction: {b['correction']['note']}" if b.get("correction") else ""), ""]
        for key in sorted(set(a) | set(b)):
            if key in ("updatedAt",) or a.get(key) == b.get(key):
                continue
            if key == "verification" and {k: v for k, v in a[key].items() if k != "reviewedAt"} == {k: v for k, v in b[key].items() if k != "reviewedAt"}:
                continue
            out.append(f"- **{key}** was: {json.dumps(a.get(key), ensure_ascii=False)[:600]}")
            out.append(f"  - now: {json.dumps(b.get(key), ensure_ascii=False)[:600]}")
        out.append("")
    out += ["## Withdrawn", ""]
    for (_, a), (iso, b) in withdrawn:
        out += [f"- {iso}: {b['title']} (`{b['id']}`) — {b['correction']['note']}"]
    out += ["", "## Removed: out of the 30-day window", ""]
    out += [f"- {iso}: {e['title']} (`{e['id']}`, last day {e.get('occurredUntil') or e['occurredOn']})" for iso, e in removed]
    out += ["", "## Held (not included)", ""]
    out += [f"- `{h['id']}` {h.get('title', '')}: {h['reason']}" for h in holds]
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    e = sub.add_parser("expire")
    e.add_argument("published", type=Path)
    e.add_argument("out", type=Path)
    e.add_argument("--date", help="UTC date to expire against (default today)")
    e.add_argument("--at", help="generatedAt for changed files (default now)")
    b = sub.add_parser("build")
    b.add_argument("folder", type=Path)
    b.add_argument("--published", type=Path, required=True)
    b.add_argument("--at", help="publication time (default now)")
    r = sub.add_parser("review")
    r.add_argument("edition", type=Path)
    r.add_argument("--published", type=Path, required=True)
    r.add_argument("--changes", type=Path)
    r.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if args.command == "expire":
        on = dt.date.fromisoformat(args.date) if args.date else dt.datetime.now(dt.timezone.utc).date()
        stamp = args.at or now_stamp()
        removed = cmd_expire(args.published, args.out, on, stamp)
        for iso, eid, last in removed:
            print(f"expired {iso} {eid} (last day {last})")
        print(f"{len(removed)} event(s) expired on {on}")
        return 0
    if args.command == "build":
        return cmd_build(args.folder, args.published, args.at or now_stamp())
    text = cmd_review(args.edition, args.published, args.changes)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"Wrote {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
