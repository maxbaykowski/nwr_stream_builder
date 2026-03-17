#!/usr/bin/env python3
"""Scrape NOAA Weather Radio station data into JSON."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


BASE_URL = "https://www.weather.gov"
STATIONS_URL = BASE_URL + "/nwr/stations?State={state}"
CCL_URL = BASE_URL + "/source/nwr/JS/CCL.js"
USER_AGENT = "nwr_stream_builder/0.1 (+https://www.weather.gov/nwr)"
PAGE_MARKER = "/source/nwr/JS/CCL.js"
REQUIRED_FIELDS = ("ST", "STATE", "SITENAME", "SITELOC", "SITESTATE", "FREQ", "CALLSIGN")
ASSIGNMENT_RE = re.compile(
    r'^(?P<field>[A-Z]+)\[(?P<index>\d+)\]\s*=\s*"(?P<value>(?:\\.|[^"])*)";$',
    re.MULTILINE,
)
US_STATES = (
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "FL",
    "GA",
    "HI",
    "ID",
    "IL",
    "IN",
    "IA",
    "KS",
    "KY",
    "LA",
    "ME",
    "MD",
    "MA",
    "MI",
    "MN",
    "MS",
    "MO",
    "MT",
    "NE",
    "NV",
    "NH",
    "NJ",
    "NM",
    "NY",
    "NC",
    "ND",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VT",
    "VA",
    "WA",
    "WV",
    "WI",
    "WY",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape NOAA Weather Radio station data into a JSON file."
    )
    parser.add_argument(
        "-o",
        "--output",
        default="data/nwr_stations.json",
        help="Path to the JSON output file (default: %(default)s).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="HTTP timeout in seconds (default: %(default)s).",
    )
    parser.add_argument(
        "--states",
        nargs="+",
        help="Optional list of 2-letter state abbreviations to scrape.",
    )
    return parser.parse_args()


def fetch_text(url: str, timeout: int) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def normalize_states(states: Iterable[str] | None) -> list[str]:
    if not states:
        return list(US_STATES)

    normalized = [state.strip().upper() for state in states if state.strip()]
    invalid = sorted(set(normalized) - set(US_STATES))
    if invalid:
        raise ValueError(f"Unsupported state abbreviations: {', '.join(invalid)}")
    return normalized


def validate_state_pages(states: Iterable[str], timeout: int) -> None:
    for state in states:
        url = STATIONS_URL.format(state=state)
        html = fetch_text(url, timeout)
        if PAGE_MARKER not in html:
            raise RuntimeError(f"Unexpected station page format for {state}: {url}")


def decode_js_string(raw_value: str) -> str:
    return json.loads(f'"{raw_value}"')


def parse_ccl(js_text: str) -> dict[int, dict[str, str]]:
    rows: dict[int, dict[str, str]] = defaultdict(dict)

    for match in ASSIGNMENT_RE.finditer(js_text):
        field = match.group("field")
        if field not in REQUIRED_FIELDS:
            continue
        index = int(match.group("index"))
        rows[index][field] = decode_js_string(match.group("value"))

    missing = [field for field in REQUIRED_FIELDS if not any(field in row for row in rows.values())]
    if missing:
        raise RuntimeError(f"Missing expected fields in CCL.js: {', '.join(missing)}")

    return rows


def build_state_name_map(rows: dict[int, dict[str, str]]) -> dict[str, str]:
    state_names: dict[str, str] = {}
    for row in rows.values():
        abbreviation = row.get("ST", "")
        state_name = row.get("STATE", "")
        if abbreviation and state_name and abbreviation not in state_names:
            state_names[abbreviation] = state_name
    return state_names


def build_station_list(
    rows: dict[int, dict[str, str]], states: Iterable[str], state_names: dict[str, str]
) -> list[dict[str, str]]:
    target_states = set(states)
    seen: set[tuple[str, str]] = set()
    stations: list[dict[str, str]] = []

    for index in sorted(rows):
        row = rows[index]
        state = row.get("SITESTATE", "")
        callsign = row.get("CALLSIGN", "")
        if state not in target_states or not callsign:
            continue

        key = (state, callsign)
        if key in seen:
            continue
        seen.add(key)

        stations.append(
            {
                "city": row.get("SITELOC") or row.get("SITENAME", ""),
                "state": state,
                "state_name": state_names.get(state, ""),
                "callsign": callsign,
                "frequency": row.get("FREQ", ""),
                "site_name": row.get("SITENAME", ""),
                "source_url": STATIONS_URL.format(state=state),
            }
        )

    return sorted(stations, key=lambda station: (station["state"], station["callsign"]))


def write_output(path: Path, stations: list[dict[str, str]], states: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "station_count": len(stations),
        "states_scraped": states,
        "source": {
            "station_pages": [STATIONS_URL.format(state=state) for state in states],
            "dataset": CCL_URL,
        },
        "stations": stations,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()

    try:
        states = normalize_states(args.states)
        validate_state_pages(states, args.timeout)
        ccl_text = fetch_text(CCL_URL, args.timeout)
        rows = parse_ccl(ccl_text)
        state_names = build_state_name_map(rows)
        stations = build_station_list(rows, states, state_names)
        write_output(Path(args.output), stations, states)
    except (HTTPError, URLError, TimeoutError) as exc:
        print(f"Network error: {exc}", file=sys.stderr)
        return 1
    except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Wrote {len(stations)} stations to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
