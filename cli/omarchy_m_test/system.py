"""What this Mac runs: the Omarchy stack or reference distro, its packages,
disk encryption and candidate set. It becomes the report's "system" block.

Stacks, decided in this order:
  converged           the omarchy-mac package, or Omarchy's own omarchy package
                      (converged images, quattro-upstream installs); omacom's
                      omarchy-dev comes with omarchy-mac, so it stays converged
  mx-mac              the omarchy-mx-mac fork's omarchy-dev without omarchy-mac
  legacy-omarchy-mac  no Omarchy package, but omarchy-mac-keyring or a checkout's
                      omarchy-version on PATH
  reference           another distro on the Mac (Fedora Asahi Remix, Asahi ALARM, ...)

Only package names and versions, the os-release ID, the kinds of block devices
under the root filesystem and the image's own build records (allowlisted keys
of its target record) are read: never device names, UUIDs or key slots.

The build a run is on (build_identity) is derived from those: the Omarchy
runtime commit and the candidate-set stamp its packages carry, plus the boot
package and Aurora kernel versions. The site derives the same (Build).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .host import Host

OS_RELEASE = "/etc/os-release"
# The image-target manifest a converged image's builder writes; the first
# boot retires it to target.booted (omarchy-mac's install/helpers/image-target.sh).
# Its format ignores unknown keys, so the image builder records its provenance
# there (candidate_set=, builder_commit=, image_profile=, ...); older images
# only have format= and platform=. The factory seal, on the root after a
# factory reset, names the set too.
TARGET_RECORDS = ("/var/lib/omarchy/image/target", "/var/lib/omarchy/image/target.booted")
FACTORY_SEAL = "/var/lib/omarchy/factory-sealed"

STACK_PACKAGES = (
    "omarchy", "omarchy-dev", "omarchy-settings", "omarchy-settings-dev", "omarchy-mac", "omarchy-mac-boot", "omarchy-mac-keyring",
)
BOOT_PACKAGES = ("linux-aurora", "linux-asahi", "m1n1", "m1n1-aurora", "uboot-asahi", "limine")
# What Omarchy's hardware setup installs for the GPU and the speakers.
HARDWARE_PACKAGES = ("mesa", "vulkan-asahi", "asahi-audio", "speakersafetyd", "alsa-ucm-conf-asahi")
# Aurora images ship their own builds of four of them (mesa-aurora, vulkan-aurora, aurora-audio,
# alsa-ucm-conf-aurora). Each provides the Asahi name, so pacman -Q answers that name with the Aurora package:
# either name of an entry counts.
HARDWARE_REQUIREMENTS = (
    ("mesa", "mesa-aurora"), ("vulkan-asahi", "vulkan-aurora"), ("asahi-audio", "aurora-audio"), ("speakersafetyd",),
    ("alsa-ucm-conf-asahi", "alsa-ucm-conf-aurora"),
)
PACKAGES = STACK_PACKAGES + BOOT_PACKAGES + HARDWARE_PACKAGES
# The names pacman -Q can answer the query with.
ANSWERED = PACKAGES + tuple(name for names in HARDWARE_REQUIREMENTS for name in names[1:])
PACKAGE_QUERY = ["pacman", "-Q", *PACKAGES]
OMARCHY_VERSION = ["omarchy-version"]
ROOT_SOURCE = ["findmnt", "--noheadings", "--output", "SOURCE", "/"]

STACK_WORDS = {
    "converged": "Omarchy, converged image",
    "mx-mac": "Omarchy, mx-mac",
    "legacy-omarchy-mac": "Omarchy, legacy omarchy-mac",
    "reference": "reference run",
}

# The image target record's keys that reach the report (system.image), each
# with the only shape its value may have: the image builder's own record of
# the build (omacom/omarchy-mac-installer's build-mac-image), what the image
# is, never who has it. Older images have only format and platform. Other
# keys are ignored; the schema lists exactly these.
IMAGE_KEYS = {
    "format": re.compile(r"[0-9]{1,4}"),
    "platform": re.compile(r"[a-z0-9][a-z0-9-]{0,39}"),
    "candidate_set": re.compile(r"[A-Za-z0-9._-]{1,128}"),
    "candidate_source_commit": re.compile(r"[0-9a-f]{7,40}"),
    "builder_commit": re.compile(r"[0-9a-f]{7,40}"),
    "builder_tree_clean": re.compile(r"(true|false|yes|no)"),
    "image_profile": re.compile(r"[a-z]{1,20}"),
    "package_set_sha256": re.compile(r"[0-9a-f]{64}"),
    "built": re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"),
}

_TAG = re.compile(r"[A-Za-z0-9._-]{1,128}")
# In a package version: the git commit of a VCS build (".g99ace4070354")
# and the pkgrel's build stamp ("-1.2026092602": what the candidate-set lane
# appends to every package it builds, the same across one build's packages).
_COMMIT = re.compile(r"\.g([0-9a-f]{7,40})(?![0-9a-f])")
_STAMP = re.compile(r"-\d+\.(\d+)$")
RUNTIME_PACKAGES = ("omarchy", "omarchy-settings", "omarchy-dev")
_DISTRO = re.compile(r"[a-z0-9][a-z0-9._-]{0,39}")


@dataclass(frozen=True)
class System:
    stack: str
    distro: str
    packages: dict[str, str]  # installed package -> version, in PACKAGES order
    has_pacman: bool
    candidate_set: str | None
    image: dict[str, str]  # allowlisted keys of the image's target record; empty without one
    encryption: str  # on, off or unknown
    root_chain: tuple[str, ...]  # "TYPE FSTYPE" from the root filesystem down to its disk

    @property
    def is_omarchy(self) -> bool:
        return self.stack != "reference"

    def describe(self) -> str:
        words = STACK_WORDS[self.stack]
        if self.stack == "reference":
            words += f" on {self.distro}"
        if self.candidate_set:
            words += f", candidate set {self.candidate_set}"
        return words

    def build(self) -> str | None:
        """The build this run is on, e.g. "f60e1ba.2026092602 (linux-aurora 7.1.12.aurora2-11, omarchy-mac-boot 20260926-1)"
        or, on an image naming its set, "apple-test-f22c43fb7903-20260928 (runtime f22c43f.…, …, built 2026-09-28T03:04:05Z)"."""
        return build_words(self.packages, self.candidate_set, self.image) if self.is_omarchy else None

    def report(self, boot_loader: str) -> dict:
        block = {
            "stack": self.stack,
            "distro": self.distro,
            "boot_loader": boot_loader,
            "encryption": self.encryption,
            "packages": [{"name": name, "version": version} for name, version in self.packages.items()],
        }
        if self.candidate_set:
            block["candidate_set"] = self.candidate_set
        if self.image:
            block["image"] = dict(self.image)
        return block


def build_parts(packages: dict[str, str]) -> tuple[str | None, str | None]:
    """The runtime's commit (7 characters) and build stamp, from its package versions; either may be None."""
    runtime = next((packages[name] for name in RUNTIME_PACKAGES if name in packages), None)
    commit = _COMMIT.search(runtime) if runtime else None
    stamp = next((m.group(1) for name in (*RUNTIME_PACKAGES, "omarchy-mac")
                  if name in packages and (m := _STAMP.search(packages[name]))), None)
    return (commit.group(1)[:7] if commit else None), stamp


