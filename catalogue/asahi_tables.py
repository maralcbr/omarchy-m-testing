#!/usr/bin/env python3
"""Asahi layer of the feature catalogue: parse Asahi's feature tables, seed and check for drift.

Asahi Linux publishes per-chip feature-support tables as Markdown in
github.com/AsahiLinux/docs (docs/platform/feature-support/m1.md ... m4.md),
licensed CC-BY-3.0. This script turns them into the catalogue's Asahi layer:
one feature per table row, with an Asahi state per chip generation and,
where a Mac model's column differs from its generation, per board.

  asahi_tables.py check  [--source DIR | --live]   exit 1 and list the drift if the
                                                   catalogue's Asahi layer differs
  asahi_tables.py update [--source DIR | --live]   rewrite the catalogue's Asahi layer
                                                   (Aurora and Omarchy fields are kept)

--source defaults to catalogue/asahi-snapshot/, the pinned copy recorded in
the catalogue's sources.asahi.commit. --live reads Asahi's main branch.

Unknown table columns (a new Mac model), unknown rows (a new feature) and cells
this parser can't read are drift too: add them to COLUMNS / ROWS below.
An Asahi-layer feature marked "asahi_tables": false is kept by hand (hardware
the tables have no row for, such as DisplayPort audio): check and update
leave it alone.
Python 3 standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
CATALOGUE = os.path.join(HERE, "catalogue.json")
SNAPSHOT = os.path.join(HERE, "asahi-snapshot")
PAGES = ("m1", "m2", "m3", "m4")
LIVE_URL = "https://raw.githubusercontent.com/AsahiLinux/docs/main/docs/platform/feature-support/{page}.md"
DEVICES = "device-list"
DEVICES_URL = "https://raw.githubusercontent.com/AsahiLinux/docs/main/docs/hw/devices/device-list.md"
# Asahi's device list prints the MacBook Neo's SoC as T8040; its feature table,
# its device tree and the catalogue say t8140.
SOC_FIXES = {"j700": "t8140"}
NOT_IN_TABLES = {"status": "no", "cell": "not in Asahi's tables"}

# Asahi column header (as printed, <br> -> space) -> the chip generations it covers
# and, for model columns, the device-tree boards it stands for. A generation's
# state is the most common state among the columns that have the hardware; a
# column that differs (including "-", no such hardware) is recorded per board.
COLUMNS = {
    # SoC-block tables
    "M1 (T8103)": {"chips": ["m1"]},
    "M1 Pro/Max/Ultra (T600x)": {"chips": ["m1-pro-max-ultra"]},
    "M2 (T8112)": {"chips": ["m2"]},
    "M2 Pro/Max/Ultra (T602x)": {"chips": ["m2-pro-max-ultra"]},
    "M3 (T8122)": {"chips": ["m3"]},
    "M3 Pro (T6030)": {"chips": ["m3-pro"]},
    "M3 Max (T603{1,4})": {"chips": ["m3-max"]},
    "M3 Ultra (T6032)": {"chips": ["m3-ultra"]},
    "A18 Pro (t8140)": {"chips": ["a18-pro"]},
    "M4 (T8132)": {"chips": ["m4"]},
    "M4 Pro/Max (T604x)": {"chips": ["m4-pro-max"]},
}
MODEL_COLUMNS = {
    ("m1", "Mac Mini (2020)"): {"m1": ["j274"]},
    ("m1", "MacBook Pro (13-inch, 2020)"): {"m1": ["j293"]},
    ("m1", "MacBook Air (2020)"): {"m1": ["j313"]},
    ("m1", "iMac (2021)"): {"m1": ["j456", "j457"]},
    ("m1", "MacBook Pro (14/16-inch, 2021)"): {"m1-pro-max-ultra": ["j314s", "j314c", "j316s", "j316c"]},
    ("m1", "Mac Studio (2022)"): {"m1-pro-max-ultra": ["j375c", "j375d"]},
    ("m2", "MacBook Air (13-inch, 2022)"): {"m2": ["j413"]},
    ("m2", "MacBook Air (15-inch, 2023)"): {"m2": ["j415"]},
    ("m2", "MacBook Pro (13-inch, 2022)"): {"m2": ["j493"]},
    ("m2", "Mac Mini (2023)", 0): {"m2": ["j473"]},
    ("m2", "Mac Mini (2023)", 1): {"m2-pro-max-ultra": ["j474s"]},
    ("m2", "MacBook Pro (14/16-inch, 2023)"): {"m2-pro-max-ultra": ["j414s", "j414c", "j416s", "j416c"]},
    ("m2", "Mac Studio (2023)"): {"m2-pro-max-ultra": ["j475c", "j475d"]},
    ("m2", "Mac Pro (2023)"): {"m2-pro-max-ultra": ["j180d"]},
    ("m3", "iMac (2023)"): {"m3": ["j433", "j434"]},
    ("m3", "MacBook Pro (14-inch, late 2023)"): {"m3": ["j504"]},
    ("m3", "MacBook Air (13/15-inch 2024)"): {"m3": ["j613", "j615"]},
    ("m3", "MacBook Pro (14/16-inch, late 2023)"): {"m3-pro": ["j514s", "j516s"], "m3-max": ["j514c", "j516c", "j514m", "j516m"]},
    ("m3", "Mac Studio (2025)"): {"m3-ultra": ["j575d"]},
    ("m4", "Macbook Neo (2026)"): {"a18-pro": ["j700"]},
    ("m4", "iMac (2024)"): {"m4": ["j623", "j624"]},
    ("m4", "Mac mini (2024)", 0): {"m4": ["j773g"]},
    ("m4", "MacBook Pro (14-inch, Nov 2024)"): {"m4": ["j604"]},
    ("m4", "MacBook Air (13\" and 15\", 2025)"): {"m4": ["j713", "j715"]},
    ("m4", "Mac mini (2024)", 1): {"m4-pro-max": ["j773s"]},
    ("m4", "Mac Studio (2025)"): {"m4-pro-max": ["j575c"]},
    ("m4", "MacBook Pro (14\" and 16\", 2024)"): {"m4-pro-max": ["j614s", "j616s", "j614c", "j616c"]},
}

# Asahi row label (lower-cased, markup stripped) -> catalogue feature id and name.
ROWS = {
    "dcp": ("dcp", "Display coprocessor (DCP)"),
    "usb2 (tb ports)": ("usb2-tb-ports", "USB2 on Thunderbolt ports"),
    "usb3 (tb ports)": ("usb3-tb-ports", "USB3 on Thunderbolt ports"),
    "thunderbolt": ("thunderbolt", "Thunderbolt"),
    "dp alt mode": ("dp-alt-mode", "DisplayPort alt mode"),
    "gpu": ("gpu", "GPU"),
    "video decoder": ("video-decoder", "Video decoder"),
    "nvme": ("nvme", "NVMe storage"),
    "pcie": ("pcie", "PCIe"),
    "pcie (ge)": ("pcie-ge", "PCIe (GE)"),
    "cpufreq": ("cpufreq", "CPU frequency scaling"),
    "cpuidle": ("cpuidle", "CPU idle"),
    "suspend/sleep": ("suspend-sleep", "Suspend and sleep"),
    "video encoder": ("video-encoder", "Video encoder"),
    "prores codec": ("prores-codec", "ProRes codec"),
    "aicv2": ("aic", "Interrupt controller (AIC)"),
    "aicv3": ("aic", "Interrupt controller (AIC)"),
    "dart": ("dart", "IOMMU (DART)"),
    "pmu": ("pmu", "Performance monitoring unit"),
    "uart": ("uart", "UART"),
    "watchdog": ("watchdog", "Watchdog"),
    "i2c": ("i2c", "I2C"),
    "gpio": ("gpio", "GPIO"),
    "usb-pd": ("usb-pd", "USB power delivery"),
    "mca": ("mca", "Audio interface (MCA)"),
    "spi": ("spi", "SPI"),
    "spi nor": ("spi-nor", "SPI NOR flash"),
    "smc": ("smc", "System management controller (SMC)"),
    "spmi": ("spmi", "SPMI"),
    "rtc": ("rtc", "Real-time clock"),
    "sep": ("sep", "Secure enclave (SEP)"),
    "neural engine": ("neural-engine", "Neural engine"),
    "installer": ("installer", "Asahi installer"),
    "devicetree": ("devicetree", "Device tree"),
    "main display": ("main-display", "Built-in display"),
    "brightness": ("brightness", "Display brightness"),
    "hdmi out": ("hdmi-out", "HDMI out"),
    "hdmi audio": ("hdmi-audio", "HDMI audio"),
    "keyboard": ("keyboard", "Keyboard"),
    "kb backlight": ("kb-backlight", "Keyboard backlight"),
    "touchpad": ("touchpad", "Trackpad"),
    "battery info": ("battery-info", "Battery information"),
    "usb-a ports": ("usb-a-ports", "USB-A ports"),
    "wifi": ("wifi", "Wi-Fi"),
    "bluetooth": ("bluetooth", "Bluetooth"),
    "3.5mm jack": ("headphone-jack", "3.5 mm headphone jack"),
    "speakers": ("speakers", "Speakers"),
    "sd card slot": ("sd-card-slot", "SD card slot"),
    "1gbps ethernet": ("ethernet-1g", "1 Gbps Ethernet"),
    "10gbps ethernet": ("ethernet-10g", "10 Gbps Ethernet"),
    "microphones": ("microphones", "Microphones"),
    "webcam": ("webcam", "Webcam"),
    "touch bar": ("touch-bar", "Touch Bar"),
    "touchid": ("touch-id", "Touch ID"),
}


class Drift(Exception):
    """Asahi's tables contain something this parser doesn't know."""


# -- parsing ---------------------------------------------------------------


def _clean(text: str) -> str:
    text = re.sub(r"<br\s*/?>", " ", text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_cell(raw: str) -> dict:
    """One table cell -> {status, version?, note?, cell}. Raises Drift if unreadable."""
    if m := re.fullmatch(r"Boot framebuffer - (.+), full support - (.+)", _clean(raw)):
        # Two stages in one cell: the full-support stage decides, the framebuffer stage is kept as a note.
        state = parse_cell(m.group(2))
        boot = parse_cell(m.group(1))["cell"]
        state["note"] = (state["note"] + "; " if state.get("note") else "") + f"boot framebuffer: {boot}"
        state["cell"] = _clean(raw)
        return state
    has_notes = bool(re.search(r"\[notes\]\(#[^)]*\)", raw))
    text = _clean(re.sub(r"\(?\[notes\]\(#[^)]*\)\)?", "", raw))
    state: dict = {}
    if text == "":
        if not has_notes:
            raise Drift(f"empty cell {raw!r}")
        state["status"] = "see-notes"
    elif text == "-":
        state["status"] = "absent"
    elif text in ("WIP", "TBA", "yes", "no"):
        state["status"] = text.lower()
    elif text == "out of tree":
        state["status"] = "out-of-tree"
    elif m := re.fullmatch(r"linux-asahi(?: \((.+)\))?", text):
        state["status"] = "linux-asahi"
        detail = m.group(1)
        if detail and re.fullmatch(r"[0-9]+\.[0-9]+", detail):
            state["version"] = detail
        elif detail:
            state["note"] = detail
    elif re.match(r"[0-9]+\.[0-9]+", text):
        state["status"] = "upstream"
        state["version"] = text
    else:
        raise Drift(f"unreadable cell {raw!r}")
    if has_notes:
        state["note"] = (state.get("note", "") + "; " if state.get("note") else "") + "see Asahi's notes"
    state["cell"] = text + (" (notes)" if has_notes else "") if text else "(notes)"
    return state


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse_page(page: str, text: str) -> list[dict]:
    """A page -> tables: {kind: "soc"|"model", columns: [spec...], rows: {label: [cell...]}}."""
    tables, current, seen_headers = [], None, Counter()
    for line in text.splitlines():
        if not line.startswith("|"):
            current = None
            continue
        cells = _cells(line)
        if current is None:
            headers = [_clean(c) for c in cells[1:]]
            kind = "soc" if headers and headers[0] in COLUMNS else "model"
            specs = []
            for header in headers:
                if kind == "soc":
                    spec = COLUMNS.get(header)
                else:
                    key = (page, header, seen_headers[(page, header)])
                    spec = MODEL_COLUMNS.get(key) or (MODEL_COLUMNS.get((page, header)) if key[2] == 0 else None)
                    seen_headers[(page, header)] += 1
                    spec = {"chips": list(spec), "boards": spec} if spec else None
                if spec is None:
                    raise Drift(f"{page}.md: unknown column {header!r} (a new Mac model or chip?)")
                specs.append({"header": header, **spec})
            current = {"kind": kind, "columns": specs, "rows": {}}
            tables.append(current)
        elif set("".join(cells)) <= set(":- "):
            continue  # the |---|:---:| separator
        else:
            label = _clean(cells[0]).lower()
            if label not in ROWS:
                raise Drift(f"{page}.md: unknown row {_clean(cells[0])!r} (a new feature?)")
            values = cells[1:]
            if len(values) != len(current["columns"]):
                raise Drift(f"{page}.md: row {label!r} has {len(values)} cells for {len(current['columns'])} columns")
            current["rows"][label] = [parse_cell(v) for v in values]
    return tables


def _key(state: dict) -> str:
    return json.dumps(state, sort_keys=True)


def asahi_layer(pages: dict[str, str]) -> dict[str, dict]:
    """Asahi's tables -> {feature_id: {"name", "chips": {chip: state}, "models": {board: state}}}."""
    features: dict[str, dict] = {}
    for page in PAGES:
        for table in parse_page(page, pages[page]):
            for label, states in table["rows"].items():
                feature_id, name = ROWS[label]
                feature = features.setdefault(feature_id, {"name": name, "chips": {}, "models": {}})
                by_chip: dict[str, list] = {}
                for column, state in zip(table["columns"], states):
                    for chip in column["chips"]:
                        by_chip.setdefault(chip, []).append((column, state))
                for chip, entries in by_chip.items():
                    if chip in feature["chips"]:
                        raise Drift(f"{page}.md: feature {feature_id!r} listed twice for {chip}")
                    # Models without the hardware ("-") don't decide the generation's state.
                    present = [s for _, s in entries if s["status"] != "absent"] or [s for _, s in entries]
                    counts = Counter(_key(s) for s in present)
                    best = max(counts.values())
                    common = next(s for s in present if counts[_key(s)] == best)
                    feature["chips"][chip] = common
                    for column, state in entries:
                        if _key(state) != _key(common):
                            for board in column.get("boards", {}).get(chip, []):
                                feature["models"][board] = state
    return features


