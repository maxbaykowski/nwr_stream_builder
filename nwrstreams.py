#!/usr/bin/env python3

from __future__ import annotations

import base64
import http.client
import json
from difflib import SequenceMatcher
import grp
import os
import pwd
import readline
import re
import select
import shutil
import socket
import ssl
import subprocess
import sys
import termios
import time
import tty
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse


DEPENDENCIES = {
    "rtl-sdr": ("rtl_sdr", "rtl_test", "rtl_eeprom"),
    "multimon-ng": ("multimon-ng",),
    "sox": ("sox",),
    "ffmpeg": ("ffmpeg",),
    "liquidsoap": ("liquidsoap",),
    "sdr_server": ("sdr_server",),
    "sdr_server_client": ("sdr_server_client",),
}

KNOWN_RTL_USB_IDS = {
    "0bda:2832",
    "0bda:2838",
}
IQBUS_USER = "iqbus"
IQBUS_GROUP = "plugdev"
STREAM_GROUP = "broadcasters"
IQBUS_CONFIG_PATH = Path("/etc/iqbus.config")
IQBUS_SERVICE_PATH = Path("/etc/systemd/system/iqbus.service")
STREAM_SERVICE_DIR = Path("/etc/systemd/system")
LIQUIDSOAP_BIN_DIR = Path("/usr/local/bin")
FALLBACK_TARGET_DIR = Path("/usr/local/share/nwr")
FALLBACK_TARGET_PATH = FALLBACK_TARGET_DIR / "fallback.wav"
EAS_SCRIPT_TARGET_PATH = LIQUIDSOAP_BIN_DIR / "easrecorder.py"
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATION_DATA_PATH = BASE_DIR / "data" / "nwr_stations.json"
EAS_SCRIPT_SOURCE_PATH = BASE_DIR / "scripts" / "easrecorder.py"
FALLBACK_SOURCE_PATH = BASE_DIR / "audio" / "fallback.wav"
LOW_SAMPLE_RATE_MIN = 225001
LOW_SAMPLE_RATE_MAX = 300000
HIGH_SAMPLE_RATE_MIN = 900001
HIGH_SAMPLE_RATE_MAX = 3200000
USB_WARNING_SAMPLE_RATE = 2560000
MIN_MANUAL_GAIN = 0.0
MAX_MANUAL_GAIN = 49.6
MIN_PPM = -100
MAX_PPM = 100
IQBUS_FAILED_MESSAGE = "Error: SDR Server failed to start!"
PASSWORD_TOGGLE_CHAR = "\x13"
GWES_SUBMISSION_URL = "https://forms.office.com/r/MLx6hKmnCe"
WEATHER_USA_SUBMISSION_URL = "https://www.weatherusa.net/members/services/radio"
NOAA_RADIO_ORG_LOOKUP_URL = "https://noaaweatherradio.org/N2radio-finder.php"
NOAA_RADIO_ORG_SUBMISSION_URL = "http://noaaweatherradio.org/addstream/addstream.html"


@dataclass
class IcecastOutput:
    server: str
    port: str
    username: str
    password: str
    mountpoint: str
    bitrate: str


@dataclass
class ParsedIcecastOutput:
    output: IcecastOutput
    start: int
    end: int


@dataclass
class RtlDevice:
    index: int
    description: str
    usb_id: str | None = None
    serial: str | None = None

    def label(self) -> str:
        details = [f"RTL-SDR #{self.index}", self.description]
        if self.usb_id:
            details.append(f"USB {self.usb_id}")
        if self.serial:
            details.append(f"SN {self.serial}")
        return " | ".join(details)


class SetupError(RuntimeError):
    pass


def missing_dependencies() -> list[str]:
    missing = []
    for package_name, commands in DEPENDENCIES.items():
        if not any(shutil.which(command) for command in commands):
            missing.append(package_name)
    return missing


def run_command(command: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=check,
        )
    except FileNotFoundError as error:
        raise SetupError(f"Required command not found: {command[0]}") from error
    except subprocess.CalledProcessError as error:
        output = "\n".join(part.strip() for part in (error.stdout, error.stderr) if part.strip())
        message = f"Command failed: {' '.join(command)}"
        if output:
            message = f"{message}\n{output}"
        raise SetupError(message) from error


def prompt_menu(title: str, options: list[str]) -> int:
    while True:
        print()
        print(title)
        for index, option in enumerate(options, start=1):
            print(f"{index}. {option}")

        selection = input("Select an option: ").strip()
        if not selection.isdigit():
            print("Enter the number for the option you want.")
            continue

        selected_index = int(selection)
        if 1 <= selected_index <= len(options):
            return selected_index - 1

        print("That selection is not available.")


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


def parse_keypress() -> str:
    char = os.read(sys.stdin.fileno(), 1).decode("utf-8", errors="ignore")
    if char != "\x1b":
        return char

    if not select.select([sys.stdin], [], [], 0.1)[0]:
        return char

    sequence = char + os.read(sys.stdin.fileno(), 1).decode("utf-8", errors="ignore")
    if sequence in ("\x1b[", "\x1bO") and select.select([sys.stdin], [], [], 0.1)[0]:
        sequence += os.read(sys.stdin.fileno(), 1).decode("utf-8", errors="ignore")
    if sequence.startswith("\x1b[") and sequence[-1:].isdigit() and select.select([sys.stdin], [], [], 0.1)[0]:
        sequence += os.read(sys.stdin.fileno(), 1).decode("utf-8", errors="ignore")

    normalized_sequences = {
        "\x1bOA": "\x1b[A",
        "\x1bOB": "\x1b[B",
        "\x1bOC": "\x1b[C",
        "\x1bOD": "\x1b[D",
        "\x1bOH": "\x1b[H",
        "\x1bOF": "\x1b[F",
    }
    return normalized_sequences.get(sequence, sequence)


def render_text_prompt(prompt: str, buffer: list[str], cursor: int, visible: bool = True) -> None:
    display = "".join(buffer) if visible else "*" * len(buffer)
    sys.stdout.write("\r")
    sys.stdout.write("\033[2K")
    sys.stdout.write(f"{prompt}{display}")
    cursor_back = len(display) - cursor
    if cursor_back > 0:
        sys.stdout.write(f"\033[{cursor_back}D")
    sys.stdout.flush()


def prompt_text(prompt: str, prefill: str, hidden: bool = False, allow_toggle: bool = False) -> str:
    if not sys.stdin.isatty():
        return input(prompt)

    if allow_toggle:
        sys.stdout.write("\nPress Ctrl+S to show or hide the password.\n")
        sys.stdout.flush()

    buffer = list(prefill)
    cursor = len(buffer)
    visible = not hidden
    file_descriptor = sys.stdin.fileno()
    original_settings = termios.tcgetattr(file_descriptor)

    try:
        tty.setraw(file_descriptor)
        render_text_prompt(prompt, buffer, cursor, visible)
        while True:
            key = parse_keypress()
            if key in ("\r", "\n"):
                sys.stdout.write("\n")
                sys.stdout.flush()
                return "".join(buffer)
            if allow_toggle and key == PASSWORD_TOGGLE_CHAR:
                visible = not visible
                render_text_prompt(prompt, buffer, cursor, visible)
                continue
            if key == "\x03":
                raise KeyboardInterrupt
            if key in ("\x7f", "\b"):
                if cursor > 0:
                    del buffer[cursor - 1]
                    cursor -= 1
                    render_text_prompt(prompt, buffer, cursor, visible)
                continue
            if key == "\x1b[D":
                if cursor > 0:
                    cursor -= 1
                    render_text_prompt(prompt, buffer, cursor, visible)
                continue
            if key == "\x1b[C":
                if cursor < len(buffer):
                    cursor += 1
                    render_text_prompt(prompt, buffer, cursor, visible)
                continue
            if key == "\x1b[H":
                cursor = 0
                render_text_prompt(prompt, buffer, cursor, visible)
                continue
            if key == "\x1b[F":
                cursor = len(buffer)
                render_text_prompt(prompt, buffer, cursor, visible)
                continue
            if key == "\x1b[3~":
                if cursor < len(buffer):
                    del buffer[cursor]
                    render_text_prompt(prompt, buffer, cursor, visible)
                continue
            if key.isprintable():
                buffer.insert(cursor, key)
                cursor += 1
                render_text_prompt(prompt, buffer, cursor, visible)
    finally:
        termios.tcsetattr(file_descriptor, termios.TCSADRAIN, original_settings)


