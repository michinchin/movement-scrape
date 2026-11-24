#!/usr/bin/env python3
"""Sync Movement Gyms class schedules to a Google Calendar.

How it works
  1. Opens each gym's portal calendar page in headless Chromium (Playwright).
  2. Captures the page's own GraphQL "StorefrontCalendarQuery" request (it holds the
     facility + plan IDs the gym uses) and replays it for the date range you want.
  3. Upserts the sessions into a dedicated Google Calendar. Event IDs are derived from
     the session ID, so re-running updates events in place instead of duplicating, and
     sessions that disappeared from the schedule are removed.

Usage
  python movement_to_gcal.py                       # rest of this month, default gyms
  python movement_to_gcal.py --dry-run             # print what would change
  python movement_to_gcal.py --gyms sunnyvale santa-clara mountain-view
  python movement_to_gcal.py --include-all         # don't filter out kids/lead checks/etc.
"""
import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path

PORTAL = "https://portal.movementgyms.com"
CAL_PATH = "n/climbing-yoga-fitness-calendar"
TZ = "America/Los_Angeles"
CALENDAR_NAME = "Movement Gyms"
DEFAULT_GYMS = ["sunnyvale", "santa-clara", "mountain-view"]

# Sessions matching this are skipped unless --include-all (not "gym activities" you'd put on a calendar).
EXCLUDE_RE = re.compile(
    r"lead check|kids|after school|book a tour|climbing party|birthday|camp|"
    r"book a climb|meet-up|partner project|youth|team",
    re.I,
)

HERE = Path(__file__).resolve().parent
TOKEN = HERE / "token.json"
CREDS = HERE / "credentials.json"  # OAuth desktop client from Google Cloud console
SCOPES = ["https://www.googleapis.com/auth/calendar"]


# ---------------------------------------------------------------- scraping
def fetch_gym(slug, start, end):
    """Return raw calendar rows for one gym between start and end (inclusive dates)."""
    from playwright.sync_api import sync_playwright

    url = f"{PORTAL}/{slug}/{CAL_PATH}"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        captured = {}

        def on_request(req):
            body = req.post_data or ""
            if "StorefrontCalendarQuery" in body and "body" not in captured:
                captured["body"] = json.loads(body)

        page.on("request", on_request)
        page.goto(url, wait_until="networkidle")
        if "body" not in captured:
            page.wait_for_timeout(4000)
        if "body" not in captured:
            raise RuntimeError(f"{slug}: calendar request not seen (bad slug or site changed?) -> {url}")
        body = captured["body"]
        body["variables"]["input"]["startDate"] = start.isoformat()
        body["variables"]["input"]["endDate"] = end.isoformat()
        result = page.evaluate(
            """async (b) => {
                const r = await fetch('/graphql-public', {method:'POST',
                  headers:{'content-type':'application/json'}, credentials:'include',
                  body: JSON.stringify(b)});
                return await r.json();
            }""",
            body,
        )
        browser.close()
    if result.get("errors"):
        raise RuntimeError(f"{slug}: {result['errors'][0].get('message')}")
    return result["data"]["calendar"]


# ---------------------------------------------------------------- transform
def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&mdash;", "-")
    return re.sub(r"\s+", " ", s).strip()


def pretty_gym(slug):
    return slug.replace("-", " ").title()


def to_event(slug, row):
    start = dt.datetime.fromisoformat(row["startLocal"])
    end = dt.datetime.fromisoformat(row["endLocal"]) + dt.timedelta(seconds=1)  # 17:59:59 -> 18:00
    gym = pretty_gym(slug)
    lines = []
    if row.get("instructorText"):
        lines.append(f"Instructor: {row['instructorText']}")
    if row.get("capacityText"):
        lines.append(f"Availability at sync time: {row['capacityText']}")
    summary = strip_html(row.get("shortSummary"))
    if summary:
        lines.append(summary[:500])
    lines.append(f"Book: {PORTAL}/{slug}/{CAL_PATH}")
    key = f"{slug}:{row['sessionGraphId']}"
    return {
        "id": "mvt" + hashlib.sha1(key.encode()).hexdigest(),  # base32hex-safe, stable
        "summary": f"{row['publicTitle']} ({gym})",
        "location": f"Movement {gym}",
        "description": "\n".join(lines),
        "start": {"dateTime": start.isoformat(), "timeZone": TZ},
        "end": {"dateTime": end.isoformat(), "timeZone": TZ},
        "extendedProperties": {"private": {"source": "movement-sync", "gym": slug}},
    }


