"""Privacy: what may reach a report or a recording, and the evidence scrubber.

Two rules, applied to every report before it is written, shown or uploaded:

1. Allowlist. Only the fields in REPORT_ALLOWLIST reach the report; anything
   else a check or the machine description carries is dropped. The allowlist
   mirrors schema/report-v1.schema.json (a Seam A test keeps them equal).
2. Scrubbed, text-only, bounded evidence. Every evidence line passes the
   Scrubber, which replaces MAC addresses, IP addresses, Wi-Fi network names,
   home paths, hostnames, usernames, e-mail addresses, serial numbers (of the
   Mac, displays, audio and USB or Thunderbolt devices), Bluetooth addresses, the
   names people give their devices, hex dumps (bytes, and 32-bit words), disk
   identifiers, UUIDs and long hex identifiers with placeholders such as <mac> or <ssid>.
   Evidence is text only (non-text lines are replaced by a note) and at most
   EVIDENCE_BUDGET_BYTES (64 KiB) per report.

Recordings (recording.py) pass the same Scrubber before they are saved.

The Scrubber works from patterns plus what this machine tells it about
itself (Scrubber.for_host): its hostname, the accounts under /home and the
names of saved network connections (not those NetworkManager keeps for virtual
interfaces such as lo, docker0 or tailscale0, by type or by name), so those are
removed wherever they appear.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from .host import Host
from .system import IMAGE_KEYS

EVIDENCE_BUDGET_BYTES = 64 * 1024
EVIDENCE_LINE_MAX_CHARS = 500
EVIDENCE_LINES_PER_CHECK = 50

NON_TEXT_NOTE = "[non-text evidence removed]"
TRUNCATED_NOTE = "[evidence truncated: 64 KiB report limit]"

HOSTNAME_PATH = "/proc/sys/kernel/hostname"
HOME_DIR = "/home"
SAVED_CONNECTIONS = ["nmcli", "--get-values", "NAME,TYPE", "connection", "show"]
# Connections NetworkManager keeps for virtual interfaces are named after the
# interface (docker0, tailscale0, lo): not personal, and learning them would
# turn every mention of the interface into <ssid>. Wi-Fi, VPN, Ethernet and
# the rest are learned.
_INTERFACE_CONNECTION_TYPES = {"loopback", "bridge", "tun", "dummy", "veth", "macvlan", "vxlan", "ip-tunnel"}
# The same connections by name, as the kernel, Docker, Tailscale and libvirt
# name their interfaces: kept out of learning when nmcli gives a type that
# isn't a network a person names (Wi-Fi, VPN, WireGuard), and left alone where
# NetworkManager activates one on its own interface ("device (lo): Activation:
# starting connection 'lo'"). Only these exact shapes and contexts, so a
# Wi-Fi network called "docker-home", or even "docker0", is still scrubbed.
_INTERFACE_CONNECTION_NAME = re.compile(r"lo|docker\d+|tailscale\d+|veth[0-9a-f]+|br-[0-9a-f]{12}|virbr\d+(?:-nic)?")
_NAMED_NETWORK_TYPES = {"802-11-wireless", "wifi", "vpn", "wireguard", "wifi-p2p"}


def _quoted_network(m: re.Match) -> str:
    name = m.group(3)
    line = m.string[m.string.rfind("\n", 0, m.start()) + 1:m.start()]
    if (m.group(1).lower() == "connection" and _INTERFACE_CONNECTION_NAME.fullmatch(name)
            and line.endswith(f"device ({name}): Activation: starting ")):
        return m.group(0)
    return f"{m.group(1)}{m.group(2)}'<ssid>'"

# Only these fields reach a report. A dict lists allowed keys (True: keep the
# value as is); a one-item list means "a list of these".
_CLASSIFICATION = {
    "outcome": True,
    "feature": True,
    "layer": True,
    "expected": {"asahi": True, "aurora": True, "omarchy": True},
}
_SYSTEM = {
    "stack": True,
    "distro": True,
    "boot_loader": True,
    "encryption": True,
    "candidate_set": True,
    # The image's build record: only the keys system.IMAGE_KEYS reads (the schema lists the same).
    "image": {key: True for key in IMAGE_KEYS},
    "packages": [{"name": True, "version": True}],
}
# The hardware inventory: node types, statuses, driver-bound states and
# counts, and build-option names and values; never a property value or path.
_INVENTORY = {
    "nodes": [{"compatible": True, "status": True, "driver": True, "count": True}],
    "unclaimed": [{"compatible": True, "count": True, "outcome": True, "feature": True, "layer": True}],
    "kernel_config": {"reference": True, "differences": [{"option": True, "asahi": True, "kernel": True}], "omitted": True},
}
_SCORE = {"value": True, "unit": True, "tool": True, "suite": True}
_CHECK = {"id": True, "kind": True, "status": True, "evidence": True, "classification": _CLASSIFICATION, "score": _SCORE,
          "answered_by_default": True}
REPORT_ALLOWLIST: dict[str, Any] = {
    "schema_version": True,
    "tool": {"name": True, "version": True},
    "consent_version": True,
    "catalogue_version": True,
    "machine": {"model": True, "board": True, "soc": True, "chip": True, "arch": True, "kernel": True},
    "system": _SYSTEM,
    "unattended": True,
    "inventory": _INVENTORY,
    "checks": [_CHECK],
    # Added by signing.py after the allowlist is applied; listed so the allowlist stays the schema.
    "signature": {"public_key": True, "signature": True},
}
# Free-text report fields that are scrubbed like evidence. The model is the
# device-tree model string, constrained by the schema, and is left alone.
_SCRUBBED_MACHINE_FIELDS = ("kernel",)

# Names that are never personal and would wreck logs if scrubbed as hints
# (the image's default hostname is "omarchy", Arch's is "archlinux").
_NOT_PERSONAL = {
    "root", "nobody", "localhost", "omarchy", "archlinux", "alarm", "linux", "lo",
    "wlan0", "eth0", "wired connection 1",
}
_SYSTEM_USERS = {
    "root", "nobody", "dbus", "polkitd", "rtkit", "avahi", "colord", "gdm", "sddm", "greeter",
    "http", "git", "alpm", "dnsmasq", "uuidd", "systemd", "user", "daemon", "bin", "mail", "ftp",
}

_HEX = "[0-9A-Fa-f]"
_H16 = f"{_HEX}{{1,4}}"
_SYSLOG_TIME = r"[A-Z][a-z]{2} [ \d]\d \d\d:\d\d:\d\d(?:\.\d+)?"
_FULL_TIME = r"[A-Z][a-z]{2} \d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d+)? [A-Z][A-Za-z0-9+-]{1,5}"
_ISO_TIME = r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:[.,]\d+)?(?:[+-]\d\d:?\d\d|Z)?"
_USER = r"[a-z_][a-z0-9_.-]*"
_UNIT_SUFFIX = re.compile(r"\.(service|socket|target|timer|mount|slice|scope|device|path|swap|automount)$")

# A serial-like key, its separator (quotes kept) and its value: group 4 is the value.
_SERIAL_KEY = (
    r"(?:(?-i:serial)|(?:device[._]|ID_(?:USB_)?)serial(?:_short)?|i?serial[ _-]?(?:number|num|no)|iserial|unique[_-]id)"
)  # a bare "serial" only in lowercase: the kernel's "Serial: 8250/16550 driver" isn't one
SERIAL_FIELD = re.compile(
    rf"(?i)(?<![\w.-])((?:POWER_SUPPLY_)?{_SERIAL_KEY}[\"']?)(\s*(?:=>|[:=])\s*)([\"']?)(?!<|[\"',;}}\]]|\s|$)((?:(?<=[\"'])[^\"'\n]*|[^\s\"',;}}\]]+(?:\s+\(0x{_HEX}+\))?))(?=\3)"
)
# A serial value removed wherever it appears in the text: identifier-shaped, so a
# USB root hub's "SerialNumber: xhci-hcd.3.auto" doesn't wipe the controller's name.
# Placeholder serials cheap devices report (0000000000, 12345678, 0x0000) aren't: every
# "0x0000" in lspci would go. So: a digit, 5+ characters, not one repeated character,
# and 8+ digits when it's only digits.
_LEARNED_SERIAL = re.compile(r"(?!(?:0x)?(.)\1*$)(?!\d{1,7}$)(?!(?:0x)?0+$)(?!0?123456789?0?$)(?=[^\s]*\d)[A-Za-z0-9_-]{5,}")
# Files whose whole content is a serial (sysfs), kept only as <serial> in a recording.
SERIAL_FILES = ("/serial", "/unique_id", "/serial_number")
# A battery's manufacture date and cycle count (power_supply uevents: the SMC battery's, as
# POWER_SUPPLY_MANUFACTURE_YEAR=...): with the model they narrow a battery down to a few.
BATTERY_HISTORY = re.compile(r"(?i)(?<![\w.-])((?:POWER_SUPPLY_)?(?:MANUFACTURE_(?:YEAR|MONTH|DAY)|CYCLE_COUNT)\s*[:=]\s*)(?!<)(\S+)")
SENSOR_SERIAL = re.compile(r"\b(found sensor \d+ )(?!<)([A-Za-z0-9]{8,})")

# Names people give their devices ("Marcelo's AirPods", "Kestrel's MacBook Pro"): the
# whole name is learned and replaced by <device-name> wherever it appears (learn()).
# From: bluetoothctl's "Device <address> NAME" lines, and its "Name:"/"Alias:" lines in
# the same output; any device.alias property; PipeWire and PulseAudio descriptions and
# nicks of Bluetooth (bluez) devices and nodes; hostnamectl's pretty hostname.
_BT_ADDRESS = rf"{_HEX}{{2}}(?::{_HEX}{{2}}){{5}}"
# bluetoothctl's property lines ("[CHG] Device <address> RSSI: -60") aren't names.
_BT_PROPERTY = r"(?:Name|Alias|Connected|Paired|Bonded|Trusted|Blocked|RSSI|TxPower|Class|Icon|UUIDs?|Modalias|ManufacturerData|ServiceData|LegacyPairing|WakeAllowed|ServicesResolved|Battery Percentage|Powered|Discoverable|Pairable|Discovering|Key)"
_BT_DEVICE_LINE = re.compile(rf"(?m)^[ \t]*(?:\[[A-Z]+\][ \t]*)?(?:Device|Controller)[ \t]+{_BT_ADDRESS}[ \t]+(?!{_BT_PROPERTY}:)(.+?)(?:[ \t]+\[default\])?[ \t]*$")
_BT_INFO_NAME = re.compile(r"^[ \t]+(?:Name|Alias):[ \t]*(.+?)[ \t]*$")
_BT_HEADER = re.compile(rf"^[ \t]*(?:\[[A-Z]+\][ \t]*)?(?:Device|Controller)[ \t]+{_BT_ADDRESS}\b")
_BT_CHANGED_NAME = re.compile(rf"(?m)^[ \t]*(?:\[[A-Z]+\][ \t]*)?(?:Device|Controller)[ \t]+{_BT_ADDRESS}[ \t]+(?:Name|Alias):[ \t]*(.+?)[ \t]*$")
_BT_ADDRESS_KIND = re.compile(r"\((?:public|random|static|private)\)")
# A quoted property value, apostrophes and escaped quotes included ("Marcelo's AirPods").
_QUOTED = r"""(?:"((?:\\.|[^"\\\n])+)"|'((?:\\.|[^'\\\n])+)')"""
_ALIAS_PROPERTY = re.compile(rf"""["']?\bdevice\.alias["']?\s*(?:=|:)\s*{_QUOTED}""")
_BLUEZ_NAME_PROPERTY = re.compile(
    rf"""(?m)["']?\b(?:device|node)\.(?:description|nick|alias)["']?\s*(?:=|:)\s*{_QUOTED}|^[ \t]+Description:[ \t]*(.+?)[ \t]*$"""
)
_PRETTY_HOSTNAME = re.compile(r"""(?mi)^[ \t]*(?:Pretty hostname:[ \t]*|PRETTY_HOSTNAME=["']?)(.+?)["']?[ \t]*$""")
_BLOCK_START = re.compile(r"(?m)^(?=\S)|^\s*\{\s*$")  # pactl's "Card #52", pw-dump's objects
_MIN_NAME = 3
# Stock names no one chose, left alone so "Keyboard" or "Apple Inc." elsewhere stays readable.
_STOCK_NAMES = {
    "keyboard", "mouse", "trackpad", "speaker", "speakers", "headphones", "headset", "earbuds", "audio", "controller",
    "airpods", "airpods pro", "airpods max", "magic keyboard", "magic mouse", "magic trackpad", "apple inc.", "apple",
    "macbook", "macbook pro", "macbook air", "mac mini", "mac studio", "imac", "iphone", "ipad", "bluetooth",
}
# Hex dumps (edid-decode's raw EDID: 16 bytes a line) hold serials as bytes; each run of
# such lines becomes one HEX_DUMP_NOTE.
_HEX_DUMP_LINE = re.compile(rf"^[ \t]*(?:{_HEX}+:[ \t]*)?(?:{_HEX}{{2}}[ \t]+){{7,}}{_HEX}{{2}}[ \t]*$")
HEX_DUMP_NOTE = "[hex dump removed]"