def normalize_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def default_manual_output() -> IcecastOutput:
    return IcecastOutput(
        server="http://127.0.0.1",
        port="8000",
        username="source",
        password="hackme",
        mountpoint="",
        bitrate="64",
    )


def default_gwes_output() -> IcecastOutput:
    return IcecastOutput(
        server="http://ingest.wxr.gwes-cdn.net",
        port="10000",
        username="",
        password="",
        mountpoint="",
        bitrate="64",
    )


def default_weather_usa_output() -> IcecastOutput:
    return IcecastOutput(
        server="http://radio-master.weatherusa.net",
        port="80",
        username="source",
        password="",
        mountpoint="",
        bitrate="32",
    )


def default_noaa_radio_org_output(station: dict[str, str]) -> IcecastOutput:
    return IcecastOutput(
        server="http://wxradio.org",
        port="8000",
        username="source",
        password="WxRadio2014",
        mountpoint=noaa_radio_org_mountpoint(station, 0),
        bitrate="32",
    )


def default_noaa_radio_org_custom_output() -> IcecastOutput:
    return IcecastOutput(
        server="http://wxradio.org",
        port="8000",
        username="source",
        password="WxRadio2014",
        mountpoint="",
        bitrate="32",
    )


def normalize_server_url(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if value.startswith(("http://", "https://")):
        return value
    return f"http://{value}"


def normalize_mountpoint(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if value.startswith("/"):
        return value
    return f"/{value}"


def output_host(output: IcecastOutput) -> str:
    parsed = urlparse(output.server)
    return parsed.hostname or ""


def normalize_site_name_for_mount(site_name: str) -> str:
    tokens = re.findall(r"[A-Za-z0-9]+", site_name)
    return "".join(token[:1].upper() + token[1:] for token in tokens)


def noaa_radio_org_mountpoint(station: dict[str, str], slot: int) -> str:
    base = f"{station['state']}-{normalize_site_name_for_mount(station['site_name'])}-{station['callsign']}"
    if slot <= 0:
        return f"/{base}"
    return f"/{base}-alt{slot}"


def noaa_radio_org_mountpoint_slot(station: dict[str, str], mountpoint: str) -> int | None:
    normalized_mountpoint = normalize_mountpoint(mountpoint)
    base = noaa_radio_org_mountpoint(station, 0)
    if normalized_mountpoint == base:
        return 0
    match = re.fullmatch(rf"{re.escape(base)}-alt([1-5])", normalized_mountpoint)
    if match:
        return int(match.group(1))
    return None


def output_provider(output: IcecastOutput, station: dict[str, str] | None = None) -> str:
    host = output_host(output).lower()
    if host == "ingest.wxr.gwes-cdn.net" and output.port == "10000":
        return "gwes"
    if host == "radio-master.weatherusa.net" and output.port == "80" and output.username == "source":
        return "weatherusa"
    if (
        station is not None
        and host == "wxradio.org"
        and output.port == "8000"
        and output.username == "source"
        and output.password == "WxRadio2014"
        and output.bitrate == "32"
        and noaa_radio_org_mountpoint_slot(station, output.mountpoint) is not None
    ):
        return "noaa_radio_org"
    return "custom"


def provider_hosts() -> set[str]:
    return {"ingest.wxr.gwes-cdn.net", "radio-master.weatherusa.net", "wxradio.org"}


def bitrate_limits_for_output(output: IcecastOutput) -> tuple[int, int]:
    host = output_host(output).lower()
    if host == "ingest.wxr.gwes-cdn.net":
        return (64, 128)
    if host == "radio-master.weatherusa.net":
        return (16, 56)
    return (16, 320)


def prompt_output_bitrate(output: IcecastOutput) -> str:
    minimum, maximum = bitrate_limits_for_output(output)
    while True:
        value = prompt_text("Bitrate (kbps): ", output.bitrate).strip()
        if not value:
            print("Enter the MP3 bitrate.")
            continue
        if not value.isdigit():
            print("Enter the bitrate as a whole number.")
            continue
        bitrate = int(value)
        if bitrate % 8 != 0:
            print("The MP3 bitrate must be a multiple of 8.")
            continue
        if not minimum <= bitrate <= maximum:
            print(f"Bitrate must be between {minimum} and {maximum} kbps for this platform.")
            continue
        return str(bitrate)


def load_station_catalog() -> list[dict[str, str]]:
    payload = json.loads(STATION_DATA_PATH.read_text(encoding="utf-8"))
    return payload["stations"]


def get_station_by_callsign(callsign: str) -> dict[str, str] | None:
    for station in load_station_catalog():
        if station["callsign"].lower() == callsign.lower():
            return station
    return None


def station_menu_label(station: dict[str, str]) -> str:
    return f"{station['site_name']}, {station['state']} | {station['callsign']} | {station['frequency']} MHz"


def token_matches_value(token: str, value: str) -> bool:
    if not value:
        return False

    normalized_value = normalize_text(value)
    if not normalized_value:
        return False
    if token in normalized_value:
        return True

    return any(
        SequenceMatcher(None, token, candidate).ratio() >= 0.86
        for candidate in normalized_value.split()
    )


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
                SequenceMatcher(None, token, callsign).ratio() * 80,
                SequenceMatcher(None, token, state).ratio() * 50,
                SequenceMatcher(None, token, state_name).ratio() * 60,
                SequenceMatcher(None, token, city).ratio() * 70,
                SequenceMatcher(None, token, site_name).ratio() * 70,
            )

    return score


def search_stations(query: str, stations: list[dict[str, str]]) -> list[dict[str, str]]:
    normalized_query = normalize_text(query)
    if not normalized_query:
        return []

    if re.fullmatch(r"[a-z]{3}\d{2,3}", normalized_query):
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
        key=lambda item: (-item[0], item[1]["state"], item[1]["callsign"], item[1]["site_name"])
    )
    return [station for _, station in scored_results]


def prompt_station_selection() -> dict[str, str] | None:
    stations = load_station_catalog()
    last_query = ""

    while True:
        print()
        query = prompt_with_prefill(
            "Search NOAA Weather Radio stations by state, city, site name, or callsign: ",
            last_query,
        ).strip()
        if not query:
            print("Enter a search term.")
            continue

        last_query = query
        results = search_stations(query, stations)
        if not results:
            print("No results found.")
            continue

        visible_results = results[:25]
        print()
        if len(results) > 25:
            print(f"{len(results)} results found. Showing top 25.")
        else:
            print(f"{len(results)} results found.")

        selection = prompt_menu_with_back(
            "Station Results",
            [station_menu_label(station) for station in visible_results],
            "Back to stream setup",
        )
        if selection == -1:
            return None
        return visible_results[selection]


def load_template(name: str) -> str:
    template_path = TEMPLATES_DIR / name
    try:
        return template_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise SetupError(f"Template not found: {template_path}") from error


def update_config_value(config_text: str, key: str, value: str) -> str:
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    replacement = f'{key}={value}'
    if pattern.search(config_text):
        return pattern.sub(replacement, config_text, count=1)
    return f"{config_text.rstrip()}\n{replacement}\n"


def detect_usb_ids() -> list[str]:
    lsusb = shutil.which("lsusb")
    if not lsusb:
        return []

    result = run_command([lsusb], check=False)
    usb_ids = []
    for line in result.stdout.splitlines():
        match = re.search(r"\bID ([0-9a-fA-F]{4}:[0-9a-fA-F]{4})\b", line)
        if not match:
            continue

        usb_id = match.group(1).lower()
        if usb_id in KNOWN_RTL_USB_IDS:
            usb_ids.append(usb_id)

    return usb_ids


def read_device_serial(index: int) -> str | None:
    rtl_eeprom = shutil.which("rtl_eeprom")
    if not rtl_eeprom:
        return None

    result = run_command([rtl_eeprom, "-d", str(index)], check=False)
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)

    serial_patterns = (
        r"Serial number:\s*(.+)",
        r"Serial number string:\s*(.+)",
        r"serial:\s*(.+)",
    )
    for pattern in serial_patterns:
        match = re.search(pattern, output, re.IGNORECASE)
        if match:
            serial = match.group(1).strip().strip('"')
            if serial:
                return serial

    return None


