"""The Audio section's live checks: the microphone, a speaker tone and the headphone jack.

Microphone (automatic): 3 s from the default input, measured in memory by
a few lines of Python on the Mac (peak and RMS); the sound itself is never
kept, recorded or uploaded. It passes when the peak reaches MIC_MIN_PEAK
(room noise does). Only the built-in microphone is tested: another default
input (a USB or Bluetooth headset) skips it.

Speaker tone (human): nothing plays until speaker protection is confirmed
active, all three of:

  - speakersafetyd is active (systemctl is-active speakersafetyd), or on
    the MacBook Neo speakerguardd, whose feed-forward model replaces it
    there (speakersafetyd's J700 drop-in skips it);
  - this boot's kernel log says "Speaker volumes unlocked", which
    snd-soc-macaudio logs once the daemon has lifted the amps' -100 dB
    lock (read as the user, else through sudo -n);
  - the default output is asahi-audio's DSP filter sink for the speakers
    (audio_effect.<board>-convolver), so the tone goes through the speaker
    model, never straight to the amps.

Anything else skips the check with the reason, and the volume is never
touched. Then the default sink is set to 30% (changes.set_volume caps it
there and puts it and the mute back when the section ends), and a 2 s tone
at about -12 dBFS, faded in and out, plays on that sink by name.

Headphone jack (human): the human plugs headphones in and says whether the
Mac noticed; the default output afterwards is kept as evidence.

Output and input names are only recorded when they are the Mac's own
(platform sinks, the DSP sinks, the microphone mapping): other names can
carry a Bluetooth address or a USB serial number.
"""

from __future__ import annotations

import re

from . import changes, human
from .inventory import KERNEL_LOG
from .session import Context

SAFETY_UNIT = ["systemctl", "is-active", "speakersafetyd"]
GUARD_UNIT = ["systemctl", "is-active", "speakerguardd"]
AMPS_UNLOCKED = "Speaker volumes unlocked"
DEFAULT_SINK = ["pactl", "get-default-sink"]
DEFAULT_SOURCE = ["pactl", "get-default-source"]
TONE_VOLUME = 0.30
MIC_MIN_PEAK = 8

SPEAKER_SINK = re.compile(r"^audio_effect\.[a-z0-9]+-convolver$")
HEADPHONE_SINK = re.compile(r"^alsa_output\.platform-[A-Za-z0-9_.-]*Headphones[A-Za-z0-9_.-]*$")
BUILT_IN_MIC = re.compile(r"^(omarchy_asahi_mic(\.monitor)?|effect_output\.[a-z0-9]+-mic|alsa_input\.platform-[A-Za-z0-9_.-]+)$")

# Run as `sh -c SCRIPT sh PROGRAM DEVICE`: the Python program is an argument, never pasted into the script.
TONE_SCRIPT = 'python3 -c "$1" | pacat --raw --format=s16le --channels=2 --rate=48000 --device="$2"'
TONE_PROGRAM = (
    "import math, struct, sys\n"
    "rate = 48000\n"
    "n = 2 * rate\n"
    "fade = rate // 20\n"
    "def level(i):\n"
    "    return min(1.0, i / fade, (n - i) / fade)\n"
    "wave = (int(8000 * level(i) * math.sin(2 * math.pi * 440 * i / rate)) for i in range(n))\n"
    "sys.stdout.buffer.write(b''.join(struct.pack('<hh', v, v) for v in wave))\n"
)
MIC_SCRIPT = 'timeout 3 parec --raw --format=s16le --channels=1 --rate=48000 --device="$2" | python3 -c "$1"'
MIC_PROGRAM = (
    "import array, math, sys\n"
    "data = sys.stdin.buffer.read()\n"
    "samples = array.array('h', data[: len(data) // 2 * 2])\n"
    "if sys.byteorder == 'big':\n"
    "    samples.byteswap()\n"
    "peak = max(map(abs, samples), default=0)\n"
    "rms = math.sqrt(sum(v * v for v in samples) / len(samples)) if samples else 0.0\n"
    "print(f'samples {len(samples)} peak {peak} rms {rms:.1f}')\n"
)
_MEASURED = re.compile(r"^samples (\d+) peak (\d+) rms ([0-9.]+)$")

TONE_QUESTION = "Did you hear a short tone from the built-in speakers, without crackling or a pop?"
HEADPHONE_QUESTION = "Plug headphones into the headphone jack now (s if you have none). Did the Mac switch its sound to them?"


def tone_argv(sink: str) -> list[str]:
    return ["sh", "-c", TONE_SCRIPT, "sh", TONE_PROGRAM, sink]


def mic_argv(source: str) -> list[str]:
    return ["sh", "-c", MIC_SCRIPT, "sh", MIC_PROGRAM, source]


def _say(ctx: Context, text: str) -> None:
    if ctx.ui:
        ctx.ui.text(text)
    else:
        ctx.host.show(text)


def _why(result) -> str:
    lines = (result.stderr or result.stdout).strip().splitlines()
    return lines[-1] if lines else f"exit {result.returncode}"


def _read_name(ctx: Context, argv: list[str]) -> str | None:
    result = ctx.host.run(argv)
    name = result.stdout.strip()
    return name if result.returncode == 0 and name and "\n" not in name else None