def parse_devices(text: str, chips: dict) -> dict[str, dict]:
    """Asahi's device list -> {board: {"name", "chip", "soc"}}. Raises Drift on a SoC no chip lists."""
    boards = {}
    for line in text.splitlines():
        cells = _cells(line) if line.startswith("|") else []
        if len(cells) < 4 or not re.fullmatch(r"J\w+AP", cells[2]):
            continue
        board = cells[2][0].lower() + cells[2][1:-2]
        soc = SOC_FIXES.get(board, cells[3].lower())
        chip = next((key for key, info in chips.items() if soc in info["socs"]), None)
        if chip is None:
            raise Drift(f"{DEVICES}.md: {board} has SoC {soc}, which no catalogue chip lists (a new chip?)")
        if board in boards:
            raise Drift(f"{DEVICES}.md: {board} listed twice")
        boards[board] = {"name": _clean(cells[0]), "chip": chip, "soc": soc}
    if not boards:
        raise Drift(f"{DEVICES}.md: no devices found")
    return boards


def table_chips(catalogue: dict) -> list[str]:
    """Chips Asahi's feature tables cover; the rest are marked "asahi_tables": false."""
    return [chip for chip, info in catalogue["chips"].items() if info.get("asahi_tables", True)]


# -- the catalogue ---------------------------------------------------------


