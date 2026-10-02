"""The automatic checks of a run. Each check returns one schema-v1 check result.

One function per section (sections.py): each wraps omarchy-mac's check
scripts (scripts.py) and adds the checks they don't cover (hardware.py).
mac-check covers several sections and runs once, for the first of them that
isn't skipped. On a reference distro the Omarchy-layer results are skipped:
only the hardware is compared with Omarchy.
"""

from __future__ import annotations

from . import audio as live_audio, display as live_display, hardware, inputs, inventory, network as live_network, packages, scripts
from .catalogue import Catalogue
from .machine import Machine
from .session import Context
from .system import System

MAC_CHECK_RESULTS = "mac_check"
DISPLAY_CHECK_RESULTS = "display_check"
AUDIO_CHECK_RESULTS = "audio_check"
BOOT_LOADER = "boot_loader"
INVENTORY = "inventory"

# Every check id, in report order (the sections' order). Each is in the catalogue's checks.
ORDER = (
    "system.identity",
    "boot.kernel-package", "boot.chain", "boot.files", "boot.encryption",
    "packages.repositories", "packages.kernel-updates", "packages.hardware",
    "setup.first-boot-hardware", "setup.vendor-firmware",
    "system.failed-units", "system.snapshots",
    "hardware.drivers", "hardware.firmware", "hardware.probe-errors", "hardware.kernel-config",
    "gpu.driver", "gpu.vulkan", "gpu.opengl",
    "video.h264-on-screen", "video.hevc-on-screen",
    "display.outputs", "display.controller", "display.backlight", "display.notch-strip",
    "display.notch-bar", "display.brightness-steps", "display.cursor",
    "audio.sound-cards", "audio.default-sink", "audio.speaker-dsp", "audio.speaker-protection",
    "audio.speaker-amps-unlocked", "audio.microphone-mapping",
    "audio.microphone-signal", "audio.speaker-tone", "audio.headphone-detection",
    "network.wifi", "network.wifi-backend", "network.bluetooth", "network.bluetooth-pairing", "network.wifi-first-join",
    "sleep.lid-suspend", "sleep.clamshell", "sleep.wifi-after-resume", "sleep.thunderbolt-after-resume",
    "input.ambient-light", "input.auto-keyboard-light", "input.keyboard-light-follows-room",
    "input.function-keys", "input.trackpad-gestures",
    "sep.attach", "touch-id.ready", "touch-id.unlock",
    "camera.isp", "camera.frames", "camera.image",
    "ports.usb-c", "ports.thunderbolt", "ports.external-displays", "ports.devices-work", "ports.external-display-picture",
    "power.battery", "power.charge-limit", "power.charge-limit-kept", "power.idle-draw", "power.sleep-drain",
    "cpu.frequency-scaling",
)


def system_identity(machine: Machine) -> dict:
    """Automatic check: the Mac's model, chip and kernel were identified."""
    evidence = [
        f"model: {machine.model}",
        f"compatible: {' '.join(machine.compatible)}",
        f"chip: {machine.chip} ({machine.soc})",
        f"kernel: {machine.kernel}",
    ]
    identified = machine.chip != "unknown" and machine.kernel != "unknown"
    return {
        "id": "system.identity",
        "kind": "automatic",
        "status": "pass" if identified else "fail",
        "evidence": evidence,
    }


def mac_check(ctx: Context) -> list[dict]:
    """mac-check's results, run once per run whichever section needs them first.

    Its boot-loader line goes in `shared` for the report's system block, so a
    resumed run still has it.
    """
    if MAC_CHECK_RESULTS not in ctx.cache:
        found = scripts.mac_check(ctx.host)
        ctx.cache[MAC_CHECK_RESULTS] = found.results
        ctx.shared[BOOT_LOADER] = found.boot_loader
    return ctx.cache[MAC_CHECK_RESULTS]