def describe_sink(name: str | None) -> str:
    if name is None:
        return "none (pactl couldn't say)"
    if SPEAKER_SINK.match(name):
        return f"{name} (the speakers' DSP sink)"
    if HEADPHONE_SINK.match(name):
        return f"{name} (the headphone jack)"
    return "another output (its name isn't recorded)"


# -- the microphone ------------------------------------------------------------------

def microphone(ctx: Context) -> dict:
    check_id = "audio.microphone-signal"
    source = _read_name(ctx, DEFAULT_SOURCE)
    if source is None:
        return _automatic(check_id, "skip", ["skipped: pactl couldn't name the default input"])
    if not BUILT_IN_MIC.match(source):
        return _automatic(check_id, "skip", ["skipped: the default input isn't the built-in microphone (its name isn't recorded)"])
    _say(ctx, "Listening to the built-in microphone for 3 seconds: say something now. The sound is measured in memory and never kept.")
    captured = ctx.host.run(mic_argv(source))
    evidence = [f"default input: {source}"]
    found = _MEASURED.match(captured.stdout.strip().splitlines()[-1]) if captured.stdout.strip() else None
    if found is None or int(found.group(1)) == 0:
        return _automatic(check_id, "skip", [*evidence, f"skipped: nothing was captured ({_why(captured)})"])
    samples, peak, rms = int(found.group(1)), int(found.group(2)), found.group(3)
    evidence.append(f"captured {samples} samples: peak {peak}, rms {rms} (16-bit)")
    if peak < MIC_MIN_PEAK:
        return _automatic(check_id, "fail", [*evidence, f"silence: the peak stayed under {MIC_MIN_PEAK}"])
    return _automatic(check_id, "pass", evidence)


# -- the speaker tone ----------------------------------------------------------------

def protection(ctx: Context) -> tuple[str | None, list[str], str | None]:
    """Whether the speakers are protected: (why not, or None; evidence; the speakers' sink)."""
    evidence = []
    unit = ctx.host.run(SAFETY_UNIT)
    active = unit.returncode == 0 and unit.stdout.strip() == "active"
    evidence.append(f"speakersafetyd: {unit.stdout.strip() or _why(unit)}")
    if not active:
        guard = ctx.host.run(GUARD_UNIT)
        active = guard.returncode == 0 and guard.stdout.strip() == "active"
        evidence.append(f"speakerguardd: {guard.stdout.strip() or _why(guard)}")
    if not active:
        return "speaker protection isn't active: neither speakersafetyd nor speakerguardd is running", evidence, None

    unlocked = AMPS_UNLOCKED in ctx.host.run(KERNEL_LOG).stdout or AMPS_UNLOCKED in ctx.host.run(["sudo", "-n", *KERNEL_LOG]).stdout
    if not unlocked:
        return f"speaker protection isn't confirmed: this boot's kernel log doesn't say {AMPS_UNLOCKED!r}", evidence, None
    evidence.append(f"kernel log: {AMPS_UNLOCKED}")

    sink = _read_name(ctx, DEFAULT_SINK)
    evidence.append(f"default output: {describe_sink(sink)}")
    if sink is None or not SPEAKER_SINK.match(sink):
        return "the default output isn't the speakers' protected DSP sink", evidence, None
    return None, evidence, sink


def speaker_tone(ctx: Context) -> dict:
    check_id = "audio.speaker-tone"
    why, evidence, sink = protection(ctx)
    if why:
        return human.skip(check_id, f"{why}, so nothing was played", evidence)
    assert sink is not None
    why = changes.set_volume(ctx, TONE_VOLUME)
    if why:
        return human.skip(check_id, f"{why}, so nothing was played", evidence)
    if _read_name(ctx, DEFAULT_SINK) != sink:
        return human.skip(check_id, "the default output changed while the volume was set, so nothing was played", evidence)
    _say(ctx, "Listen: a short tone plays on the built-in speakers now, at 30% volume.")
    played = ctx.host.run(tone_argv(sink))
    if played.returncode != 0:
        return human.skip(check_id, f"the tone couldn't be played ({_why(played)})", evidence)
    evidence.append(f"played a 2 s 440 Hz tone at {TONE_VOLUME:.0%} volume on {sink}")
    return human.check(ctx, check_id, TONE_QUESTION, evidence)


# -- the headphone jack ----------------------------------------------------------------

def headphones(ctx: Context) -> dict:
    check_id = "audio.headphone-detection"
    if human.absent(ctx, check_id):
        return human.skip(check_id, "this Mac has no headphone jack")
    result = human.check(ctx, check_id, HEADPHONE_QUESTION)
    sink = _read_name(ctx, DEFAULT_SINK)
    result["evidence"].append(f"default output after: {describe_sink(sink)}")
    on_headphones = sink is not None and HEADPHONE_SINK.match(sink) is not None
    if sink is not None and result["status"] == "pass" and not on_headphones:
        human.not_backed(result, "the default output afterwards isn't the headphone jack")
    elif result["status"] == "fail" and on_headphones:
        human.not_backed(result, "the default output afterwards is the headphone jack")
    _say(ctx, "You can unplug the headphones.")
    return result


def run(ctx: Context) -> list[dict]:
    """The live checks, in order: the microphone first, the tone before anything is plugged in."""
    return [microphone(ctx), speaker_tone(ctx), headphones(ctx)]


def _automatic(check_id: str, status: str, evidence: list[str]) -> dict:
    return {"id": check_id, "kind": "automatic", "status": status, "evidence": evidence}