# (pattern, replacement) in the order they are applied. Replacements only
# use characters none of the later patterns can match.
_RULES: list[tuple[re.Pattern, Any]] = []


def _rule(pattern: str, replacement: Any, flags: int = 0) -> None:
    _RULES.append((re.compile(pattern, flags), replacement))


def _keep_unless_system(group: int, placeholder: str, prefix_groups: tuple[int, ...] = (1,), suffix_groups: tuple[int, ...] = ()):
    def replace(m: re.Match) -> str:
        name = m.group(group)
        if name.lower() in _SYSTEM_USERS or name.startswith("systemd-") or name.startswith("<"):
            return m.group(0)
        return "".join(m.group(g) or "" for g in prefix_groups) + placeholder + "".join(m.group(g) or "" for g in suffix_groups)
    return replace


def _email(m: re.Match) -> str:
    if _UNIT_SUFFIX.search(m.group(2)):
        return m.group(0)  # systemd template units such as user@1000.service
    if _MODE.fullmatch(m.group(0)):
        return m.group(0)  # a display mode such as hyprctl's 3440x1440@59.97300
    return "<email>"


_MODE = re.compile(r"\d+x\d+@\d+(?:\.\d+)?(?:Hz)?")


# Home-directory paths: the account name and everything under it.
_rule(r"(?<![\w.~-])(?:/home|/Users|/var/home)/[^\s'\"`:;,()\[\]{}<>]+", "<home>")
_rule(r"(?<![\w.-])/root/[^\s'\"`:;,()\[\]{}<>]*", "<home>")
_rule(r"(?<![\w/])~/[^\s'\"`:;,()\[\]{}<>]*", "<home>")
# E-mail and user@host addresses.
_rule(r"(?<![\w.%+-])([A-Za-z0-9._%+-]+)@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)", _email)
# Device-tree properties that carry per-unit values (dtc output).
_rule(r"(?i)\b((?:local-)?(?:mac|bd)-address|[\w,-]*serial-?(?:number|no)[\w,-]*|[\w,-]*uuid|[\w,-]*nonce|[\w,-]*ecid|mlb-[\w-]+)(\s*=\s*)(\[[^\]]*\]|\"[^\"]*\"(?:\s*,\s*\"[^\"]*\")*|<[^>]*>)", r"\1\2<redacted>")
# Serial-like fields in any key-value or JSON form: hyprctl monitors ("serial": "9RKXZN3",
# serial: 9RKXZN3), EDID decoders (Serial Number: 1112231500 (0x424b4c4c)), PipeWire and
# PulseAudio properties (device.serial = "..."), udev (ID_USB_SERIAL=...), Thunderbolt
# (unique_id). The value is replaced whole, quoted or bare; the values found are also
# removed wherever else they appear in the same text (Scrubber.scrub).
_rule(SERIAL_FIELD, r"\1\2\3<serial>")
_rule(BATTERY_HISTORY.pattern, r"\1<redacted>")
# Serial numbers in tool output (lsusb, SerialNumber:, serial=...).
_rule(r"(?i)\b(serial[ _-]?(?:number|no|num)|ID_SERIAL(?:_SHORT)?)(\s*[:=]\s*)(?!['<])(\"?)([^\s\",;]+)\3", r"\1\2<serial>")
_rule(r"\b(iSerial\s+\d+\s+)(\S.*)$", r"\1<serial>")
# The camera module's serial, as the ISP driver logs it ("apple-isp ...: found sensor 558 CC2411719QXRWTVDC on ch 0").
_rule(SENSOR_SERIAL, r"\1<serial>")
# Runs of 32-bit hex words (the ISP's command dumps spell the camera module's serial in them as ASCII).
_rule(rf"(?<![0-9A-Za-z])(?:{_HEX}{{8}}[ \t]+){{3,}}{_HEX}{{8}}(?![0-9A-Za-z])", "<hex words>")
# Bluetooth addresses with underscores (BlueZ D-Bus paths dev_7C_C1_..., PipeWire bluez_output.7C_C1_...).
_rule(rf"(?<![0-9A-Fa-f]){_HEX}{{2}}(?:_{_HEX}{{2}}){{5}}(?!_?[0-9A-Fa-f])", "<mac>")
# Disk identifiers: /dev/disk/by-* names and FAT volume ids (UUID=ABCD-1234).
_rule(r"(/dev/disk/by-(?:uuid|partuuid|id|label|partlabel|diskseq|path)/)[^\s'\"`:;,()\[\]{}<>]+", r"\1<disk>")
_rule(r"(?i)\b((?:PART)?UUID=\"?)[0-9A-F]{4}-[0-9A-F]{4}\b", r"\1<disk>")
# SSH key fingerprints.
_rule(r"\b(SHA256|MD5):[A-Za-z0-9+/:=]{16,}", r"\1:<fingerprint>")
# UUIDs (disk, partition, connection and boot identifiers).
_rule(rf"(?<![0-9A-Fa-f]){_HEX}{{8}}-{_HEX}{{4}}-{_HEX}{{4}}-{_HEX}{{4}}-{_HEX}{{12}}(?![0-9A-Fa-f])", "<uuid>")
# MAC and Bluetooth addresses, and longer byte runs that embed them
# (firewall logs print MAC=<dst>:<src>:<ethertype>).
_rule(rf"(?<![0-9A-Fa-f:-]){_HEX}{{2}}([:-])(?:{_HEX}{{2}}\1){{4,}}{_HEX}{{2}}(?![0-9A-Fa-f]|[:-]{_HEX})", "<mac>")
# IPv6 (full or compressed; must contain a hex group, so C++ "::" survives).
_rule(
    rf"(?<![\w:.])(?:(?:{_H16}:){{7}}{_H16}|(?:{_H16}(?::{_H16}){{0,6}})?::(?:{_H16}(?::{_H16}){{0,6}})?(?<=[0-9A-Fa-f]))(?:%[\w.-]+)?(?![\w:])",
    "<ip>",
)
# IPv4, but not inside longer dotted versions such as 23.20.95.0.40.50.92.
_rule(r"(?<![\w.-])(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?![\w]|\.\d)", "<ip>")
# Long hex identifiers (boot and machine ids, hashes, kernel pointers).
_rule(rf"(?<![0-9A-Za-z])(?:0x)?{_HEX}{{16,}}(?![0-9A-Za-z])", "<hex>")
# Wi-Fi network names in NetworkManager, iwd, wpa_supplicant, iw and nmcli output.
_rule(r"(?i)\b(e?ssid)(\s*[:=]?\s*)(['\"])(.*?)\3", r"\1\2\3<ssid>\3")
_rule(r"(?i)\b(e?ssid)(\s*[:=]\s*)(?!['\"<])([^,\n]*?[^,\s])(?=,|\s*$|\s+[\w-]+[:=])", r"\1\2<ssid>")
_rule(r"(?i)\b(connection|access point|network|connected to|to network|for network|known network)(\s+)'([^'\n]*)'",
      _quoted_network)