# ---------------------------------------------------------------- .ics feed (no Google login needed)
def _ics_escape(s):
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def write_ics(events, path):
    """Write a subscribable iCalendar feed. Floating local times + X-WR-TIMEZONE keep it simple."""
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fmt = lambda s: dt.datetime.fromisoformat(s).strftime("%Y%m%dT%H%M%S")
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//movement-sync//EN", "CALSCALE:GREGORIAN",
           f"X-WR-CALNAME:{CALENDAR_NAME}", f"X-WR-TIMEZONE:{TZ}", "REFRESH-INTERVAL;VALUE=DURATION:PT12H"]
    for e in events:
        out += ["BEGIN:VEVENT", f"UID:{e['id']}@movement-sync", f"DTSTAMP:{stamp}",
                f"DTSTART;TZID={TZ}:{fmt(e['start']['dateTime'])}",
                f"DTEND;TZID={TZ}:{fmt(e['end']['dateTime'])}",
                f"SUMMARY:{_ics_escape(e['summary'])}", f"LOCATION:{_ics_escape(e['location'])}",
                f"DESCRIPTION:{_ics_escape(e['description'])}", "END:VEVENT"]
    out.append("END:VCALENDAR")
    Path(path).write_text("\r\n".join(out) + "\r\n")
    print(f"wrote {len(events)} events to {path}")


# ---------------------------------------------------------------- google calendar
def gcal_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_file(TOKEN, SCOPES) if TOKEN.exists() else None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDS.exists():
                sys.exit(f"Missing {CREDS}. See README.md step 2.")
            creds = InstalledAppFlow.from_client_secrets_file(CREDS, SCOPES).run_local_server(port=0)
        TOKEN.write_text(creds.to_json())
    return build("calendar", "v3", credentials=creds)


def get_or_create_calendar(svc):
    page = None
    while True:
        r = svc.calendarList().list(pageToken=page).execute()
        for c in r["items"]:
            if c.get("summary") == CALENDAR_NAME:
                return c["id"]
        page = r.get("nextPageToken")
        if not page:
            break
    return svc.calendars().insert(body={"summary": CALENDAR_NAME, "timeZone": TZ}).execute()["id"]


def existing_synced(svc, cal_id, start, end):
    out, page = {}, None
    while True:
        r = svc.events().list(
            calendarId=cal_id, pageToken=page, showDeleted=False, singleEvents=True,
            timeMin=f"{start}T00:00:00-12:00", timeMax=f"{end + dt.timedelta(days=1)}T00:00:00+14:00",
            privateExtendedProperty="source=movement-sync", maxResults=2500,
        ).execute()
        for e in r["items"]:
            out[e["id"]] = e
        page = r.get("nextPageToken")
        if not page:
            return out


def sync(svc, cal_id, events, scanned_gyms, start, end, dry):
    from googleapiclient.errors import HttpError

    have = existing_synced(svc, cal_id, start, end)
    want = {e["id"]: e for e in events}
    added = updated = removed = 0
    for eid, ev in want.items():
        if eid in have:
            old = have[eid]
            same = (old.get("summary") == ev["summary"] and old.get("description", "") == ev["description"]
                    and old["start"].get("dateTime", "")[:16] == ev["start"]["dateTime"][:16]
                    and old["end"].get("dateTime", "")[:16] == ev["end"]["dateTime"][:16])
            if not same:
                updated += 1
                if not dry:
                    svc.events().update(calendarId=cal_id, eventId=eid, body=ev).execute()
        else:
            added += 1
            if not dry:
                try:
                    svc.events().insert(calendarId=cal_id, body=ev).execute()
                except HttpError as e:
                    if e.resp.status == 409:  # previously deleted event with same id
                        svc.events().update(calendarId=cal_id, eventId=eid, body=ev).execute()
                    else:
                        raise
    for eid, old in have.items():
        gym = old.get("extendedProperties", {}).get("private", {}).get("gym")
        if eid not in want and gym in scanned_gyms:  # only prune gyms we actually scraped
            removed += 1
            if not dry:
                svc.events().delete(calendarId=cal_id, eventId=eid).execute()
    print(f"{'[dry-run] ' if dry else ''}added {added}, updated {updated}, removed {removed}")


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gyms", nargs="+", default=DEFAULT_GYMS, help="portal slugs")
    ap.add_argument("--start", type=dt.date.fromisoformat, default=dt.date.today())
    ap.add_argument("--end", type=dt.date.fromisoformat,
                    help="default: last day of the start month")
    ap.add_argument("--include-all", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--ics", metavar="FILE", help="write a subscribable .ics feed instead of using the Google API")
    ap.add_argument("--from-json", help="test mode: read rows from this JSON file instead of scraping")
    a = ap.parse_args()
    if not a.end:
        nxt = (a.start.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        a.end = nxt - dt.timedelta(days=1)

    events = []
    for slug in a.gyms:
        rows = json.load(open(a.from_json)) if a.from_json else fetch_gym(slug, a.start, a.end)
        kept = [r for r in rows if a.include_all or not EXCLUDE_RE.search(r["publicTitle"])]
        print(f"{slug}: {len(rows)} sessions, {len(kept)} kept")
        events += [to_event(slug, r) for r in kept]

    if a.ics:
        write_ics(sorted(events, key=lambda e: e["start"]["dateTime"]), a.ics)
        return
    if a.dry_run and a.from_json:
        for e in events[:10]:
            print(e["start"]["dateTime"], "->", e["end"]["dateTime"][11:16], e["summary"])
        return
    svc = gcal_service()
    cal_id = get_or_create_calendar(svc)
    sync(svc, cal_id, events, set(a.gyms), a.start, a.end, a.dry_run)


if __name__ == "__main__":
    main()