def build_id(packages: dict[str, str]) -> str | None:
    """The runtime build, e.g. "f60e1ba.2026092602"; a release package's own version ("4.0.2-1") when it
    carries neither a commit nor a stamp; None without an Omarchy package."""
    found = ".".join(part for part in build_parts(packages) if part)
    return found or next((packages[name] for name in (*RUNTIME_PACKAGES, "omarchy-mac") if name in packages), None)


def build_words(packages: dict[str, str], candidate_set: str | None = None, image: dict | None = None) -> str | None:
    """The build a run is on, as the site words it (Build#words): the image's candidate set when it names one,
    else build_id; then the runtime build (under a set), the kernel and boot package, without the build's
    stamp, and when the image was built and its package set digest (12 characters), which together name the
    image's release asset."""
    identity = build_id(packages)
    if identity is None:
        return None
    stamp = build_parts(packages)[1]
    extras = [f"runtime {identity}"] if candidate_set else []
    for name in ("linux-aurora", "omarchy-mac-boot"):
        version = packages.get(name)
        if version:
            if stamp and version.endswith(f".{stamp}"):
                version = version[: -len(stamp) - 1]
            extras.append(f"{name} {version}")
    image = image or {}
    if image.get("built"):
        extras.append(f"built {image['built']}")
    if image.get("package_set_sha256"):
        extras.append(f"package set {image['package_set_sha256'][:12]}")
    return (candidate_set or identity) + (f" ({', '.join(extras)})" if extras else "")