_rule(r"^(\s*ssid )(\S.*)$", r"\1<ssid>")
_rule(r"(?i)\b((?:connected|connecting|joined|joining) to network\s+|Wireless network\s+)(?!['<])(\S+)", r"\1<ssid>")
_rule(r"(policy: set )'[^'\n]*'", r"\1'<ssid>'")
_rule(r"(audit: op=\"connection[\w-]*\".*?\bname=)\"[^\"]*\"", r'\1"<ssid>"')
_rule(r"(?m)^(\s*[\w.-]+:wifi:[\w ()-]+:)(?!<ssid>)(.+)$", r"\1<ssid>")
_rule(r"(?m)^(\s*Connected network\s+)(\S.*?)\s*$", r"\1<ssid>")
_rule(r"(/var/lib/iwd/)[^/\s]+?(\.(?:psk|open|8021x))\b", r"\1<ssid>\2")
_rule(r"(system-connections/)[^/\s'\"]+", r"\1<ssid>")
# nmcli's own list of connections (NAME:TYPE): a Wi-Fi network or VPN is scrubbed there whatever its name,
# even one too short to learn and remove elsewhere ("lo").
_rule(r"(?m)^(?!<ssid>:)(?:[^\n:\\]|\\.)+(:(?:802-11-wireless|wifi|vpn|wireguard|wifi-p2p))$", r"<ssid>\1")
# Hostnames: journal and syslog line prefixes, uname -a, host=... and hostnamed.
_rule(rf"(?m)^(\s*(?:{_FULL_TIME}|{_SYSLOG_TIME}|{_ISO_TIME})\s+)(?!<)([A-Za-z0-9][A-Za-z0-9.-]*)(\s+[^\s\[\]:]+(?:\[\d+\])?:)", r"\1<hostname>\3")
_rule(r"(?m)^(\s*\[\s*\d+\.\d+\]\s+)(?!<)([A-Za-z0-9][A-Za-z0-9.-]*)(\s+(?:kernel|[^\s\[\]:]+\[\d+\]):)", r"\1<hostname>\3")
_rule(r"(?m)^(\s*Linux )(\S+)( \d+\.\d+)", r"\1<hostname>\3")
_rule(r"(?i)\b((?:static |transient |pretty )?host(?:_?name)?)(\s*(?:=>|[:=])\s*)(['\"]?)(?!<)([A-Za-z0-9][\w.-]*)\3", r"\1\2\3<hostname>\3")
_rule(r"(?i)\b(hostname (?:set )?to\s+)<?(?!hostname>)[\w.-]+>?", r"\1<hostname>")
# Usernames: sudo, PAM, logind, environment-style keys, uid=N(name).
_rule(rf"(?m)^(.*?\s)({_USER})(\s+:\s+(?=(?:TTY|PWD|USER|COMMAND)=))", _keep_unless_system(2, "<user>", (1,), (3,)))
_rule(rf"\b((?:USER|LOGNAME|SUDO_USER|user|ruser|acct)=\"?)({_USER})", _keep_unless_system(2, "<user>", (1,)))
_rule(rf"(?i)\b((?:for|of) user\s+'?)({_USER})", _keep_unless_system(2, "<user>", (1,)))
_rule(rf"\b(by\s+)({_USER})(\(uid=)", _keep_unless_system(2, "<user>", (1,), (3,)))
_rule(rf"\b(uid=\d+\()({_USER})(\))", _keep_unless_system(2, "<user>", (1,), (3,)))
_rule(rf"(?i)\b(new session \S+ of user\s+|Accepted \S+ for\s+|Invalid user\s+)({_USER})", _keep_unless_system(2, "<user>", (1,)))