def list_rtl_devices() -> list[RtlDevice]:
    rtl_test = shutil.which("rtl_test")
    if not rtl_test:
        raise SetupError("rtl_test was not found, so RTL-SDR devices cannot be detected.")

    result = run_command([rtl_test, "-t"], check=False)
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)

    if "No supported devices found." in output:
        return []

    devices = []
    for line in output.splitlines():
        match = re.match(r"^\s*(\d+):\s*(.+?)\s*$", line)
        if match:
            devices.append(
                RtlDevice(
                    index=int(match.group(1)),
                    description=match.group(2),
                )
            )

    usb_ids = detect_usb_ids()
    if len(usb_ids) == len(devices):
        for device, usb_id in zip(devices, usb_ids):
            device.usb_id = usb_id

    for device in devices:
        device.serial = read_device_serial(device.index)

    return devices


def ensure_root() -> None:
    if os.geteuid() != 0:
        raise SetupError("You must run this program as root.")


def ensure_group_exists(group_name: str) -> None:
    try:
        grp.getgrnam(group_name)
    except KeyError:
        run_command(["groupadd", "--system", group_name])


def ensure_user_exists(username: str) -> None:
    try:
        pwd.getpwnam(username)
    except KeyError:
        run_command(
            [
                "useradd",
                "--system",
                "--create-home",
                "--home-dir",
                f"/var/lib/{username}",
                "--shell",
                "/usr/sbin/nologin",
                username,
            ]
        )


def ensure_user_in_group(username: str, group_name: str) -> None:
    user_info = pwd.getpwnam(username)
    current_groups = {group.gr_name for group in grp.getgrall() if username in group.gr_mem}
    primary_group = grp.getgrgid(user_info.pw_gid).gr_name
    current_groups.add(primary_group)

    if group_name not in current_groups:
        run_command(["usermod", "-a", "-G", group_name, username])


def build_iqbus_config(device: RtlDevice) -> str:
    config_text = load_template("iqbus.config.template")
    config_text = update_config_value(config_text, "device_index", str(device.index))

    if device.serial:
        config_text = update_config_value(config_text, "device_serial", f'"{device.serial}"')
    else:
        config_text = update_config_value(config_text, "device_serial", '""')

    return config_text


def build_iqbus_service() -> str:
    service_text = load_template("iqbus.service")
    service_text = service_text.replace("user=", "User=")
    service_text = service_text.replace("ExecStart= sdr_server", "ExecStart=sdr_server")
    return service_text


def write_iqbus_config(config_text: str) -> None:
    IQBUS_CONFIG_PATH.write_text(config_text, encoding="utf-8")
    iqbus_user = pwd.getpwnam(IQBUS_USER)
    os.chown(IQBUS_CONFIG_PATH, 0, iqbus_user.pw_gid)
    os.chmod(IQBUS_CONFIG_PATH, 0o640)


def write_iqbus_service(service_text: str) -> None:
    IQBUS_SERVICE_PATH.write_text(service_text, encoding="utf-8")
    os.chown(IQBUS_SERVICE_PATH, 0, 0)
    os.chmod(IQBUS_SERVICE_PATH, 0o644)


def reload_and_enable_service() -> None:
    run_command(["systemctl", "daemon-reload"])
    run_command(["systemctl", "enable", "--now", "iqbus.service"])


def get_iqbus_active_state() -> str:
    result = run_command(["systemctl", "is-active", "iqbus.service"], check=False)
    return result.stdout.strip()


def iqbus_has_failed() -> bool:
    return get_iqbus_active_state() == "failed"


def iqbus_is_running() -> bool:
    return get_iqbus_active_state() == "active"


def report_failed_iqbus_service() -> bool:
    if iqbus_has_failed():
        print()
        print(IQBUS_FAILED_MESSAGE)
        return True
    return False


def iqbus_starts_on_boot() -> bool:
    result = run_command(["systemctl", "is-enabled", "iqbus.service"], check=False)
    return result.stdout.strip() == "enabled"


def wait_for_iqbus_state(target_state: str, timeout_seconds: float = 10.0) -> str:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        state = get_iqbus_active_state()
        if state in {target_state, "failed"}:
            return state
        time.sleep(0.2)
    return get_iqbus_active_state()


def get_service_active_state(service_name: str) -> str:
    result = run_command(["systemctl", "is-active", service_name], check=False)
    return result.stdout.strip()


def service_has_failed(service_name: str) -> bool:
    return get_service_active_state(service_name) == "failed"


def service_is_running(service_name: str) -> bool:
    return get_service_active_state(service_name) == "active"


def service_starts_on_boot(service_name: str) -> bool:
    result = run_command(["systemctl", "is-enabled", service_name], check=False)
    return result.stdout.strip() == "enabled"


def wait_for_service_state(service_name: str, target_state: str, timeout_seconds: float = 10.0) -> str:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        state = get_service_active_state(service_name)
        if state in {target_state, "failed"}:
            return state
        time.sleep(0.2)
    return get_service_active_state(service_name)


def report_failed_stream_service(callsign_lower: str) -> bool:
    service_name = f"{callsign_lower}.service"
    if service_has_failed(service_name):
        print()
        print(f"Error: Stream {callsign_lower.upper()} failed to start!")
        return True
    return False


def start_stream_service(callsign_lower: str) -> bool:
    service_name = f"{callsign_lower}.service"
    start_result = run_command(["systemctl", "start", service_name], check=False)
    if start_result.returncode != 0:
        print()
        print(f"Error: Stream {callsign_lower.upper()} failed to start!")
        return False
    if wait_for_service_state(service_name, "active") == "failed":
        print()
        print(f"Error: Stream {callsign_lower.upper()} failed to start!")
        return False
    print()
    print(f"Stream {callsign_lower.upper()} started.")
    return True


def stop_stream_service(callsign_lower: str) -> None:
    service_name = f"{callsign_lower}.service"
    report_failed_stream_service(callsign_lower)
    run_command(["systemctl", "stop", service_name], check=False)
    if wait_for_service_state(service_name, "inactive") == "failed":
        print()
        print(f"Error: Stream {callsign_lower.upper()} failed to start!")
        return
    print()
    print(f"Stream {callsign_lower.upper()} stopped.")


def toggle_stream_start_on_boot(callsign_lower: str) -> None:
    service_name = f"{callsign_lower}.service"
    if service_starts_on_boot(service_name):
        run_command(["systemctl", "disable", service_name])
        print()
        print("Start stream on host boot is now off.")
        return

    run_command(["systemctl", "enable", service_name])
    print()
    print("Start stream on host boot is now on.")


def restart_iqbus_if_active() -> bool:
    if report_failed_iqbus_service():
        return False
    result = run_command(["systemctl", "is-active", "--quiet", "iqbus.service"], check=False)
    if result.returncode == 0:
        restart_result = run_command(["systemctl", "restart", "iqbus.service"], check=False)
        if restart_result.returncode != 0:
            print()
            print(IQBUS_FAILED_MESSAGE)
            return False
        if wait_for_iqbus_state("active") == "failed":
            report_failed_iqbus_service()
            return False
        return True
    return False


def restart_iqbus_if_not_inactive() -> bool:
    if report_failed_iqbus_service():
        return False
    if iqbus_is_running():
        restart_result = run_command(["systemctl", "restart", "iqbus.service"], check=False)
        if restart_result.returncode != 0:
            print()
            print(IQBUS_FAILED_MESSAGE)
            return False
        if wait_for_iqbus_state("active") == "failed":
            report_failed_iqbus_service()
            return False
        return True
    return False


def start_iqbus_service() -> bool:
    start_result = run_command(["systemctl", "start", "iqbus.service"], check=False)
    if start_result.returncode != 0:
        print()
        print(IQBUS_FAILED_MESSAGE)
        return False
    if wait_for_iqbus_state("active") == "failed":
        print()
        print(IQBUS_FAILED_MESSAGE)
        return False
    print()
    print("SDR server started.")
    return True


def stop_iqbus_service() -> None:
    report_failed_iqbus_service()
    run_command(["systemctl", "stop", "iqbus.service"])
    if wait_for_iqbus_state("inactive") == "failed":
        print()
        print(IQBUS_FAILED_MESSAGE)
        return
    print()
    print("SDR server stopped.")


