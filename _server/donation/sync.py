"""Refresh the read-only aggregate served to the donation widget."""
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

DEFAULT_ZONE = "Europe/Moscow"

def query_donations(token, settings):
    source_id = settings['data_source']
    type_field = settings['type_property']
    kind_field = settings['kind_property']
    base = f"https://api.notion.com/v1/data_sources/{source_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2025-09-03",
        "Content-Type": "application/json",
    }
    with urlopen(Request(base, headers=headers), timeout=15) as response:
        schema = {prop["id"]: prop for prop in json.load(response)["properties"].values()}
    if schema[settings['free_property']]["type"] != "checkbox":
        raise RuntimeError("Donation eligibility property is not a checkbox")
    types = {option["id"]: option["name"] for option in schema[type_field]["select"]["options"]}
    kinds = {option["id"]: option["name"] for option in schema[kind_field]["select"]["options"]}
    filters = {
        "page_size": 100,
        "filter": {
            "and": [
                {"property": type_field, "select": {"equals": types[settings['donation_option']]}},
                {"or": [
                    {"property": kind_field, "select": {"equals": kinds[kind]}}
                    for kind in (settings['blood_option'], settings['plasma_option'])
                ]},
            ],
        },
    }
    url = base + "/query"
    cursors = set()
    while True:
        request = Request(url, data=json.dumps(filters).encode(), headers=headers)
        with urlopen(request, timeout=15) as response:
            result = json.load(response)
        yield from result["results"]
        if not result["has_more"]:
            return
        cursor = result["next_cursor"]
        if not cursor or cursor in cursors:
            raise RuntimeError("Incomplete Notion pagination")
        cursors.add(cursor)
        filters["start_cursor"] = cursor

def aggregate(rows, now, settings, timezone=DEFAULT_ZONE):
    zone = ZoneInfo(timezone)
    blood, plasma = settings['blood_option'], settings['plasma_option']
    today = now.astimezone(zone).date()
    counts = {blood: 0, plasma: 0}
    latest = None
    latest_kind = None
    seen = set()
    for row in rows:
        if row.get("in_trash") or row.get("archived") or row["id"] in seen:
            continue
        seen.add(row["id"])
        properties = row["properties"]
        fields = {prop["id"]: prop for prop in properties.values()}
        kind = (fields[settings['kind_property']]["select"] or {}).get("id")
        period = fields[settings['date_property']]["date"]
        if kind not in counts or not period:
            continue
        # A date range records a known donation within that period. Use its last day.
        raw_date = period.get("end") or period["start"]
        if len(raw_date) == 10:
            donation_date = dt.date.fromisoformat(raw_date)
        else:
            donation_date = dt.datetime.fromisoformat(raw_date.replace('Z','+00:00')).astimezone(zone).date()
        if donation_date > today:
            continue
        if latest is None or donation_date > latest or (donation_date == latest and kind == blood):
            latest = donation_date
            latest_kind = "blood" if kind == blood else "plasma"
        if fields[settings['free_property']]["checkbox"]:
            counts[kind] += 1
    return {
        "blood": counts[blood],
        "plasma": counts[plasma],
        "lastDonation": latest.isoformat() if latest else None,
        "lastDonationKind": latest_kind,
        "timezone": timezone,
        "syncedAt": now.isoformat(timespec="seconds"),
    }

def save_snapshot(path, snapshot):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
        staged = Path(output.name)
        try:
            json.dump(snapshot, output, separators=(",", ":"))
            output.flush()
            os.fchmod(output.fileno(), 0o600)
            staged.replace(path)
        finally:
            staged.unlink(missing_ok=True)

def main():
    config = Path(os.environ["CREDENTIALS_DIRECTORY"]) / "notion-config"
    config = json.loads(config.read_text())
    settings = config['donation']
    donations = query_donations(config['notion_token'], settings)
    snapshot = aggregate(donations, dt.datetime.now(dt.timezone.utc), settings, config.get("tracker_timezone", DEFAULT_ZONE))
    save_snapshot(Path(os.environ["STATE_DIRECTORY"]) / "data.json", snapshot)

if __name__ == "__main__":
    try:
        main()
    except HTTPError as error:
        sys.exit(f"Notion query failed with HTTP {error.code}")
    except Exception as error:
        sys.exit(f"Donation refresh failed ({type(error).__name__})")
