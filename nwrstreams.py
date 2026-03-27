#!/usr/bin/env python3

from __future__ import annotations

import shutil
import sys


DEPENDENCIES = {
    "rtl-sdr": ("rtl_sdr",),
    "multimon-ng": ("multimon-ng",),
    "sox": ("sox",),
    "ffmpeg": ("ffmpeg",),
    "liquidsoap": ("liquidsoap",),
    "sdr_server": ("sdr_server",),
    "sdr_server_client": ("sdr_server_client",),
}


def missing_dependencies() -> list[str]:
    missing = []
    for package_name, commands in DEPENDENCIES.items():
        if not any(shutil.which(command) for command in commands):
            missing.append(package_name)
    return missing


def main() -> int:
    missing = missing_dependencies()
    if missing:
        print("Missing required dependencies:", file=sys.stderr)
        for dependency in missing:
            print(f"- {dependency}", file=sys.stderr)
        return 1

    print("All dependencies are met.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