def dump(value, indent: int = 0, width: int = 150) -> str:
    """JSON that keeps anything short enough on one line, so catalogue diffs stay readable."""
    flat = json.dumps(value, ensure_ascii=False, separators=(", ", ": "))
    pad, inner = "  " * indent, "  " * (indent + 1)
    if len(pad) + len(flat) <= width or not isinstance(value, (dict, list)) or not value:
        return flat
    if isinstance(value, dict):
        items = [f"{inner}{json.dumps(k)}: {dump(v, indent + 1, width - len(json.dumps(k)) - 2)}" for k, v in value.items()]
        return "{\n" + ",\n".join(items) + "\n" + pad + "}"
    return "[\n" + ",\n".join(inner + dump(v, indent + 1, width) for v in value) + "\n" + pad + "]"


def from_tables(feature: dict) -> bool:
    """False for an Asahi-layer feature kept by hand ("asahi_tables": false): hardware Asahi's tables have no row for."""
    return feature.get("asahi_tables", True)


def projection(catalogue: dict) -> dict[str, dict]:
    """The Asahi layer as it stands in the catalogue, in asahi_layer()'s shape."""
    found = {}
    covered = table_chips(catalogue)
    for feature in catalogue["features"]:
        if feature["layer"] != "asahi" or not from_tables(feature):
            continue
        found[feature["id"]] = {
            "name": feature["name"],
            "chips": {c: s["asahi"] for c, s in feature["chips"].items() if "asahi" in s and c in covered},
            "models": {b: s["asahi"] for b, s in feature.get("models", {}).items() if "asahi" in s},
        }
    return found


