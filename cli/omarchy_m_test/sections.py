"""The sections of a run on an Apple Silicon Mac, in order.

Each section names the check ids it reports, in report order, so a skipped
section still reports every one of its checks (as skipped). Together they
are checks.ORDER. Titles are the sections' headings (and the picker's
choices): letters and spaces, short.

A command that runs out of time (host.TimedOut) ends its section: the checks
that section's scripts already reported keep their results, every other one
is skipped as timed out. A timeout never fails a check or the run.
"""

from __future__ import annotations

from typing import Callable

from . import benchmarks, camera, checks, ports, power, sleep, touchid, video
from .host import TimedOut
from .session import Context, Section


def _section(id: str, title: str, description: str, check_ids: tuple[str, ...], run: Callable[[Context], list[dict]],
             human_checks: tuple[str, ...] = (), disruptive: bool = False) -> Section:
    def bounded(ctx: Context) -> list[dict]:
        try:
            results = run(ctx)
        except TimedOut as stopped:
            results = timed_out(check_ids, human_checks, stopped, ctx)
        return checks.only(check_ids, results, ctx)

    return Section(id, title, description, check_ids, bounded, human_checks, disruptive)


def timed_out(check_ids: tuple[str, ...], human_checks: tuple[str, ...], stopped: TimedOut, ctx: Context) -> list[dict]:
    """A section a command timed out in: what its scripts reported (mac-check's, cached), the rest skipped."""
    known = {result["id"]: result for key in (checks.MAC_CHECK_RESULTS, checks.DISPLAY_CHECK_RESULTS, checks.AUDIO_CHECK_RESULTS) for result in ctx.cache.get(key, [])}
    why = f"skip: timed out after {stopped.seconds}s ({' '.join(stopped.argv)[:200]}); the rest of this section wasn't run"
    return [
        known.get(check_id) or {"id": check_id, "kind": "human" if check_id in human_checks else "automatic", "status": "skip", "evidence": [why]}
        for check_id in check_ids
    ]


APPLE: tuple[Section, ...] = (
    _section("boot", "Boot", "Model and kernel, boot chain and files, encryption, packages, first-boot setup, services and snapshots.", (
        "system.identity",
        "boot.kernel-package", "boot.chain", "boot.files", "boot.encryption",
        "packages.repositories", "packages.kernel-updates", "packages.hardware",
        "setup.first-boot-hardware", "setup.vendor-firmware",
        "system.failed-units", "system.snapshots",
    ), checks.boot),
    _section("hardware", "Hardware", "Every hardware node and whether a driver claimed it, firmware and probe errors, and the kernel's build options against Asahi's.", (
        "hardware.drivers", "hardware.firmware", "hardware.probe-errors", "hardware.kernel-config",
    ), checks.hardware_inventory),
    _section("graphics", "Graphics", "The GPU driver, Vulkan and OpenGL.", ("gpu.driver", "gpu.vulkan", "gpu.opengl"), checks.graphics),
    _section("video", "Video", "Plays a test card full screen on the built-in screen with hardware decode (H.264, HEVC) and checks its colours in a screenshot.",
             video.CHECK_IDS, video.run),
    _section("display", "Display", "Outputs, the display controller, the backlight and the notch strip; then you look: the bar and the notch, brightness steps, the cursor.", (
        "display.outputs", "display.controller", "display.backlight", "display.notch-strip",
        "display.notch-bar", "display.brightness-steps", "display.cursor",
    ), checks.display, ("display.notch-bar", "display.brightness-steps", "display.cursor")),
    _section("audio", "Audio", "Sound cards, the default sink, speaker DSP and protection, the microphone mapping; then the microphone, a short tone at 30% volume (only once speaker protection is confirmed) and the headphone jack.", (
        "audio.sound-cards", "audio.default-sink", "audio.speaker-dsp", "audio.speaker-protection",
        "audio.speaker-amps-unlocked", "audio.microphone-mapping",
        "audio.microphone-signal", "audio.speaker-tone", "audio.headphone-detection",
    ), checks.audio, ("audio.speaker-tone", "audio.headphone-detection")),
    _section("network", "Network", "Wi-Fi, its backend and Bluetooth; then you pair a Bluetooth device, and (at the Mac, not over SSH, with your agreement) the Wi-Fi driver is reloaded to time the first join to a 5 GHz network.", (
        "network.wifi", "network.wifi-backend", "network.bluetooth", "network.bluetooth-pairing", "network.wifi-first-join",
    ), checks.network, ("network.bluetooth-pairing",)),
    _section("sleep", "Sleep", "You close and open the lid when asked: suspend and resume, then clamshell mode with an external display (the built-in screen off, no sleep), and whether Wi-Fi and Thunderbolt come back after waking up. Only at the Mac, never over SSH.",
             sleep.CHECK_IDS, sleep.run, disruptive=True),
    _section("input", "Input", "The ambient light sensor and the automatic keyboard light; then you cover the sensor and watch the keyboard light, press the function keys and try the trackpad's gestures.", (
        "input.ambient-light", "input.auto-keyboard-light", "input.keyboard-light-follows-room",
        "input.function-keys", "input.trackpad-gestures",
    ), checks.input_devices, ("input.keyboard-light-follows-room", "input.function-keys", "input.trackpad-gestures")),
    _section("touch-id", "Touch ID", "Passive SEP firmware provenance and sensor readiness; at this Mac, whether an existing fingerprint unlock worked this boot.",
             touchid.CHECK_IDS, touchid.run, (touchid.UNLOCK,)),
    _section("camera", "Camera", "The camera's image processor and a few frames from it; then (at the Mac) you look at its picture.",
             camera.CHECK_IDS, camera.run, (camera.IMAGE,)),
    _section("ports", "Ports", "USB-C ports, Thunderbolt and USB4 links and displays on USB-C: plug in what you have; then you say whether they work.",
             ports.CHECK_IDS, ports.run, (ports.DEVICES_WORK, ports.PICTURE)),
    _section("power", "Power", "The battery, its charge limit (set, cleared, then put back as it was) and omarchy-mac's saved limit, and idle power over 30 s on battery; then, at the Mac and if you want, the battery drain over ten minutes asleep.",
             power.CHECK_IDS, power.run),
    _section("cpu", "CPU", "CPU frequency scaling.", ("cpu.frequency-scaling",), checks.cpu),
    _section("benchmarks", "Benchmarks", "About a minute of short benchmarks for comparing Macs: OpenGL (glmark2, off-screen), Vulkan (vkmark, headless) and H.264 and HEVC hardware decode (ffmpeg). Leave the Mac alone while they run.",
             benchmarks.CHECK_IDS, benchmarks.run),
)
