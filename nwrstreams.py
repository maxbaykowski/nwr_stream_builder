#!/usr/bin/env python3

from __future__ import annotations

import grp
import os
import pwd
import readline
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
LOW_SAMPLE_RATE_MIN = 225001
LOW_SAMPLE_RATE_MAX = 300000
HIGH_SAMPLE_RATE_MIN = 900001
HIGH_SAMPLE_RATE_MAX = 3200000
USB_WARNING_SAMPLE_RATE = 2560000
MIN_MANUAL_GAIN = 0.0
MAX_MANUAL_GAIN = 49.6


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


def restart_iqbus_if_active() -> None:
    result = run_command(["systemctl", "is-active", "--quiet", "iqbus.service"], check=False)
    if result.returncode == 0:
        run_command(["systemctl", "restart", "iqbus.service"])


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


def server_settings_menu() -> None:
    while True:
        config_text = read_iqbus_config()
        current_sample_rate = get_config_value(config_text, "band_sampling_rate") or "unknown"
        agc_enabled = gain_mode_is_hardware_agc(config_text)
        options = [
            (
                "RTL Sample Rate: "
                f"{current_sample_rate} samples/second "
                "(controls the SDR band sampling rate)"
            ),
            f"Hardware AGC: {'on' if agc_enabled else 'off'} (toggles tuner automatic gain control)",
        ]
        if not agc_enabled:
            current_gain = get_config_value(config_text, "gain") or "unknown"
            options.append(f"Manual Gain: {current_gain} dB (sets tuner gain when Hardware AGC is off)")

        selection = prompt_menu_with_back(
            "Server Configuration",
            options,
            "Back to main menu",
        )

        if selection == -1:
            return

        if selection == 0:
            configure_sample_rate()
        elif selection == 1:
            toggle_hardware_agc()
        elif selection == 2:
            configure_manual_gain()


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