def board_drift(catalogue: dict, devices: dict[str, dict]) -> list[str]:
    have = {b: {k: info.get(k) for k in ("name", "chip", "soc")} for b, info in catalogue.get("boards", {}).items()}
    problems = [f"board {b}: in Asahi's device list, missing from the catalogue" for b in sorted(set(devices) - set(have))]
    problems += [f"board {b}: in the catalogue, no longer in Asahi's device list" for b in sorted(set(have) - set(devices))]
    for board in sorted(set(have) & set(devices)):
        if have[board] != devices[board]:
            problems.append(f"board {board}: catalogue says {have[board]}, Asahi says {devices[board]}")
    return problems


def drift(catalogue: dict, layer: dict[str, dict]) -> list[str]:
    have, want = projection(catalogue), layer
    problems = []
    for fid in sorted(set(want) - set(have)):
        problems.append(f"{fid}: in Asahi's tables, missing from the catalogue")
    for fid in sorted(set(have) - set(want)):
        problems.append(f"{fid}: in the catalogue, no longer in Asahi's tables")
    for fid in sorted(set(have) & set(want)):
        for part in ("chips", "models"):
            a, b = have[fid][part], want[fid][part]
            for key in sorted(set(a) | set(b)):
                if key not in a or key not in b or _key(a[key]) != _key(b[key]):
                    was = a.get(key, {}).get("cell", "(none)")
                    now = b.get(key, {}).get("cell", "(none)")
                    problems.append(f"{fid} [{key}]: catalogue says {was!r}, Asahi says {now!r}")
    return problems


