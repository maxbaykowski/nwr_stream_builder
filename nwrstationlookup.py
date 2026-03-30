#!/usr/bin/env python3

from __future__ import annotations

import difflib
import json
import readline
import re
import sys
from pathlib import Path


DATA_PATH = Path(__file__).resolve().parent / "data" / "nwr_stations.json"
MAX_RESULTS = 25
CALLSIGN_RE = re.compile(r"^[a-z]{3}\d{2,3}$", re.IGNORECASE)


def normalize_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def prompt_menu_with_back(title: str, options: list[str], back_label: str) -> int:
    while True:
        print()
        print(title)
        print(f"0. {back_label}")
        for index, option in enumerate(options, start=1):
            print(f"{index}. {option}")

        selection = input("Select an option: ").strip()
        if not selection.isdigit():
            print("Enter the number for the option you want.")
            continue

        selected_index = int(selection)
        if selected_index == 0:
            return -1
        if 1 <= selected_index <= len(options):
            return selected_index - 1

        print("That selection is not available.")


def prompt_with_prefill(prompt: str, prefill: str) -> str:
    readline.set_startup_hook(lambda: readline.insert_text(prefill))
    try:
        return input(prompt)
    finally:
        readline.set_startup_hook(None)


def load_stations() -> list[dict[str, str]]:
    payload = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    return payload["stations"]


def station_menu_label(station: dict[str, str]) -> str:
    return f"{station['site_name']}, {station['state']} | {station['callsign']} | {station['frequency']} MHz"


def station_detail_lines(station: dict[str, str]) -> list[str]:
    return [
        f"Site Name: {station['site_name']}",
        f"State: {station['state']}",
        f"Call Sign: {station['callsign']}",
        f"Frequency: {station['frequency']} MHz",
    ]


def token_matches_value(token: str, value: str) -> bool:
    if not value:
        return False

    normalized_value = normalize_text(value)
    if not normalized_value:
        return False

    if token in normalized_value:
        return True

    value_tokens = normalized_value.split()
    return any(difflib.SequenceMatcher(None, token, candidate).ratio() >= 0.86 for candidate in value_tokens)


def query_tokens_match_station(tokens: list[str], station: dict[str, str]) -> bool:
    fields = [
        station["callsign"],
        station["state"],
        station["state_name"],
        station["city"],
        station["site_name"],
    ]
    return all(any(token_matches_value(token, field) for field in fields) for token in tokens)


def score_station(tokens: list[str], station: dict[str, str]) -> float:
    callsign = station["callsign"].lower()
    state = station["state"].lower()
    state_name = normalize_text(station["state_name"])
    city = normalize_text(station["city"])
    site_name = normalize_text(station["site_name"])
    searchable_text = " ".join((callsign, state, state_name, city, site_name))

    score = 0.0
    joined_query = " ".join(tokens)
    if joined_query == callsign:
        score += 1000
    if joined_query == state or joined_query == state_name:
        score += 700
    if joined_query == city or joined_query == site_name:
        score += 600
    if joined_query in city or joined_query in site_name:
        score += 250
    if joined_query in searchable_text:
        score += 120

    for token in tokens:
        if token == callsign:
            score += 400
        elif token == state or token == state_name:
            score += 250
        elif token in city or token in site_name:
            score += 220
        elif token in searchable_text:
            score += 140
        else:
            score += max(
                difflib.SequenceMatcher(None, token, callsign).ratio() * 80,
                difflib.SequenceMatcher(None, token, state).ratio() * 50,
                difflib.SequenceMatcher(None, token, state_name).ratio() * 60,
                difflib.SequenceMatcher(None, token, city).ratio() * 70,
                difflib.SequenceMatcher(None, token, site_name).ratio() * 70,
            )

    return score


def search_stations(query: str, stations: list[dict[str, str]]) -> list[dict[str, str]]:
    normalized_query = normalize_text(query)
    if not normalized_query:
        return []

    if CALLSIGN_RE.fullmatch(normalized_query):
        exact_callsign_matches = [station for station in stations if station["callsign"].lower() == normalized_query]
        if exact_callsign_matches:
            return exact_callsign_matches

    if len(normalized_query) == 2 and normalized_query.isalpha():
        exact_state_matches = [station for station in stations if station["state"].lower() == normalized_query]
        if exact_state_matches:
            return exact_state_matches

    tokens = normalized_query.split()
    scored_results = []
    for station in stations:
        if not query_tokens_match_station(tokens, station):
            continue
        score = score_station(tokens, station)
        if score >= 150:
            scored_results.append((score, station))

    scored_results.sort(
        key=lambda item: (
            -item[0],
            item[1]["state"],
            item[1]["callsign"],
            item[1]["site_name"],
        )
    )
    return [station for _, station in scored_results]


def show_station_details(station: dict[str, str]) -> None:
    print()
    for line in station_detail_lines(station):
        print(line)
    input("\nPress Enter to return to the results list...")


def show_search_results(results: list[dict[str, str]]) -> None:
    while True:
        visible_results = results[:MAX_RESULTS]
        print()
        if len(results) > MAX_RESULTS:
            print(f"{len(results)} results found. Showing top {MAX_RESULTS}.")
        else:
            print(f"{len(results)} results found.")

        selection = prompt_menu_with_back(
            "Station Results",
            [station_menu_label(station) for station in visible_results],
            "Back to search",
        )
        if selection == -1:
            return

        show_station_details(visible_results[selection])


def main() -> int:
    stations = load_stations()
    last_query = ""

    while True:
        print()
        try:
            query = prompt_with_prefill(
                "Search NOAA Weather Radio stations by state, city, site name, or callsign: ",
                last_query,
            ).strip()
        except EOFError:
            print()
            return 0
        if not query:
            print("Enter a search term.")
            continue

        last_query = query

        results = search_stations(query, stations)
        if not results:
            print("No results found.")
            continue

        show_search_results(results)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print()
        raise SystemExit(130)