class Scrubber:
    """Replaces personal identifiers in text with placeholders."""

    def __init__(self, hostnames: Iterable[str] = (), users: Iterable[str] = (), networks: Iterable[str] = ()):
        # Hostnames match in any case (DNS ignores case); account and network
        # names only exactly, so a user called "max" leaves "M2 Max" alone.
        hints = []
        for values, placeholder, flags in (
            (networks, "<ssid>", 0),
            (hostnames, "<hostname>", re.IGNORECASE),
            (users, "<user>", 0),
        ):
            for value in values:
                value = value.strip()
                if len(value) < 3 or value.lower() in _NOT_PERSONAL or value.startswith("<"):
                    continue
                hints.append((value, placeholder, flags))
        # Longest first, so "omarchy-m2-max" goes before a network named "omarchy-m2".
        hints.sort(key=lambda hint: -len(hint[0]))
        self._hints = [
            (re.compile(rf"(?<![\w-]){re.escape(value)}(?![\w-])", flags), placeholder)
            for value, placeholder, flags in hints
        ]
        self._hint_values = {value.lower() for value, _, _ in hints}
        self._learned: dict[str, str] = {}  # value -> placeholder
        self._learned_patterns: list[tuple[re.Pattern, str]] = []

    @classmethod
    def for_host(cls, host: Host) -> "Scrubber":
        """Learn this machine's hostname, accounts and saved network names."""
        hostnames: list[str] = []
        try:
            hostnames = [host.read_file(HOSTNAME_PATH).decode("utf-8", "replace").strip()]
        except OSError:
            pass
        try:
            users = [name for name in host.list_dir(HOME_DIR) if not name.startswith(".")]
        except OSError:
            users = []
        saved = host.run(SAVED_CONNECTIONS)
        networks = saved_connection_names(saved.stdout) if saved.returncode == 0 else []
        return cls(hostnames=hostnames, users=users, networks=networks)

    def learn(self, text: str) -> None:
        """Remember the serials and device names in `text`, to remove them wherever they appear.

        Serials: the values of serial-like fields (hyprctl repeats a monitor's
        serial in its description, "Dell Inc. DELL U3423WE 9RKXZN3"; PipeWire a
        USB card's in its name); only identifier-shaped ones (_LEARNED_SERIAL).
        Device names: see _BT_DEVICE_LINE and the rules after it. scrub()
        learns from the text it is given; a caller scrubbing many texts (a
        recording, a report's evidence) learns from all of them first.
        """
        serials = [m.group(4) for m in SERIAL_FIELD.finditer(text) if _LEARNED_SERIAL.fullmatch(m.group(4))]
        serials += [m.group(2) for m in SENSOR_SERIAL.finditer(text)]
        self.remember(serials=serials, names=_device_names(text))

    def remember(self, serials: Iterable[str] = (), names: Iterable[str] = ()) -> None:
        added = False
        for values, placeholder in ((serials, "<serial>"), (names, "<device-name>")):
            for value in values:
                value = value.strip()
                if value and value not in self._learned and not value.startswith("<") and (placeholder == "<serial>" or self._is_name(value)):
                    self._learned[value] = placeholder
                    added = True
        if added:
            self._learned_patterns = [
                (re.compile(rf"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])"), placeholder)
                for value, placeholder in sorted(self._learned.items(), key=lambda item: -len(item[0]))
            ]

    def _is_name(self, value: str) -> bool:
        """A device name worth learning: long enough, not stock, and not a hostname, account or network already hinted."""
        lower = value.lower()
        return len(value) >= _MIN_NAME and lower not in _NOT_PERSONAL and lower not in _STOCK_NAMES and lower not in self._hint_values

    @property
    def serials(self) -> list[str]:
        """The serial values learned so far, for a checkpoint to hand the next run (remember())."""
        return sorted(value for value, placeholder in self._learned.items() if placeholder == "<serial>")

    @property
    def names(self) -> list[str]:
        """The device names learned so far, for a checkpoint to hand the next run (remember())."""
        return sorted(value for value, placeholder in self._learned.items() if placeholder == "<device-name>")

    def scrub(self, text: str) -> str:
        """Scrub each line on its own, so no pattern can reach across lines (serials and names learned from the whole text).

        A run of hex dump lines becomes one HEX_DUMP_NOTE.
        """
        self.learn(text)
        lines: list[str] = []
        for line in text.splitlines(keepends=True):
            if _HEX_DUMP_LINE.match(line.rstrip("\r\n")):
                note = HEX_DUMP_NOTE + line[len(line.rstrip("\r\n")):]
                if not (lines and lines[-1].rstrip("\r\n") == HEX_DUMP_NOTE):
                    lines.append(note)
                continue
            lines.append(self._scrub_line(line))
        return "".join(lines)

    def _scrub_line(self, line: str) -> str:
        body = line.rstrip("\r\n")
        end = line[len(body):]
        # Learned values first: a whole device name ("Kestrel's MacBook Pro") before a rule takes part of it.
        for pattern, placeholder in self._learned_patterns:
            body = pattern.sub(placeholder, body)
        for pattern, replacement in _RULES:
            body = pattern.sub(replacement, body)
        for pattern, placeholder in self._hints:
            body = pattern.sub(placeholder, body)
        return body + end