def update(catalogue: dict, layer: dict[str, dict], devices: dict[str, dict]) -> dict:
    covered = table_chips(catalogue)
    by_id = {f["id"]: f for f in catalogue["features"]}
    features = [f for f in catalogue["features"] if f["layer"] != "asahi" or f["id"] in layer or not from_tables(f)]
    for fid, found in layer.items():
        feature = by_id.get(fid)
        if feature is None:
            feature = {"id": fid, "name": found["name"], "layer": "asahi", "chips": {}}
            features.append(feature)
        for chip in list(feature["chips"]):
            if chip in covered and chip not in found["chips"]:  # Asahi no longer lists it for this chip
                del feature["chips"][chip]
        for chip, state in found["chips"].items():
            entry = feature["chips"].setdefault(chip, {})
            entry["asahi"] = state
            entry.setdefault("aurora", {"status": "unknown"})
            feature["chips"][chip] = {"asahi": entry.pop("asahi"), **entry}
        models = feature.setdefault("models", {})
        for board in list(models):
            models[board].pop("asahi", None)
            if not models[board]:
                del models[board]
        for board, state in found["models"].items():
            models[board] = {"asahi": state, **models.get(board, {})}
        if not models:
            del feature["models"]
    order = list(catalogue["chips"])
    for feature in features:
        # A chip no Asahi table covers isn't supported anywhere yet; kept states win.
        for chip in order:
            if chip in covered:
                continue
            entry = feature["chips"].setdefault(chip, {})
            if feature["layer"] == "asahi":
                entry.setdefault("asahi", dict(NOT_IN_TABLES))
            entry.setdefault("aurora", {"status": "unsupported"})
            if feature["layer"] == "omarchy":
                entry.setdefault("omarchy", {"status": "unsupported"})
        feature["chips"] = {c: feature["chips"][c] for c in order if c in feature["chips"]}
    layers = {"asahi": 0, "aurora": 1, "omarchy": 2}
    catalogue["features"] = sorted(features, key=lambda f: layers[f["layer"]])
    kept = catalogue.get("boards", {})
    # Name, chip and SoC come from Asahi; the lineup and hardware facts are kept by hand.
    catalogue["boards"] = {
        board: {**info, **{k: v for k, v in kept.get(board, {}).items() if k not in info}}
        for board, info in devices.items()
    }
    return catalogue