def detect(host: Host) -> System:
    query = host.run(PACKAGE_QUERY)
    has_pacman = query.returncode != 127
    installed = {}
    for line in query.stdout.splitlines() if has_pacman else []:
        parts = line.split()
        if len(parts) == 2 and parts[0] in ANSWERED:
            installed[parts[0]] = parts[1]
    packages = {name: installed[name] for name in ANSWERED if name in installed}

    if "omarchy-mac" in packages or "omarchy" in packages:
        stack = "converged"
    elif "omarchy-dev" in packages:
        stack = "mx-mac"
    elif "omarchy-mac-keyring" in packages or host.run(OMARCHY_VERSION).returncode == 0:
        stack = "legacy-omarchy-mac"
    else:
        stack = "reference"

    chain = _root_chain(host)
    if chain is None:
        encryption = "unknown"
    elif any(line.split()[0] == "crypt" or "crypto_LUKS" in line.split()[1:] for line in chain):
        encryption = "on"
    else:
        encryption = "off"

    return System(
        stack=stack,
        distro=_distro(host),
        packages=packages,
        has_pacman=has_pacman,
        candidate_set=_candidate_set(host),
        image=_image(host),
        encryption=encryption,
        root_chain=tuple(chain or ()),
    )


def _key_values(host: Host, path: str) -> dict[str, str] | None:
    try:
        text = host.read_file(path).decode("utf-8", "replace")
    except OSError:
        return None
    values = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip().strip("\"'")
    return values


def _distro(host: Host) -> str:
    distro = (_key_values(host, OS_RELEASE) or {}).get("ID", "").lower()
    return distro if _DISTRO.fullmatch(distro) else "unknown"


def _candidate_set(host: Host) -> str | None:
    """The candidate set a candidate image was built from, if its target record is present."""
    target = next((values for path in TARGET_RECORDS if (values := _key_values(host, path)) is not None), None)
    if target is None:
        return None
    tag = target.get("candidate_set") or (_key_values(host, FACTORY_SEAL) or {}).get("candidate_set")
    return tag if tag and _TAG.fullmatch(tag) else None


def _image(host: Host) -> dict[str, str]:
    """The allowlisted keys of the image's target record, each only when its value has its key's shape."""
    target = next((values for path in TARGET_RECORDS if (values := _key_values(host, path)) is not None), None) or {}
    return {key: target[key] for key, shape in IMAGE_KEYS.items() if key in target and shape.fullmatch(target[key])}


def _root_chain(host: Host) -> list[str] | None:
    """Block device types under the root filesystem, e.g. ["crypt btrfs", "part crypto_LUKS", "disk"]."""
    source = host.run(ROOT_SOURCE)
    device = source.stdout.strip().splitlines()[0].split("[")[0] if source.returncode == 0 and source.stdout.strip() else ""
    if not device.startswith("/dev/"):
        return None
    chain = host.run(["lsblk", "--noheadings", "--inverse", "--raw", "--output", "TYPE,FSTYPE", device])
    lines = [" ".join(line.split()) for line in chain.stdout.splitlines() if line.strip()]
    return lines if chain.returncode == 0 and lines else None