def toggle_iqbus_start_on_boot() -> None:
    if iqbus_starts_on_boot():
        run_command(["systemctl", "disable", "iqbus.service"])
        print()
        print("Start server on host boot is now off.")
        return

    run_command(["systemctl", "enable", "iqbus.service"])
    print()
    print("Start server on host boot is now on.")


def read_iqbus_config() -> str:
    try:
        return IQBUS_CONFIG_PATH.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise SetupError(f"Existing config not found: {IQBUS_CONFIG_PATH}") from error


def get_config_value(config_text: str, key: str) -> str | None:
    match = re.search(rf"^{re.escape(key)}=(.+)$", config_text, re.MULTILINE)
    if match:
        return match.group(1).strip()
    return None


def ensure_iqbus_identity() -> None:
    ensure_group_exists(IQBUS_GROUP)
    ensure_user_exists(IQBUS_USER)
    ensure_user_in_group(IQBUS_USER, IQBUS_GROUP)


def write_config_and_restart(config_text: str) -> None:
    ensure_iqbus_identity()
    write_iqbus_config(config_text)
    restart_iqbus_if_active()


def gain_mode_is_hardware_agc(config_text: str) -> bool:
    return get_config_value(config_text, "gain_mode") == "0"


def is_valid_sample_rate(value: int) -> bool:
    return (
        LOW_SAMPLE_RATE_MIN <= value <= LOW_SAMPLE_RATE_MAX
        or HIGH_SAMPLE_RATE_MIN <= value <= HIGH_SAMPLE_RATE_MAX
    )


def prompt_for_sample_rate(current_value: int | None) -> int:
    while True:
        print()
        print("RTL Sample Rate")
        print("Controls the SDR band sampling rate used by sdr_server.")
        if current_value is not None:
            print(f"Current value: {current_value}")
        print(
            "Valid ranges: "
            f"{LOW_SAMPLE_RATE_MIN}-{LOW_SAMPLE_RATE_MAX} or "
            f"{HIGH_SAMPLE_RATE_MIN}-{HIGH_SAMPLE_RATE_MAX} samples/second."
        )

        prefill = str(current_value) if current_value is not None else ""
        value = prompt_with_prefill("Enter a new sample rate: ", prefill).strip()
        if not value.isdigit():
            print("Enter the sample rate as a whole number.")
            continue

        sample_rate = int(value)
        if is_valid_sample_rate(sample_rate):
            return sample_rate

        print("That sample rate is outside the supported RTL-SDR ranges.")


def confirm_high_sample_rate(sample_rate: int) -> bool:
    if sample_rate <= USB_WARNING_SAMPLE_RATE:
        return True

    while True:
        response = input(
            "Warning: going above 2560000 samples/second may result in dropped "
            "samples over USB. Are you sure? (Y/n): "
        ).strip().lower()
        if response in ("", "y", "yes"):
            return True
        if response in ("n", "no"):
            return False

        print("Enter y to continue or n to cancel.")


def configure_sample_rate() -> None:
    config_text = read_iqbus_config()
    current_value = get_config_value(config_text, "band_sampling_rate")
    current_sample_rate = int(current_value) if current_value and current_value.isdigit() else None

    sample_rate = prompt_for_sample_rate(current_sample_rate)
    if not confirm_high_sample_rate(sample_rate):
        print()
        print("Sample rate was not changed.")
        return

    updated_config = update_config_value(config_text, "band_sampling_rate", str(sample_rate))
    write_config_and_restart(updated_config)

    print()
    print(f"RTL sample rate set to {sample_rate}.")


def toggle_hardware_agc() -> None:
    config_text = read_iqbus_config()
    agc_enabled = gain_mode_is_hardware_agc(config_text)
    updated_config = update_config_value(config_text, "gain_mode", "1" if agc_enabled else "0")
    write_config_and_restart(updated_config)

    print()
    print(f"Hardware AGC is now {'on' if not agc_enabled else 'off'}.")


def prompt_for_manual_gain(current_value: float | None) -> float:
    while True:
        print()
        print("Manual Gain")
        print("Controls the tuner gain when hardware AGC is off.")
        if current_value is not None:
            print(f"Current value: {current_value:.1f} dB")
        print(f"Valid range: {MIN_MANUAL_GAIN:.1f}-{MAX_MANUAL_GAIN:.1f} dB.")

        prefill = f"{current_value:.1f}" if current_value is not None else ""
        value = prompt_with_prefill("Enter a new manual gain in dB: ", prefill).strip()
        try:
            gain = float(value)
        except ValueError:
            print("Enter the gain as a number, for example 45.0.")
            continue

        if MIN_MANUAL_GAIN <= gain <= MAX_MANUAL_GAIN:
            return gain

        print("That gain is outside the supported RTL-SDR range.")


def configure_manual_gain() -> None:
    config_text = read_iqbus_config()
    if gain_mode_is_hardware_agc(config_text):
        print()
        print("Manual gain is unavailable while Hardware AGC is on.")
        return

    current_value = get_config_value(config_text, "gain")
    try:
        current_gain = float(current_value) if current_value is not None else None
    except ValueError:
        current_gain = None

    gain = prompt_for_manual_gain(current_gain)
    updated_config = update_config_value(config_text, "gain", f"{gain:.1f}")
    write_config_and_restart(updated_config)

    print()
    print(f"Manual gain set to {gain:.1f} dB.")


def prompt_for_ppm(current_value: int | None) -> int:
    while True:
        print()
        print("PPM Correction")
        print("Adjusts tuner frequency correction for SDRs that drift off frequency.")
        if current_value is not None:
            print(f"Current value: {current_value} PPM")
        print(f"Valid range: {MIN_PPM} to {MAX_PPM} PPM.")

        prefill = str(current_value) if current_value is not None else ""
        value = prompt_with_prefill("Enter a new PPM correction: ", prefill).strip()
        try:
            ppm = int(value)
        except ValueError:
            print("Enter the PPM correction as a whole number.")
            continue

        if MIN_PPM <= ppm <= MAX_PPM:
            return ppm

        print("That PPM correction is outside the allowed range.")


def configure_ppm() -> None:
    config_text = read_iqbus_config()
    current_value = get_config_value(config_text, "ppm")
    try:
        current_ppm = int(current_value) if current_value is not None else None
    except ValueError:
        current_ppm = None

    ppm = prompt_for_ppm(current_ppm)
    updated_config = update_config_value(config_text, "ppm", str(ppm))
    write_config_and_restart(updated_config)

    print()
    print(f"PPM correction set to {ppm}.")


