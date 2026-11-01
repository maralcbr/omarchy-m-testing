"""Which Mac this is, read from the device tree through the host.

Only non-identifying facts are read: the model string, the board and SoC ids
from the device-tree compatible list, the CPU architecture and the kernel
release. Never serial numbers or any per-unit identifier.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .host import Host

MODEL_PATH = "/proc/device-tree/model"
COMPATIBLE_PATH = "/proc/device-tree/compatible"
APPLE_PLATFORM = "apple,arm-platform"

# SoC id -> chip name, for every SoC in the catalogue.
CHIPS = {
    "t8103": "M1",
    "t6000": "M1 Pro",
    "t6001": "M1 Max",
    "t6002": "M1 Ultra",
    "t8112": "M2",
    "t6020": "M2 Pro",
    "t6021": "M2 Max",
    "t6022": "M2 Ultra",
    "t8122": "M3",
    "t6030": "M3 Pro",
    "t6031": "M3 Max",
    "t6034": "M3 Max",
    "t6032": "M3 Ultra",
    "t8132": "M4",
    "t6040": "M4 Pro",
    "t6041": "M4 Max",
    "t8140": "A18 Pro",
    "t8142": "M5",
    "t6050": "M5 Pro",
    "t6051": "M5 Max",
    "t6052": "M5 Ultra",
    "t8152": "M6",
}

_BOARD = re.compile(r"^apple,(j[0-9]{3}[a-z]{0,2})$")
_SOC = re.compile(r"^apple,(t[0-9]{4})$")


@dataclass(frozen=True)
class Machine:
    model: str
    board: str
    soc: str
    chip: str
    arch: str
    kernel: str
    compatible: tuple[str, ...]


class NotAppleSilicon(Exception):
    """This machine isn't an Apple Silicon Mac running Linux."""


def _read_strings(host: Host, path: str) -> list[str] | None:
    try:
        raw = host.read_file(path)
    except OSError:  # absent, a directory, unreadable: no usable device tree
        return None
    return [part for part in raw.decode("utf-8", "replace").split("\0") if part.strip()]


def identify(host: Host) -> Machine:
    compatible = _read_strings(host, COMPATIBLE_PATH)
    if not compatible or APPLE_PLATFORM not in compatible:
        raise NotAppleSilicon("no Apple Silicon device tree found")

    arch = host.run(["uname", "-m"]).stdout.strip()
    if arch != "aarch64":
        raise NotAppleSilicon(f"CPU architecture is {arch or 'unknown'}, not aarch64")

    board = next((m.group(1) for c in compatible if (m := _BOARD.match(c))), None)
    soc = next((m.group(1) for c in compatible if (m := _SOC.match(c))), None)
    model_parts = _read_strings(host, MODEL_PATH)
    model = model_parts[0].strip() if model_parts else ""
    if not board or not soc or not model.startswith("Apple "):
        raise NotAppleSilicon("the device tree doesn't name an Apple Mac model, board and chip")

    kernel = host.run(["uname", "-r"])
    return Machine(
        model=model,
        board=board,
        soc=soc,
        chip=CHIPS.get(soc, "unknown"),
        arch=arch,
        kernel=kernel.stdout.strip() if kernel.returncode == 0 and kernel.stdout.strip() else "unknown",
        compatible=tuple(compatible),
    )