def read_pages(source: str | None, live: bool) -> dict[str, str]:
    pages = {}
    for page in (*PAGES, DEVICES):
        if live:
            url = DEVICES_URL if page == DEVICES else LIVE_URL.format(page=page)
            with urllib.request.urlopen(url, timeout=30) as response:
                pages[page] = response.read().decode("utf-8")
        else:
            with open(os.path.join(source or SNAPSHOT, f"{page}.md"), encoding="utf-8") as f:
                pages[page] = f.read()
    return pages


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["check", "update"])
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--source", help="directory with m1.md..m4.md (default: the pinned snapshot)")
    where.add_argument("--live", action="store_true", help="read Asahi's main branch")
    parser.add_argument("--catalogue", default=CATALOGUE)
    args = parser.parse_args(argv)

    origin = "Asahi's main branch" if args.live else (args.source or "the pinned snapshot")
    with open(args.catalogue, encoding="utf-8") as f:
        catalogue = json.load(f)
    try:
        pages = read_pages(args.source, args.live)
        layer = asahi_layer(pages)
        devices = parse_devices(pages[DEVICES], catalogue["chips"])
    except Drift as problem:
        print(f"Asahi's tables changed in a way this parser doesn't know ({origin}):\n  {problem}")
        return 1

    if args.command == "check":
        problems = drift(catalogue, layer) + board_drift(catalogue, devices)
        if problems:
            print(f"The catalogue's Asahi layer has drifted from {origin} ({len(problems)} differences):")
            print("  " + "\n  ".join(problems))
            print("Run catalogue/asahi_tables.py update, review the diff and bump catalogue_version.")
            return 1
        print(f"Asahi layer matches {origin} ({len(layer)} features).")
        return 0

    with open(args.catalogue, "w", encoding="utf-8") as f:
        f.write(dump(update(catalogue, layer, devices)) + "\n")
    print(f"Asahi layer rewritten from {origin} ({len(layer)} features). Review the diff and bump catalogue_version.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