def saved_connection_names(stdout: str) -> list[str]:
    """The names to learn from `nmcli --get-values NAME,TYPE connection show` (NAME:TYPE, a colon in NAME as \\:)."""
    names = []
    for line in stdout.splitlines():
        name, colon, kind = line.rpartition(":")
        if not colon:
            name, kind = line, ""
        name = re.sub(r"\\(.)", r"\1", name)
        kind = kind.strip()
        if kind in _INTERFACE_CONNECTION_TYPES or (kind not in _NAMED_NETWORK_TYPES and _INTERFACE_CONNECTION_NAME.fullmatch(name)):
            continue
        names.append(name)
    return names


def _device_names(text: str) -> list[str]:
    """The user-given device names in a tool's output (see _BT_DEVICE_LINE)."""
    names = [m.group(1) for m in _BT_DEVICE_LINE.finditer(text) if not _BT_ADDRESS_KIND.fullmatch(m.group(1))]
    names += [m.group(1) for m in _BT_CHANGED_NAME.finditer(text)]
    # bluetoothctl info / show: the indented lines under a "Device <address>" header.
    in_device = False
    for line in text.splitlines():
        if _BT_HEADER.match(line):
            in_device = True
        elif in_device and line[:1] in (" ", "\t"):
            found = _BT_INFO_NAME.match(line)
            if found:
                names.append(found.group(1))
        else:
            in_device = False
    names += [m.group(1) or m.group(2) for m in _ALIAS_PROPERTY.finditer(text)]
    names += [m.group(1) for m in _PRETTY_HOSTNAME.finditer(text)]
    if "bluez" in text:
        for block in _BLOCK_START.split(text):
            if "bluez" in block:
                names += [m.group(1) or m.group(2) or m.group(3) for m in _BLUEZ_NAME_PROPERTY.finditer(block)]
    return names


