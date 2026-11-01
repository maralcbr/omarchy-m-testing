"""Files that ship with the tool itself, never anything on the machine.

The feature catalogue sits next to the package in a release
(omarchy_m_test/catalogue.json) and at catalogue/catalogue.json in the
repository; Asahi's reference kernel config likewise
(omarchy_m_test/asahi-kernel/ and catalogue/asahi-kernel/). omarchy-mac's check scripts are vendored under
omarchy_m_test/vendor/omarchy-mac/ (see ORIGIN there), and the tool's own
on-screen video check script under omarchy_m_test/testcard/; the real host runs
them by name (Host.run_bundled). This is the only module besides the hosts that
touches files (scripts/check_boundary.py allows it), and only these paths.
"""

from __future__ import annotations

import os

_PACKAGE = os.path.dirname(os.path.abspath(__file__))
CATALOGUE_PATHS = (
    os.path.join(_PACKAGE, "catalogue.json"),
    os.path.join(os.path.dirname(os.path.dirname(_PACKAGE)), "catalogue", "catalogue.json"),
)
ASAHI_KERNEL_DIRS = (
    os.path.join(_PACKAGE, "asahi-kernel"),
    os.path.join(os.path.dirname(os.path.dirname(_PACKAGE)), "catalogue", "asahi-kernel"),
)
ASAHI_KERNEL_FILES = ("source.json", "config")
MACOS_REFERENCE_DIRS = (
    os.path.join(_PACKAGE, "macos-reference"),
    os.path.join(os.path.dirname(os.path.dirname(_PACKAGE)), "catalogue", "macos-reference"),
)


VENDOR = os.path.join(_PACKAGE, "vendor", "omarchy-mac")
TESTCARD = os.path.join(_PACKAGE, "testcard")
# Bundled script name -> its file.
SCRIPTS = {
    "mac-check": os.path.join(VENDOR, "mac-check"),
    "apple-audio-check": os.path.join(VENDOR, "apple-audio-check.sh"),
    "apple-display-check": os.path.join(VENDOR, "apple-display-check.sh"),
    "video-card": os.path.join(TESTCARD, "video-card.sh"),  # with testcard.lua beside it
    "gpu-benchmark": os.path.join(_PACKAGE,"gpu_native","run.sh"),
    "gl-spec": os.path.join(_PACKAGE, "gl_spec", "run.sh"),
}


def script_path(name: str) -> str:
    """Where a bundled script is; FileNotFoundError if the tool was installed without it."""
    path = SCRIPTS[name]
    if not os.path.isfile(path):
        raise FileNotFoundError(f"{name} isn't installed next to omarchy-m-test")
    return path


def catalogue_text() -> bytes:
    """The bundled catalogue's bytes; FileNotFoundError if the tool was installed without it."""
    for path in CATALOGUE_PATHS:
        if os.path.isfile(path):
            with open(path, "rb") as f:
                return f.read()
    raise FileNotFoundError("the feature catalogue isn't installed next to omarchy-m-test")


def macos_reference_text(board: str) -> bytes:
    """A board's macOS reference file; FileNotFoundError if there's none. `board` is a validated board id."""
    for base in MACOS_REFERENCE_DIRS:
        path = os.path.join(base, f"{board}.json")
        if os.path.isfile(path):
            with open(path, "rb") as f:
                return f.read()
    raise FileNotFoundError(f"no macOS reference for {board}")


def gpu_document_schema() -> bytes:
    """The tool's own separate benchmark schema; never user configuration."""
    with open(os.path.join(_PACKAGE,"gpu_native","report.schema.json"),"rb") as f:
        return f.read()


def asahi_kernel_config() -> tuple[bytes, bytes]:
    """Asahi's pinned reference kernel config: (source.json, config); FileNotFoundError if not installed."""
    for base in ASAHI_KERNEL_DIRS:
        paths = [os.path.join(base, name) for name in ASAHI_KERNEL_FILES]
        if all(os.path.isfile(path) for path in paths):
            texts = []
            for path in paths:
                with open(path, "rb") as f:
                    texts.append(f.read())
            return texts[0], texts[1]
    raise FileNotFoundError("Asahi's reference kernel config isn't installed next to omarchy-m-test")
