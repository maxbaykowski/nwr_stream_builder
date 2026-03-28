#!/usr/bin/env python3

from __future__ import annotations

import grp
import os
import pwd
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


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
IQBUS_CONFIG_PATH = Path("/etc/iqbus.config")
IQBUS_SERVICE_PATH = Path("/etc/systemd/system/iqbus.service")
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"


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


def configure_server() -> None:
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


def main_menu() -> int:
    while True:
        selection = prompt_menu(
            "NWR Stream Builder",
            [
                "Server configuration",
                "Exit",
            ],
        )

        if selection == 0:
            try:
                configure_server()
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
    raise SystemExit(main())