def _is_text(line: str) -> bool:
    return "\ufffd" not in line and not any(ord(c) < 32 and c != "\t" for c in line)


def _evidence_line(line: Any, scrubber: Scrubber) -> str:
    if not isinstance(line, str) or not _is_text(line):
        return NON_TEXT_NOTE
    line = scrubber.scrub(line)
    if len(line) > EVIDENCE_LINE_MAX_CHARS:
        line = line[: EVIDENCE_LINE_MAX_CHARS - 3] + "..."
    return line


def _allowed(value: Any, allowlist: Any) -> Any:
    if allowlist is True:
        return value
    if isinstance(allowlist, list):
        return [_allowed(item, allowlist[0]) for item in value] if isinstance(value, list) else []
    if isinstance(allowlist, dict) and isinstance(value, dict):
        return {key: _allowed(value[key], allowlist[key]) for key in allowlist if key in value}
    return {}


def scrub_check(check: dict, scrubber: Scrubber) -> dict:
    """One check result with its evidence scrubbed, as the checkpoint keeps it (enforce() still runs on the report)."""
    evidence = check.get("evidence")
    if not isinstance(evidence, list):
        return dict(check)
    scrubber.learn("\n".join(line for line in evidence if isinstance(line, str)))
    return {**check, "evidence": _collapse_hex_notes([_evidence_line(line, scrubber) for line in evidence])}