def display_check(ctx: Context) -> list[dict]:
    """apple-display-check's results (the Display and Input sections), run once per run."""
    if DISPLAY_CHECK_RESULTS not in ctx.cache:
        ctx.cache[DISPLAY_CHECK_RESULTS] = scripts.display_check(ctx.host, _system(ctx))
    return ctx.cache[DISPLAY_CHECK_RESULTS]


def boot(ctx: Context) -> list[dict]:
    system = _system(ctx)
    return [
        system_identity(ctx.machine),
        *mac_check(ctx),
        hardware.encryption(system),
        hardware.hardware_packages(system),
        hardware.first_boot_setup(ctx.host),
    ]


def hardware_inventory(ctx: Context) -> list[dict]:
    """The inventory and gap map; its report block goes in `shared`, so a resumed run still has it."""
    found = inventory.take(ctx.host, ctx.catalogue, ctx.machine)
    ctx.shared[INVENTORY] = found.report()
    return found.results(ctx.catalogue)


def graphics(ctx: Context) -> list[dict]:
    system = _system(ctx)
    return [hardware.gpu_driver(ctx.host), hardware.gpu_vulkan(ctx.host, system), opengl(ctx)]


def opengl(ctx: Context) -> dict:
    """gpu.opengl, with eglinfo (mesa-utils) installed for the section with consent when it isn't there (packages.py)."""
    found = hardware.gpu_opengl(ctx.host)
    if found["evidence"] != [hardware.NO_EGLINFO]:
        return found
    ready = packages.temporary(ctx, [hardware.EGLINFO_PACKAGE], "The OpenGL check")
    if not ready.ready:
        return {**found, "evidence": [hardware.NO_EGLINFO, f"skipped: {ready.skipped}"]}
    again = hardware.gpu_opengl(ctx.host)
    return {**again, "evidence": [*again["evidence"], *ready.evidence()]}


def display(ctx: Context) -> list[dict]:
    return [*mac_check(ctx), *display_check(ctx), *live_display.display(ctx)]


def audio(ctx: Context) -> list[dict]:
    """The automatic audio checks (which play nothing) first, then the microphone, the tone and the jack."""
    if AUDIO_CHECK_RESULTS not in ctx.cache:  # kept if a live check times out (sections.timed_out)
        ctx.cache[AUDIO_CHECK_RESULTS] = scripts.audio_check(ctx.host, _system(ctx))
    return [*mac_check(ctx), *ctx.cache[AUDIO_CHECK_RESULTS], *live_audio.run(ctx)]


def network(ctx: Context) -> list[dict]:
    """mac-check's Wi-Fi and Bluetooth checks, then pairing a device and the first join after a driver reload."""
    automatic = mac_check(ctx)
    return [*automatic, *live_network.run(ctx, automatic)]


def input_devices(ctx: Context) -> list[dict]:
    """The light sensor and keyboard light (apple-display-check), then the function keys and the trackpad."""
    return [*display_check(ctx), live_display.keyboard_light(ctx), *inputs.run(ctx)]


def cpu(ctx: Context) -> list[dict]:
    return [hardware.cpu_scaling(ctx.host)]


def only(check_ids: tuple[str, ...], results: list[dict], ctx: Context) -> list[dict]:
    """A section's results: its own checks, in its order, compared as a reference run off Omarchy."""
    by_id = {result["id"]: result for result in results}
    ordered = [by_id[check_id] for check_id in check_ids]
    system = _system(ctx)
    if not system.is_omarchy:
        ordered = [_reference(result, system, ctx.catalogue) for result in ordered]
    return ordered


def _system(ctx: Context) -> System:
    assert ctx.system is not None, "sections run with the detected system"
    return ctx.system


def _reference(result: dict, system: System, catalogue: Catalogue) -> dict:
    feature = catalogue.feature_for_check(result["id"])
    if feature is None or feature["layer"] != "omarchy":
        return result
    return {**result, "status": "skip", "evidence": [f"reference run on {system.distro}: Omarchy integration isn't checked"]}