def can_bind_port_on_all_interfaces(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


def get_process_name_for_bound_port(port: int) -> str | None:
    ss = shutil.which("ss")
    if not ss:
        return None

    result = run_command([ss, "-ltnpH"], check=False)
    port_suffixes = (f"0.0.0.0:{port}", f"[::]:{port}", f"*:{port}")

    for line in result.stdout.splitlines():
        columns = line.split()
        if len(columns) < 4:
            continue

        local_address = columns[3]
        if local_address not in port_suffixes:
            continue

        process_match = re.search(r'users:\(\("([^"]+)"', line)
        if process_match:
            return process_match.group(1)

        return "another process"

    return None


def prompt_for_port(current_value: int | None) -> int:
    while True:
        print()
        print("Server Port")
        print("Controls the TCP port used by sdr_server on 0.0.0.0.")
        if current_value is not None:
            print(f"Current value: {current_value}")
        print("Valid range: 1024 to 65535.")

        prefill = str(current_value) if current_value is not None else ""
        value = prompt_with_prefill("Enter a new server port: ", prefill).strip()
        if not value.isdigit():
            print("Enter the port as a whole number.")
            continue

        port = int(value)
        if not 1024 <= port <= 65535:
            print("That port is outside the valid TCP port range.")
            continue

        if port != current_value and not can_bind_port_on_all_interfaces(port):
            process_name = get_process_name_for_bound_port(port)
            if process_name:
                print(f"Port {port} is already bound on 0.0.0.0 by {process_name} and cannot be used.")
            else:
                print(f"Port {port} is already bound on 0.0.0.0 and cannot be used.")
            continue

        return port


def configure_port() -> None:
    config_text = read_iqbus_config()
    current_value = get_config_value(config_text, "port")
    current_port = int(current_value) if current_value and current_value.isdigit() else None

    port = prompt_for_port(current_port)
    updated_config = update_config_value(config_text, "port", str(port))
    write_config_and_restart(updated_config)

    print()
    print(f"Server port set to {port}.")


def bias_tee_is_enabled(config_text: str) -> bool:
    return get_config_value(config_text, "bias_t") == "1"


def confirm_enable_bias_tee() -> bool:
    while True:
        response = input(
            "Warning: Bias Tee sends DC voltage up the antenna line. Enable it "
            "only to power active antennas or low noise amplifiers, and do not "
            "enable it for passive antennas such as telescoping antennas, "
            "dipole antennas, or J-pole antennas. Continue? (Y/n): "
        ).strip().lower()
        if response in ("", "y", "yes"):
            return True
        if response in ("n", "no"):
            return False

        print("Enter y to continue or n to cancel.")


def toggle_bias_tee() -> None:
    config_text = read_iqbus_config()
    enabled = bias_tee_is_enabled(config_text)

    if not enabled and not confirm_enable_bias_tee():
        print()
        print("Bias Tee was not changed.")
        return

    updated_config = update_config_value(config_text, "bias_t", "0" if enabled else "1")
    write_config_and_restart(updated_config)

    print()
    print(f"Bias Tee is now {'on' if not enabled else 'off'}.")


def restart_sdr_server() -> None:
    if restart_iqbus_if_not_inactive():
        print()
        print("SDR server restarted.")


def start_sdr_server() -> None:
    start_iqbus_service()


def stop_sdr_server() -> None:
    stop_iqbus_service()


def server_settings_menu() -> None:
    while True:
        config_text = read_iqbus_config()
        report_failed_iqbus_service()
        server_running = iqbus_is_running()
        start_on_boot = iqbus_starts_on_boot()
        current_sample_rate = get_config_value(config_text, "band_sampling_rate") or "unknown"
        agc_enabled = gain_mode_is_hardware_agc(config_text)
        current_port = get_config_value(config_text, "port") or "unknown"

        options = [
            "Stop SDR Server" if server_running else "Start SDR Server",
            f"Start Server On Host Boot: {'on' if start_on_boot else 'off'}",
        ]

        if server_running:
            options.append("Restart SDR Server")

        options.extend(
            [
                f"Server Port: {current_port} (TCP port the server listens on)",
                (
                    "RTL Sample Rate: "
                    f"{current_sample_rate} samples/second "
                    "(controls the SDR band sampling rate)"
                ),
                f"Hardware AGC: {'on' if agc_enabled else 'off'} (toggles tuner automatic gain control)",
            ]
        )
        if not agc_enabled:
            current_gain = get_config_value(config_text, "gain") or "unknown"
            options.append(f"Manual Gain: {current_gain} dB (sets tuner gain when Hardware AGC is off)")
        current_ppm = get_config_value(config_text, "ppm") or "unknown"
        options.append(f"PPM Correction: {current_ppm} PPM (adjusts tuner frequency correction)")
        bias_tee_enabled = bias_tee_is_enabled(config_text)
        options.append(
            f"Bias Tee: {'on' if bias_tee_enabled else 'off'} "
            "(supplies DC power for active antennas or LNAs)"
        )

        selection = prompt_menu_with_back(
            "Server Configuration",
            options,
            "Back to main menu",
        )

        if selection == -1:
            return

        if selection == 0:
            if server_running:
                stop_sdr_server()
            else:
                start_sdr_server()
        elif selection == 1:
            toggle_iqbus_start_on_boot()
        elif selection == 2 and server_running:
            restart_sdr_server()
        elif selection == (3 if server_running else 2):
            configure_port()
        elif selection == (4 if server_running else 3):
            configure_sample_rate()
        elif selection == (5 if server_running else 4):
            toggle_hardware_agc()
        elif selection == (6 if server_running else 5):
            if agc_enabled:
                configure_ppm()
            else:
                configure_manual_gain()
        elif selection == (7 if server_running else 6):
            if agc_enabled:
                toggle_bias_tee()
            else:
                configure_ppm()
        elif selection == (8 if server_running else 7):
            if not agc_enabled:
                toggle_bias_tee()


def configure_server() -> None:
    if IQBUS_CONFIG_PATH.exists():
        server_settings_menu()
        return

    devices = list_rtl_devices()
    if not devices:
        print("No RTL-SDR devices were detected.")
        return

    options = [device.label() for device in devices]
    selected_index = prompt_menu("Select the RTL-SDR device to use for the spectrum server", options)
    selected_device = devices[selected_index]

    ensure_group_exists(IQBUS_GROUP)
    ensure_user_exists(IQBUS_USER)
    ensure_user_in_group(IQBUS_USER, IQBUS_GROUP)

    write_iqbus_config(build_iqbus_config(selected_device))
    write_iqbus_service(build_iqbus_service())
    reload_and_enable_service()

    print()
    print("IQ bus server configuration complete.")
    print(f"Device: {selected_device.label()}")
    print(f"Config: {IQBUS_CONFIG_PATH}")
    print(f"Service: {IQBUS_SERVICE_PATH}")


def list_existing_streams() -> list[str]:
    if not STREAM_SERVICE_DIR.exists():
        return []

    streams = []
    for path in STREAM_SERVICE_DIR.glob("*.service"):
        if path.name == IQBUS_SERVICE_PATH.name:
            continue
        liq_path = LIQUIDSOAP_BIN_DIR / f"{path.stem}.liq"
        if liq_path.exists():
            streams.append(path.stem.upper())
    return sorted(set(streams))


def show_submission_notice(url: str) -> None:
    print()
    print(f"You must first submit a stream by visiting the following URL: {url}")
    print("Press Enter to enter credentials.")
    while True:
        if input() == "":
            return


def show_noaa_radio_org_lookup_notice() -> None:
    print()
    print(f"Use the NOAA Weather Radio Org station lookup tool to check for mountpoint conflicts: {NOAA_RADIO_ORG_LOOKUP_URL}")
    print("Press Enter to continue.")
    while True:
        if input() == "":
            return


def collect_used_mountpoints(hostname: str, exclude: tuple[str, str] | None = None) -> set[str]:
    mountpoints: set[str] = set()
    for callsign in list_existing_streams():
        for parsed_output in extract_liquidsoap_icecast_outputs(read_stream_config(callsign.lower())):
            if output_host(parsed_output.output).lower() != hostname.lower():
                continue
            key = (callsign.lower(), parsed_output.output.mountpoint)
            if exclude is not None and key == exclude:
                continue
            mountpoints.add(parsed_output.output.mountpoint)
    return mountpoints


def first_open_noaa_mountpoint_slot(station: dict[str, str], exclude: tuple[str, str] | None = None) -> int:
    used_mountpoints = collect_used_mountpoints("wxradio.org", exclude=exclude)
    for slot in range(0, 6):
        candidate = noaa_radio_org_mountpoint(station, slot)
        if candidate not in used_mountpoints:
            return slot
    return 0


def prompt_noaa_mountpoint(station: dict[str, str], current_mountpoint: str | None, exclude: tuple[str, str] | None = None) -> str:
    slot = noaa_radio_org_mountpoint_slot(station, current_mountpoint or "")
    if slot is None:
        slot = first_open_noaa_mountpoint_slot(station, exclude=exclude)

    print()
    print("Use the up and down arrow keys to choose a mountpoint slot. Press Enter to accept.")
    file_descriptor = sys.stdin.fileno()
    original_settings = termios.tcgetattr(file_descriptor)
    try:
        tty.setraw(file_descriptor)
        while True:
            mountpoint = noaa_radio_org_mountpoint(station, slot)
            render_text_prompt("Mountpoint: ", list(mountpoint), len(mountpoint), True)
            key = parse_keypress()
            if key in ("\r", "\n"):
                sys.stdout.write("\n")
                sys.stdout.flush()
                return mountpoint
            if key == "\x03":
                raise KeyboardInterrupt
            if key == "\x1b[A" and slot < 5:
                slot += 1
            elif key == "\x1b[B" and slot > 0:
                slot -= 1
    finally:
        termios.tcsetattr(file_descriptor, termios.TCSADRAIN, original_settings)


def prompt_output_port(current_value: str) -> str:
    while True:
        value = prompt_text("Port: ", current_value).strip()
        if not value:
            return ""
        if not value.isdigit():
            print("Enter the port as a whole number.")
            continue
        port = int(value)
        if not 1 <= port <= 65535:
            print("Port must be between 1 and 65535.")
            continue
        return str(port)


def prompt_custom_output_server(current_value: str) -> str:
    while True:
        value = normalize_server_url(prompt_text("Server: ", current_value).strip())
        if not value:
            return ""
        host = (urlparse(value).hostname or "").lower()
        if host in provider_hosts():
            print("Please choose the relevant streaming platform from the platform selection menu.")
            continue
        return value


def output_fields_complete(output: IcecastOutput) -> bool:
    return all(
        (
            output.server.strip(),
            output.port.strip(),
            output.username.strip(),
            output.password,
            output.mountpoint.strip(),
            output.bitrate.strip(),
        )
    )


def build_output_options(output: IcecastOutput, station: dict[str, str] | None = None) -> list[str]:
    provider = output_provider(output, station)
    options = []
    if provider == "custom":
        options.append(f"Server: {output.server or '(blank)'}")
        options.append(f"Port: {output.port or '(blank)'}")
        options.append(f"Username: {output.username or '(blank)'}")
    elif provider == "gwes":
        options.append(f"Username: {output.username or '(blank)'}")
    elif provider == "noaa_radio_org":
        options.append(f"Mountpoint: {output.mountpoint or '(blank)'}")
        if output_fields_complete(output):
            options.append("Confirm")
        return options
    else:
        pass
    options.extend(
        [
            f"Password: {'*' * len(output.password) if output.password else '(blank)'}",
            f"Mountpoint: {output.mountpoint or '(blank)'}",
            f"Bitrate: {output.bitrate or '(blank)'} kbps",
        ]
    )
    if output_fields_complete(output):
        options.append("Confirm")
    return options


def parse_output_target(output: IcecastOutput) -> tuple[str, int, str, bool]:
    parsed = urlparse(output.server)
    host = parsed.hostname
    if not host:
        raise ValueError("Server is invalid.")

    port = int(output.port)
    scheme = parsed.scheme or "http"
    use_https = scheme == "https"
    path_prefix = parsed.path.rstrip("/")
    target = f"{path_prefix}{output.mountpoint}" if path_prefix else output.mountpoint
    return host, port, target, use_https


def authenticate_output(output: IcecastOutput) -> tuple[bool, str]:
    try:
        host, port, target, use_https = parse_output_target(output)
    except ValueError as error:
        return False, str(error)

    auth_bytes = f"{output.username}:{output.password}".encode("utf-8")
    auth_header = base64.b64encode(auth_bytes).decode("ascii")
    headers = {
        "Authorization": f"Basic {auth_header}",
        "Content-Length": "0",
        "Content-Type": "audio/mpeg",
        "User-Agent": "nwr_stream_builder/stream-setup",
    }

    connection_class = http.client.HTTPSConnection if use_https else http.client.HTTPConnection
    kwargs = {"timeout": 10}
    if use_https:
        kwargs["context"] = ssl.create_default_context()

    connection = None
    response = None
    try:
        connection = connection_class(host, port, **kwargs)
        connection.request("SOURCE", target, body=b"", headers=headers)
        response = connection.getresponse()
    except OSError as error:
        return False, f"Connection failed: {error}"
    except http.client.HTTPException as error:
        return False, f"HTTP error: {error}"
    finally:
        try:
            if response is not None:
                response.close()
        except Exception:
            pass
        try:
            if connection is not None:
                connection.close()
        except Exception:
            pass

    if response.status == 200:
        return True, "Icecast source authentication succeeded."
    if response.status == 401:
        return False, "Authentication failed: HTTP 401 Unauthorized. Check the username, password, and mountpoint."
    if response.status == 403:
        return False, "Authentication failed: HTTP 403 Forbidden. Another source is already connected to that mountpoint."
    if response.status == 404:
        return False, "Authentication failed: HTTP 404 Not Found. Check the server address and mountpoint."
    return False, f"Authentication failed: HTTP {response.status} {response.reason}"


def prompt_output_credentials(
    output: IcecastOutput,
    station: dict[str, str] | None = None,
    exclude: tuple[str, str] | None = None,
) -> IcecastOutput | None:
    while True:
        print()
        print("Icecast Output")
        print("0. Back")
        options = build_output_options(output, station)
        for index, option in enumerate(options, start=1):
            print(f"{index}. {option}")

        selection = input("Select an option: ").strip()
        provider = output_provider(output, station)
        editable_fields: list[str] = []
        if provider == "custom":
            editable_fields.extend(["server", "port", "username"])
        elif provider == "gwes":
            editable_fields.append("username")
        elif provider == "noaa_radio_org":
            editable_fields.extend(["mountpoint"])
        editable_fields.extend(["password", "mountpoint", "bitrate"])
        if provider == "noaa_radio_org":
            editable_fields = ["mountpoint"]
        confirm_index = len(editable_fields) + 1 if output_fields_complete(output) else None

        if not selection:
            if confirm_index is None:
                print("One or more fields are left blank.")
                continue
            success, message = authenticate_output(output)
            print()
            print(message)
            if success:
                if provider == "noaa_radio_org":
                    show_submission_notice(NOAA_RADIO_ORG_SUBMISSION_URL)
                return output
            continue

        if not selection.isdigit():
            print("Enter the number for the option you want.")
            continue

        selected_index = int(selection)
        if selected_index == 0:
            return None
        if 1 <= selected_index <= len(editable_fields):
            field = editable_fields[selected_index - 1]
            if field == "server":
                output.server = prompt_custom_output_server(output.server)
            elif field == "port":
                output.port = prompt_output_port(output.port)
            elif field == "username":
                output.username = prompt_text("Username: ", output.username).strip()
            elif field == "password":
                output.password = prompt_text("Password: ", output.password, hidden=True, allow_toggle=True)
            elif field == "mountpoint":
                if provider == "noaa_radio_org":
                    if station is None:
                        raise SetupError("Station metadata is required for NOAA Weather Radio Org outputs.")
                    output.mountpoint = prompt_noaa_mountpoint(station, output.mountpoint, exclude=exclude)
                else:
                    output.mountpoint = normalize_mountpoint(prompt_text("Mountpoint: ", output.mountpoint).strip())
            elif field == "bitrate":
                output.bitrate = prompt_output_bitrate(output)
        elif confirm_index is not None and selected_index == confirm_index:
            success, message = authenticate_output(output)
            print()
            print(message)
            if success:
                if provider == "noaa_radio_org":
                    show_submission_notice(NOAA_RADIO_ORG_SUBMISSION_URL)
                return output
        else:
            print("That selection is not available.")


def prompt_icecast_output(station: dict[str, str] | None) -> IcecastOutput | None:
    while True:
        selection = prompt_menu_with_back(
            "Streaming Platform",
            [
                "GWES Weather Radio",
                "Weather USA",
                "NOAA Weather Radio Org",
                "Enter Credentials Manually",
            ],
            "Back to stream setup",
        )
        if selection == -1:
            return None
        if selection == 0:
            show_submission_notice(GWES_SUBMISSION_URL)
            result = prompt_output_credentials(default_gwes_output(), station=station)
        elif selection == 1:
            show_submission_notice(WEATHER_USA_SUBMISSION_URL)
            result = prompt_output_credentials(default_weather_usa_output(), station=station)
        elif selection == 2:
            show_noaa_radio_org_lookup_notice()
            if station is None:
                result = prompt_output_credentials(default_noaa_radio_org_custom_output(), station=None)
            else:
                result = prompt_output_credentials(default_noaa_radio_org_output(station), station=station)
        else:
            result = prompt_output_credentials(default_manual_output(), station=station)
        if result is not None:
            return result


def ensure_shared_stream_assets() -> None:
    FALLBACK_TARGET_DIR.mkdir(parents=True, exist_ok=True)
    if not FALLBACK_TARGET_PATH.exists():
        shutil.copy2(FALLBACK_SOURCE_PATH, FALLBACK_TARGET_PATH)
    eas_target_parent = EAS_SCRIPT_TARGET_PATH.parent
    eas_target_parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(EAS_SCRIPT_SOURCE_PATH, EAS_SCRIPT_TARGET_PATH)

    ensure_group_exists(STREAM_GROUP)
    stream_group = grp.getgrnam(STREAM_GROUP)
    os.chown(FALLBACK_TARGET_DIR, 0, stream_group.gr_gid)
    os.chmod(FALLBACK_TARGET_DIR, 0o750)
    os.chown(FALLBACK_TARGET_PATH, 0, stream_group.gr_gid)
    os.chmod(FALLBACK_TARGET_PATH, 0o640)
    os.chown(EAS_SCRIPT_TARGET_PATH, 0, 0)
    os.chmod(EAS_SCRIPT_TARGET_PATH, 0o755)


def build_output_block(output: IcecastOutput) -> str:
    parsed = urlparse(output.server)
    host = parsed.hostname
    if not host:
        raise SetupError("Icecast server is invalid.")

    block_lines = [
        "output.icecast(",
        f"  %mp3(bitrate={output.bitrate}, stereo=false, samplerate=16000),",
        f'  host="{escape_liquidsoap_string(host)}",',
        f"  port={output.port},",
    ]
    if parsed.scheme == "https":
        block_lines.append('  protocol="https",')
    block_lines.extend(
        [
            f'  user="{escape_liquidsoap_string(output.username)}",',
            f'  password="{escape_liquidsoap_string(output.password)}",',
            f'  mount="{escape_liquidsoap_string(output.mountpoint)}",',
            "  name=callsign,",
            '  description="#{site_name}, #{state_abbrev}",',
            '  genre="Weather",',
            "  public=true,",
            "  radio",
            ")",
        ]
    )
    return "\n".join(block_lines)


def find_matching_parenthesis(text: str, open_index: int) -> int:
    depth = 0
    in_string = False
    escape = False
    comment = False
    for index in range(open_index, len(text)):
        char = text[index]
        if comment:
            if char == "\n":
                comment = False
            continue
        if in_string:
            if escape:
                escape = False
                continue
            if char == "\\":
                escape = True
                continue
            if char == '"':
                in_string = False
            continue
        if char == "#":
            comment = True
            continue
        if char == '"':
            in_string = True
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    raise SetupError("Could not parse Liquidsoap output.icecast block.")


def extract_liquidsoap_icecast_outputs(config_text: str) -> list[ParsedIcecastOutput]:
    outputs = []
    search_from = 0
    while True:
        start = config_text.find("output.icecast(", search_from)
        if start == -1:
            return outputs

        open_index = config_text.find("(", start)
        end = find_matching_parenthesis(config_text, open_index)
        block = config_text[start : end + 1]
        host_match = re.search(r'\bhost\s*=\s*"([^"]+)"', block)
        port_match = re.search(r"\bport\s*=\s*(\d+)", block)
        user_match = re.search(r'\buser\s*=\s*"([^"]*)"', block)
        password_match = re.search(r'\bpassword\s*=\s*"([^"]*)"', block)
        mount_match = re.search(r'\bmount\s*=\s*"([^"]+)"', block)
        bitrate_match = re.search(r"%mp3\((?:[^()\"#]|\"[^\"]*\"|\([^()]*\))*\bbitrate\s*=\s*(\d+)", block, re.DOTALL)

        if host_match and port_match and user_match and password_match and mount_match:
            scheme = "https" if 'protocol="https"' in block or "http.transport.ssl()" in block else "http"
            output = IcecastOutput(
                server=f"{scheme}://{host_match.group(1)}",
                port=port_match.group(1),
                username=user_match.group(1),
                password=password_match.group(1),
                mountpoint=mount_match.group(1),
                bitrate=bitrate_match.group(1) if bitrate_match else ("32" if host_match.group(1) == "radio-master.weatherusa.net" else "64"),
            )
            outputs.append(ParsedIcecastOutput(output=output, start=start, end=end + 1))

        search_from = end + 1


def read_stream_config(callsign_lower: str) -> str:
    stream_path = LIQUIDSOAP_BIN_DIR / f"{callsign_lower}.liq"
    try:
        return stream_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise SetupError(f"Stream config not found: {stream_path}") from error


def write_stream_config(callsign_lower: str, config_text: str) -> None:
    stream_path = LIQUIDSOAP_BIN_DIR / f"{callsign_lower}.liq"
    stream_user = pwd.getpwnam(callsign_lower)
    stream_group = grp.getgrnam(STREAM_GROUP)
    stream_path.write_text(config_text, encoding="utf-8")
    os.chown(stream_path, stream_user.pw_uid, stream_group.gr_gid)
    os.chmod(stream_path, 0o750)


def restart_stream_service(callsign_lower: str) -> None:
    run_command(["systemctl", "restart", f"{callsign_lower}.service"])


def delete_stream(callsign_lower: str) -> None:
    response = input("Delete this stream? (y/n): ").strip().lower()
    if response not in ("y", "yes"):
        print("Stream was not deleted.")
        return

    service_name = f"{callsign_lower}.service"
    service_path = STREAM_SERVICE_DIR / service_name
    liq_path = LIQUIDSOAP_BIN_DIR / f"{callsign_lower}.liq"

    run_command(["systemctl", "disable", "--now", service_name], check=False)
    if service_path.exists():
        service_path.unlink()
    if liq_path.exists():
        liq_path.unlink()
    run_command(["systemctl", "daemon-reload"])
    run_command(["pkill", "-u", callsign_lower], check=False)
    run_command(["userdel", "-r", callsign_lower], check=False)

    print()
    print(f"Stream {callsign_lower.upper()} deleted.")


def output_menu_label(output: IcecastOutput) -> str:
    return f"{output.server} | {output.mountpoint}"


def read_iqbus_stream_settings() -> tuple[str, int]:
    config_text = read_iqbus_config()
    port_value = get_config_value(config_text, "port")
    if not port_value or not port_value.isdigit():
        raise SetupError("IQ bus config is missing a valid port.")
    return ("127.0.0.1", int(port_value))


def frequency_mhz_to_hz(frequency: str) -> int:
    return int((Decimal(frequency) * Decimal("1000000")).to_integral_value())


def escape_liquidsoap_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_stream_liquidsoap(station: dict[str, str], output: IcecastOutput) -> str:
    iqbus_host, iqbus_port = read_iqbus_stream_settings()
    template = load_template("stream.liq.template")
    output_block = build_output_block(output)

    replacements = {
        "<IQBUS_SERVER>": escape_liquidsoap_string(iqbus_host),
        "<IQBUS_PORT>": str(iqbus_port),
        "<CALLSIGN>": escape_liquidsoap_string(station["callsign"]),
        "<CALLSIGN_LOWER>": station["callsign"].lower(),
        "<SITE_NAME>": escape_liquidsoap_string(station["site_name"]),
        "<STATE_ABBREV>": escape_liquidsoap_string(station["state"]),
        "<STATION_FREQ_HZ>": str(frequency_mhz_to_hz(station["frequency"])),
    }
    for placeholder, value in replacements.items():
        template = template.replace(placeholder, value)
    start = template.find("output.icecast(")
    if start == -1:
        raise SetupError("Stream Liquidsoap template is missing an output.icecast block.")
    end = find_matching_parenthesis(template, template.find("(", start))
    template = template[:start] + output_block + template[end + 1 :]
    return template


def build_stream_service(callsign_lower: str) -> str:
    template = load_template("stream.service.template")
    replacements = {
        "<CALLSIGN>": callsign_lower.upper(),
        "<CALLSIGN_LOWER>": callsign_lower,
    }
    for placeholder, value in replacements.items():
        template = template.replace(placeholder, value)
    return template


def ensure_stream_user(callsign_lower: str) -> None:
    ensure_group_exists(STREAM_GROUP)
    ensure_user_exists(callsign_lower)
    ensure_user_in_group(callsign_lower, STREAM_GROUP)


def write_stream_files(callsign_lower: str, liquidsoap_text: str, service_text: str) -> None:
    liq_path = LIQUIDSOAP_BIN_DIR / f"{callsign_lower}.liq"
    service_path = STREAM_SERVICE_DIR / f"{callsign_lower}.service"
    liq_path.write_text(liquidsoap_text, encoding="utf-8")
    service_path.write_text(service_text, encoding="utf-8")

    stream_user = pwd.getpwnam(callsign_lower)
    stream_group = grp.getgrnam(STREAM_GROUP)
    os.chown(liq_path, stream_user.pw_uid, stream_group.gr_gid)
    os.chmod(liq_path, 0o750)
    os.chown(service_path, 0, 0)
    os.chmod(service_path, 0o644)


def enable_stream_service(callsign_lower: str) -> None:
    service_name = f"{callsign_lower}.service"
    run_command(["systemctl", "daemon-reload"])
    run_command(["systemctl", "enable", "--now", service_name])


def create_stream() -> None:
    if not IQBUS_CONFIG_PATH.exists():
        raise SetupError("Configure the SDR server before creating a stream.")

    station = prompt_station_selection()
    if station is None:
        return

    output = prompt_icecast_output(station)
    if output is None:
        return

    callsign_lower = station["callsign"].lower()
    ensure_stream_user(callsign_lower)
    ensure_shared_stream_assets()
    liquidsoap_text = build_stream_liquidsoap(station, output)
    service_text = build_stream_service(callsign_lower)
    write_stream_files(callsign_lower, liquidsoap_text, service_text)
    enable_stream_service(callsign_lower)

    print()
    print(f"Stream {station['callsign']} created and started.")


def remove_output_from_stream(callsign_lower: str, parsed_output: ParsedIcecastOutput) -> None:
    response = input("Remove this output? (y/n): ").strip().lower()
    if response not in ("y", "yes"):
        print("Output was not removed.")
        return

    config_text = read_stream_config(callsign_lower)
    new_config = config_text[: parsed_output.start] + config_text[parsed_output.end :]
    write_stream_config(callsign_lower, new_config)
    restart_stream_service(callsign_lower)
    print()
    print("Output removed.")


def update_output_in_stream(callsign_lower: str, parsed_output: ParsedIcecastOutput, output: IcecastOutput) -> None:
    config_text = read_stream_config(callsign_lower)
    replacement = build_output_block(output)
    new_config = config_text[: parsed_output.start] + replacement + config_text[parsed_output.end :]
    write_stream_config(callsign_lower, new_config)
    restart_stream_service(callsign_lower)
    print()
    print("Output updated.")


def append_output_to_stream(callsign_lower: str, output: IcecastOutput) -> None:
    config_text = read_stream_config(callsign_lower)
    block = build_output_block(output)
    new_config = config_text.rstrip() + "\n\n" + block + "\n"
    write_stream_config(callsign_lower, new_config)
    restart_stream_service(callsign_lower)
    print()
    print("Output added.")


def edit_output_menu(
    callsign_lower: str,
    parsed_output: ParsedIcecastOutput,
    station: dict[str, str] | None,
) -> None:
    output = IcecastOutput(**vars(parsed_output.output))
    while True:
        print()
        print("Edit Output")
        print("0. Back")
        provider = output_provider(output, station)
        editable_fields: list[str] = []
        options = []
        if provider == "custom":
            editable_fields.extend(["server", "port", "username"])
            options.extend(
                [
                    f"Server: {output.server or '(blank)'}",
                    f"Port: {output.port or '(blank)'}",
                    f"Username: {output.username or '(blank)'}",
                ]
            )
        elif provider == "gwes":
            editable_fields.append("username")
            options.append(f"Username: {output.username or '(blank)'}")
        elif provider == "noaa_radio_org":
            editable_fields.append("mountpoint")
            options.append(f"Mountpoint: {output.mountpoint or '(blank)'}")
        if provider != "noaa_radio_org":
            editable_fields.extend(["password", "mountpoint", "bitrate"])
            options.extend(
                [
                    f"Password: {'*' * len(output.password) if output.password else '(blank)'}",
                    f"Mountpoint: {output.mountpoint or '(blank)'}",
                    f"Bitrate: {output.bitrate or '(blank)'} kbps",
                ]
            )
        confirm_index = None
        if output_fields_complete(output):
            options.append("Confirm")
            confirm_index = len(options)
        options.append("Remove Output")
        remove_index = len(options)
        for index, option in enumerate(options, start=1):
            print(f"{index}. {option}")

        selection = input("Select an option: ").strip()
        if not selection:
            if not output_fields_complete(output):
                print("One or more fields are left blank.")
                continue
            success, message = authenticate_output(output)
            print()
            print(message)
            if success:
                update_output_in_stream(callsign_lower, parsed_output, output)
                return
            continue

        if not selection.isdigit():
            print("Enter the number for the option you want.")
            continue

        selected_index = int(selection)
        if selected_index == 0:
            return
        if 1 <= selected_index <= len(editable_fields):
            field = editable_fields[selected_index - 1]
            if field == "server":
                output.server = prompt_custom_output_server(output.server)
            elif field == "port":
                output.port = prompt_output_port(output.port)
            elif field == "username":
                output.username = prompt_text("Username: ", output.username).strip()
            elif field == "password":
                output.password = prompt_text("Password: ", output.password, hidden=True, allow_toggle=True)
            elif field == "mountpoint":
                if provider == "noaa_radio_org":
                    output.mountpoint = prompt_noaa_mountpoint(
                        station,
                        output.mountpoint,
                        exclude=(callsign_lower, parsed_output.output.mountpoint),
                    )
                else:
                    output.mountpoint = normalize_mountpoint(prompt_text("Mountpoint: ", output.mountpoint).strip())
            elif field == "bitrate":
                output.bitrate = prompt_output_bitrate(output)
        elif confirm_index is not None and selected_index == confirm_index:
            success, message = authenticate_output(output)
            print()
            print(message)
            if success:
                if provider == "noaa_radio_org":
                    show_submission_notice(NOAA_RADIO_ORG_SUBMISSION_URL)
                update_output_in_stream(callsign_lower, parsed_output, output)
                return
        elif selected_index == remove_index:
            remove_output_from_stream(callsign_lower, parsed_output)
            return
        else:
            print("That selection is not available.")


def outputs_menu(callsign_lower: str, station: dict[str, str] | None) -> None:
    while True:
        parsed_outputs = extract_liquidsoap_icecast_outputs(read_stream_config(callsign_lower))
        options = [output_menu_label(parsed.output) for parsed in parsed_outputs]
        options.append("Add output")
        selection = prompt_menu_with_back("Outputs", options, "Back to stream menu")
        if selection == -1:
            return
        if selection == len(options) - 1:
            output = prompt_icecast_output(station)
            if output is not None:
                append_output_to_stream(callsign_lower, output)
        else:
            edit_output_menu(callsign_lower, parsed_outputs[selection], station)


def stream_menu(callsign: str) -> None:
    callsign_lower = callsign.lower()
    station = get_station_by_callsign(callsign)
    while True:
        service_name = f"{callsign_lower}.service"
        report_failed_stream_service(callsign_lower)
        stream_running = service_is_running(service_name)
        start_on_boot = service_starts_on_boot(service_name)
        options = [
            "Outputs",
            "Stop Stream" if stream_running else "Start Stream",
            f"Start Stream On Host Boot: {'on' if start_on_boot else 'off'}",
            "Delete Stream",
        ]
        selection = prompt_menu_with_back("Manage Stream", options, "Back to streams")
        if selection == -1:
            return
        if selection == 0:
            outputs_menu(callsign_lower, station)
        elif selection == 1:
            if stream_running:
                stop_stream_service(callsign_lower)
            else:
                start_stream_service(callsign_lower)
        elif selection == 2:
            toggle_stream_start_on_boot(callsign_lower)
        elif selection == 3:
            delete_stream(callsign_lower)
            return


def manage_streams() -> None:
    while True:
        existing_streams = list_existing_streams()
        options = existing_streams + ["Add stream"]
        selection = prompt_menu_with_back("Manage Streams", options, "Back to main menu")
        if selection == -1:
            return

        if selection == len(options) - 1:
            create_stream()
        else:
            stream_menu(existing_streams[selection])


def main_menu() -> int:
    while True:
        selection = prompt_menu(
            "NWR Stream Builder",
            [
                "Server configuration",
                "Manage streams",
                "Exit",
            ],
        )

        if selection == 0:
            try:
                configure_server()
            except SetupError as error:
                print()
                print(f"Setup failed: {error}", file=sys.stderr)
        elif selection == 1:
            try:
                manage_streams()
            except SetupError as error:
                print()
                print(f"Setup failed: {error}", file=sys.stderr)
        else:
            print("Exiting.")
            return 0


def main() -> int:
    missing = missing_dependencies()
    if missing:
        print("Missing required dependencies:", file=sys.stderr)
        for dependency in missing:
            print(f"- {dependency}", file=sys.stderr)
        return 1

    try:
        ensure_root()
    except SetupError as error:
        print(error, file=sys.stderr)
        return 1

    print("All dependencies are met.")
    return main_menu()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print()
        raise SystemExit(130)