def _collapse_hex_notes(lines: list[str]) -> list[str]:
    """One HEX_DUMP_NOTE for a run of hex dump lines given as separate evidence lines."""
    return [line for i, line in enumerate(lines) if not (line == HEX_DUMP_NOTE and i and lines[i - 1] == HEX_DUMP_NOTE)]


def enforce(report: dict, scrubber: Scrubber) -> dict:
    """The report with only allowlisted fields and scrubbed, bounded, text-only evidence."""
    report = _allowed(report, REPORT_ALLOWLIST)
    machine = report.get("machine", {})
    for name in _SCRUBBED_MACHINE_FIELDS:
        if isinstance(machine.get(name), str):
            machine[name] = scrubber.scrub(machine[name])

    for check in report.get("checks", []):
        if isinstance(check.get("evidence"), list):
            scrubber.learn("\n".join(line for line in check["evidence"] if isinstance(line, str)))
    # The truncation note is always reserved, so the total never exceeds the budget.
    budget = EVIDENCE_BUDGET_BYTES - len(TRUNCATED_NOTE.encode("utf-8"))
    exhausted = False
    for check in report.get("checks", []):
        if "evidence" not in check:
            continue
        lines = check["evidence"] if isinstance(check["evidence"], list) and not exhausted else []
        lines = _collapse_hex_notes([_evidence_line(line, scrubber) for line in lines])
        if len(lines) > EVIDENCE_LINES_PER_CHECK:
            dropped = len(lines) - (EVIDENCE_LINES_PER_CHECK - 1)
            lines = lines[: EVIDENCE_LINES_PER_CHECK - 1] + [f"[{dropped} more lines not included]"]
        kept = []
        for line in lines:
            size = len(line.encode("utf-8"))
            if size > budget:
                kept.append(TRUNCATED_NOTE)
                exhausted = True
                break
            kept.append(line)
            budget -= size
        check["evidence"] = kept
    return report


def evidence_bytes(report: dict) -> int:
    """Total UTF-8 size of a report's evidence lines."""
    return sum(len(line.encode("utf-8")) for check in report.get("checks", []) for line in check.get("evidence", []))
