"""The Power section: the battery, its charge limit, idle power draw and, optionally, drain while asleep.

Battery (power.battery): hardware.battery.

Charge limit (power.charge-limit, the macsmc-battery driver): the Apple SMC's
end threshold (/sys/class/power_supply/macsmc-battery/charge_control_end_threshold)
is set to 80% and read back, then cleared to 100% and read back. The driver
derives the start threshold from it (charging restarts at 75% under an 80%
limit); the SMC takes only 80 or 100. Writing it needs root:
`sudo -n sh -c SET_SCRIPT sh 80`.

Saved limit (power.charge-limit-kept, omarchy-mac's integration): omarchy-mac's
`omarchy battery charge limit` (omarchy-battery-charge-limit) shows the limit,
sets 80 and saves it to /etc/udev/macsmc-battery.conf, and 100 clears it; its
udev rule (94-omarchy-mac-battery-charge-limit.rules) runs
/usr/lib/omarchy-mac/battery-charge-limit-restore as the battery appears at
boot, to put the saved limit back. It passes when the command, the rule and
the helper are installed, the command reads the limit sysfs has, and both
settings stick, with 80 saved for the next boot. Nothing is rebooted, so the
rule itself isn't run.

Before either changes anything, one restorer is registered (RESTORE_SCRIPT,
as root): the original end threshold, then, 2 s later, the original saved
file as it was (its text, or no file). The wait is for asahi-scripts' path
unit (macsmc-battery-charge-control-end-threshold.path), which saves every
change to the threshold into that same file by itself. The checks put both
back themselves when they're done, read them back and drop the restorer; it
only runs if they couldn't (an error, Ctrl-C, or the next run after one that
was killed).

On battery: the last two checks measure the battery while it runs the Mac,
and on AC power it doesn't show the Mac's draw. So first thing in the
section, before anything else runs, a Mac on AC power at its seat says so
and waits up to UNPLUG_SECONDS for the charger to come out (UNPLUG_SCRIPT
polls the battery's status; at a terminal it shows a countdown on one line,
and any key stops waiting); over SSH or without a local seat nobody can
unplug it, so both are skipped. A charger left in skips both, saying so
when the section gets to them: they never end silently (the M2 Max,
2026-09-27, said "about ten minutes" and went straight to the report).

Idle power (power.idle-draw): the battery's power_now (microwatts), or
current_now times voltage_now, sampled every second for IDLE_SECONDS on the
Mac (IDLE_SCRIPT) while it runs on battery. It fails when the battery
reports no draw while discharging; the evidence gives the average, lowest and
highest.

Sleep drain (power.sleep-drain, optional, at the Mac only, asked only on
battery): the human closes the lid for about ten minutes and opens it again;
the tool never puts the Mac to sleep itself (safety.py). The lid watch and the
system log are the Sleep section's (sleep.py). The battery's energy before
and after, over the time the kernel says it was asleep, gives the drain in
watts and percent per hour; it fails above DRAIN_FAIL_PERCENT_PER_HOUR. The
step is checkpointed before the lid closes (Context.progress), so a run
stopped while the lid was closed judges it, when resumed, from the system log
and the battery then.

Privacy: macsmc-battery also exposes the battery's serial number, manufacture
date, model name and cycle count. None of them is read: only the status,
capacity, thresholds, power, current, voltage, energy and charge, as numbers.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from . import macos_reference, hardware, human, packages, presence, sleep
from .session import Context, Restorer
from .ui import Ui

BATTERY = "power.battery"
CHARGE_LIMIT = "power.charge-limit"
CHARGE_LIMIT_KEPT = "power.charge-limit-kept"
IDLE_DRAW = "power.idle-draw"
SLEEP_DRAIN = "power.sleep-drain"
CHECK_IDS = (BATTERY, CHARGE_LIMIT, CHARGE_LIMIT_KEPT, IDLE_DRAW, SLEEP_DRAIN)

POWER_SUPPLY = "/sys/class/power_supply"
SMC_BATTERY_NAME = hardware.SMC_BATTERY_NAME
SMC_BATTERY = f"{POWER_SUPPLY}/{SMC_BATTERY_NAME}"
END = f"{SMC_BATTERY}/charge_control_end_threshold"
START = f"{SMC_BATTERY}/charge_control_start_threshold"
SAVED = "/etc/udev/macsmc-battery.conf"
SAVED_KEY = "CHARGE_CONTROL_END_THRESHOLD"
COMMAND = "omarchy-battery-charge-limit"
RULE = "/usr/lib/udev/rules.d/94-omarchy-mac-battery-charge-limit.rules"
HELPER = "/usr/lib/omarchy-mac/battery-charge-limit-restore"
LIMIT, FULL = 80, 100
RESTARTS_AT = {LIMIT: 75, FULL: 100}  # the start threshold the driver derives from each end threshold
NO_SAVED = "none"

# Run as `sudo -n sh -c SCRIPT sh LIMIT`.
SET_SCRIPT = f"printf '%s\\n' \"$1\" > {END}"
# Run as `sudo -n sh -c SCRIPT sh LIMIT none` or `... sh LIMIT saved TEXT`: the threshold, then the saved file.
RESTORE_SCRIPT = (
    f"printf '%s\\n' \"$1\" > {END} || exit 1\n"
    "sleep 2\n"
    f"if [ \"$2\" = {NO_SAVED} ]; then rm -f {SAVED}; else printf '%s' \"$3\" > {SAVED}; fi"
)

IDLE_SECONDS = 30
UNPLUG_SECONDS = 60
# Run as `sh -c SCRIPT sh BATTERY_DIR SECONDS TTY LABEL` (TTY the terminal's width, 0 off one):
# "unplugged", or "timeout STATUS" / "skipped STATUS" (a key).
UNPLUG_SCRIPT = sleep.TICK_FUNCTION + r"""d=$1 wait=$(($2 * 2)) tty=$3 label=$4 n=0
while :; do
  s=$(tr -d ' \n' < "$d/status" 2>/dev/null)
  if [ "$s" = Discharging ]; then done_ticking; echo "unplugged"; exit 0; fi
  if [ "$n" -ge "$wait" ]; then done_ticking; echo "timeout $s"; exit 0; fi
  if tick "$label: unplug the charger now, $(( (wait - n + 1) / 2 )) s left (any key skips)"; then done_ticking; echo "skipped $s"; exit 0; fi
  n=$((n + 1))
done"""
# Above this an idle Apple Silicon laptop is busy with something (it idles at a few watts): said, not failed.
IDLE_HIGH_WATTS = 15.0
# Run as `sh -c SCRIPT sh BATTERY_DIR SECONDS`: a line a second, "sample STATUS POWER CURRENT VOLTAGE" (- when unreadable).
IDLE_SCRIPT = r"""d=$1 i=0
while [ "$i" -lt "$2" ]; do
  echo "sample $(tr -d ' \n' < "$d/status" 2>/dev/null) $(cat "$d/power_now" 2>/dev/null || echo -) $(cat "$d/current_now" 2>/dev/null || echo -) $(cat "$d/voltage_now" 2>/dev/null || echo -)"
  i=$((i + 1))
  if [ "$i" -lt "$2" ]; then sleep 1; fi
done"""
# Run as `sh -c SCRIPT sh BATTERY_DIR`: the time, and the battery's status and levels, one "name value" a line.
READING_FIELDS = ("status", "capacity", "energy_now", "energy_full", "charge_now", "charge_full")
READING_SCRIPT = (
    'd=$1\necho "time $(date +%s)"\n'
    f'for f in {" ".join(READING_FIELDS)}; do echo "$f $(tr -d \' \\n\' < "$d/$f" 2>/dev/null)"; done'
)

# The lid watch's budget of awake polls with the lid closed: brief wake-ups over ten minutes asleep mustn't end it.
DRAIN_CLOSED_SECONDS = 120
DRAIN_MIN_SECONDS = 300  # asleep for less says too little: the battery's levels move too slowly
# A Mac that loses more than this an hour asleep would be flat in about half a day asleep: it isn't sleeping properly.
DRAIN_FAIL_PERCENT_PER_HOUR = 8.0

UNPLUG = (
    "Power: unplug the charger now. The idle-power check (30 s) and the optional battery drain while asleep "
    "(about ten minutes) measure the battery while it runs the Mac, which it only does off the charger. "
    f"The run waits up to {UNPLUG_SECONDS} s for it; without it both are skipped."
)
STILL_PLUGGED = "The charger is still plugged in, so idle power and the battery drain while asleep are skipped."
MEASURING = f"Measuring idle power for {IDLE_SECONDS} s: leave the Mac alone."
DRAIN_WARNING = (
    "Battery drain while asleep (optional, about ten minutes): when you answer y, close the lid (the run waits up to "
    f"{sleep.CLOSE_SECONDS} s for it), leave it closed for ten minutes (a timer helps), then open it; keep the charger out. "
    "The run waits here with the lid closed and carries on once it opens. "
    "If it doesn't carry on after the lid opens, run omarchy-m-test again: it resumes here."
)
DRAIN_QUESTION = "Measure the battery drain over ten minutes asleep now?"
DRAIN_SKIPPED = "Battery drain while asleep: skipped ({why})."
PLUG_BACK = "You can plug the charger back in."
RECOVERED_NOTE = "The last run stopped during the sleep-drain check; its result is read from the system log and the battery."


def run(ctx: Context) -> list[dict]:
    progress = ctx.progress("power")
    results: dict[str, dict] = progress.setdefault("results", {})
    if not isinstance(results, dict) or not all(isinstance(r, dict) and r.get("id") == k for k, r in results.items()):
        results = progress["results"] = {}
    if "drain" in progress:
        _recover(ctx, progress, results)
    battery = _battery(ctx)
    results[BATTERY] = hardware.battery(ctx.host)
    if battery is None:
        for check_id in CHECK_IDS[1:]:
            results.setdefault(check_id, _result(check_id, "skip", ["skipped: this Mac has no battery"]))
    unplugged = False
    plugged = None  # why the charger is in, once asked to unplug it
    if battery is not None and (IDLE_DRAW not in results or SLEEP_DRAIN not in results):
        unplugged, plugged = unplug(ctx, battery)
    if CHARGE_LIMIT not in results or CHARGE_LIMIT_KEPT not in results:
        results[CHARGE_LIMIT], results[CHARGE_LIMIT_KEPT] = charge_limit(ctx, battery or "")
        ctx.changes.persist()
    if IDLE_DRAW not in results:
        results[IDLE_DRAW] = idle_draw(ctx, battery or "", plugged)
        ctx.changes.persist()
    if SLEEP_DRAIN not in results:
        results[SLEEP_DRAIN], agreed = sleep_drain(ctx, battery or "", progress, plugged)
        unplugged = unplugged or agreed
    if unplugged:
        _ui(ctx).text(PLUG_BACK)
    return [results[check_id] for check_id in CHECK_IDS]


def _ui(ctx: Context) -> Ui:
    return ctx.ui or Ui(ctx.host)


def _battery(ctx: Context) -> str | None:
    """The Mac's own battery (macsmc-battery on these Macs), never a mouse's or keyboard's (hardware.system_battery)."""
    return hardware.system_battery(ctx.host)


def _read(ctx: Context, path: str) -> str | None:
    try:
        return ctx.host.read_file(path).decode("utf-8", "replace").strip()
    except OSError:
        return None


def _number(text: str | None) -> int | None:
    return int(text) if text is not None and re.fullmatch(r"-?\d+", text) else None


def _exists(ctx: Context, path: str) -> bool:
    try:
        ctx.host.read_file(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True  # there, but not readable by this user
    return True


# -- the charge limit --------------------------------------------------------------------

def _limits(ctx: Context) -> tuple[int | None, int | None]:
    """(end threshold, start threshold) as the SMC reports them now."""
    return _number(_read(ctx, END)), _number(_read(ctx, START))


def _describe(end: int | None, start: int | None) -> str:
    return f"{end}%" + (f" (charging restarts at {start}%)" if start is not None else "")


def _saved_value(text: str | None) -> int | None:
    """The limit a saved file holds, as udev reads it (the last CHARGE_CONTROL_END_THRESHOLD= line)."""
    found = None
    for line in (text or "").splitlines():
        if line.startswith(SAVED_KEY + "="):
            found = _number(line.partition("=")[2].strip())
    return found


def set_argv(limit: int) -> list[str]:
    return ["sudo", "-n", "sh", "-c", SET_SCRIPT, "sh", str(limit)]


def restore_argv(end: int, saved: str | None) -> list[str]:
    return ["sudo", "-n", "sh", "-c", RESTORE_SCRIPT, "sh", str(end), *([NO_SAVED] if saved is None else ["saved", saved])]


def command_argv(limit: int) -> list[str]:
    return ["sudo", "-n", COMMAND, str(limit)]


def charge_limit(ctx: Context, battery: str) -> tuple[dict, dict]:
    """Set and clear the charge limit through sysfs and through omarchy-mac's command, then put it back."""
    omarchy = ctx.system is None or ctx.system.is_omarchy
    kept_skip = None if omarchy else f"reference run on {ctx.system.distro}: Omarchy integration isn't checked"  # type: ignore[union-attr]
    if battery != SMC_BATTERY_NAME:
        why = f"skipped: the battery isn't the Apple SMC's ({SMC_BATTERY_NAME})"
        return _result(CHARGE_LIMIT, "skip", [why]), _result(CHARGE_LIMIT_KEPT, "skip", [why])
    end, start = _limits(ctx)
    if end is None:
        why = "the kernel exposes no charge_control_end_threshold for the battery, so no charge limit can be set"
        return _result(CHARGE_LIMIT, "fail", [why]), _result(CHARGE_LIMIT_KEPT, "skip", [f"skipped: {why}"])
    before = f"charge limit before: {_describe(end, start)}"
    try:
        saved = ctx.host.read_file(SAVED).decode("utf-8", "replace")
    except FileNotFoundError:
        saved = None
    except OSError:
        why = f"skipped: {SAVED} can't be read, so it couldn't be put back as it was"
        return _result(CHARGE_LIMIT, "skip", [before, why]), _result(CHARGE_LIMIT_KEPT, "skip", [why])
    saved_line = f"saved for the next boot: {_saved_value(saved)}%" if _saved_value(saved) is not None else f"nothing saved in {SAVED}"

    kept: list[str] = []
    kept_problems: list[str] = []
    installed = False
    if kept_skip is None:
        kept, kept_problems, installed = _installed(ctx, end)
        kept.append(saved_line)

    if not packages.sudo_ready(ctx):
        why = "skipped: setting the charge limit needs sudo, and it wasn't given"
        if kept_skip:
            kept_result = _result(CHARGE_LIMIT_KEPT, "skip", [f"skipped: {kept_skip}"])
        elif kept_problems:  # what's missing is known without changing anything
            kept_result = _result(CHARGE_LIMIT_KEPT, "fail", [*kept, *kept_problems])
        else:
            kept_result = _result(CHARGE_LIMIT_KEPT, "skip", [*kept, why])
        return _result(CHARGE_LIMIT, "skip", [before, why]), kept_result

    restorer = ctx.changes.register(f"the battery charge limit (back to {end}%)", restore_argv(end, saved), sudo=True)
    evidence, problems = [before], []
    for limit in (LIMIT, FULL):
        done = ctx.host.run(set_argv(limit))
        if done.returncode != 0:
            problems.append(f"writing {limit} to charge_control_end_threshold failed ({_why(done)})")
            continue
        now = _limits(ctx)
        evidence.append(f"{'set' if limit == LIMIT else 'cleared to'} {limit}%: the SMC reads back {_describe(*now)}")
        if now[0] != limit or (now[1] is not None and now[1] != RESTARTS_AT[limit]):
            problems.append(f"{limit}% didn't stick: expected {_describe(limit, RESTARTS_AT[limit])}")

    if kept_skip is None and installed:
        lines, more = _through_command(ctx)
        kept += lines
        kept_problems += more

    back, file_back = _put_back(ctx, restorer, end, saved)
    kernel = _result(CHARGE_LIMIT, "fail" if problems else "pass", [*evidence, *problems, back])
    if kept_skip is not None:
        return kernel, _result(CHARGE_LIMIT_KEPT, "skip", [f"skipped: {kept_skip}"])
    return kernel, _result(CHARGE_LIMIT_KEPT, "fail" if kept_problems else "pass", [*kept, *kept_problems, file_back])


def _why(done) -> str:
    return (done.stderr or done.stdout).strip() or f"exit {done.returncode}"


def _installed(ctx: Context, end: int) -> tuple[list[str], list[str], bool]:
    """What of omarchy-mac's charge limit is installed: (evidence, problems, whether all of it is)."""
    evidence, problems = [], []
    status = ctx.host.run([COMMAND])
    command = status.returncode != 127
    if not command:
        problems.append(f"{COMMAND} isn't installed (omarchy-mac's `omarchy battery charge limit`)")
    else:
        shown = re.search(r"Charge limit: (\d+)%", status.stdout)
        if status.returncode != 0 or shown is None:
            problems.append(f"`{COMMAND}` couldn't show the limit ({_why(status)})")
        elif int(shown.group(1)) != end:
            problems.append(f"`{COMMAND}` shows {shown.group(1)}%, but the SMC has {end}%")
        else:
            evidence.append(f"`{COMMAND}` shows {shown.group(1)}%, as the SMC has")
    rule, helper = _exists(ctx, RULE), _exists(ctx, HELPER)
    if rule and helper:
        evidence.append("omarchy-mac's udev rule and helper that restore a saved limit at boot are installed")
    else:
        missing = [path for path, there in ((RULE, rule), (HELPER, helper)) if not there]
        problems.append(f"nothing of omarchy-mac's restores a saved limit at boot ({', '.join(missing)} missing)")
    return evidence, problems, command and rule and helper


def _through_command(ctx: Context) -> tuple[list[str], list[str]]:
    """Set 80 and clear it with omarchy-mac's command: each has to stick, and 80 has to be saved. (evidence, problems)"""
    evidence, problems = [], []
    for limit in (LIMIT, FULL):
        done = ctx.host.run(command_argv(limit))
        if done.returncode != 0:
            problems.append(f"`omarchy battery charge limit {limit}` failed ({_why(done)})")
            continue
        end, start = _limits(ctx)
        line = f"`omarchy battery charge limit {limit}`: the SMC reads back {_describe(end, start)}"
        if end != limit:
            problems.append(f"{limit}% didn't stick")
        if limit == LIMIT:
            saved = _saved_value(_read(ctx, SAVED))
            line += f", {saved}% saved for the next boot" if saved is not None else ", nothing saved for the next boot"
            if saved != limit:
                problems.append(f"{limit}% wasn't saved in {SAVED}, so the next boot wouldn't keep it")
        evidence.append(line)
    return evidence, problems


def _put_back(ctx: Context, restorer: Restorer, end: int, saved: str | None) -> tuple[str, str]:
    """Put the original limit and saved file back now; lines for each check's evidence."""
    done = ctx.host.run(list(restorer.command))
    now, _ = _limits(ctx)
    if saved is None:
        file_back = not _exists(ctx, SAVED)
    else:
        file_back = _read(ctx, SAVED) == saved.strip()
    if done.returncode == 0 and now == end and file_back:
        ctx.changes.replace(restorer, None)
        return f"put back to {end}% afterwards", "the saved limit put back as it was" + (f" (no {SAVED})" if saved is None else "")
    why = f"couldn't put the charge limit back yet (it's {now}%, was {end}%): it's tried again when the section ends"
    return why, why


# -- idle power --------------------------------------------------------------------------

def idle_argv(battery_dir: str, seconds: int = IDLE_SECONDS) -> list[str]:
    return ["sh", "-c", IDLE_SCRIPT, "sh", battery_dir, str(seconds)]


@dataclass(frozen=True)
class Sample:
    status: str
    watts: float | None
    source: str  # "power_now" or "current_now x voltage_now"


def parse_samples(stdout: str) -> list[Sample]:
    samples = []
    for line in stdout.splitlines():
        parts = line.split()
        if parts[:1] != ["sample"]:
            continue
        status = parts[1] if len(parts) == 5 else ""
        power, current, voltage = (_number(p) for p in parts[-3:]) if len(parts) >= 4 else (None, None, None)
        if power:
            samples.append(Sample(status, abs(power) / 1e6, "power_now"))
        elif current and voltage:
            samples.append(Sample(status, abs(current) * voltage / 1e12, "current_now x voltage_now"))
        else:
            samples.append(Sample(status, None, ""))
    return samples


def unplug_argv(battery_dir: str, tty: int = 0, label: str = "", seconds: int = UNPLUG_SECONDS) -> list[str]:
    return ["sh", "-c", UNPLUG_SCRIPT, "sh", battery_dir, str(seconds), str(tty), label]


def unplug(ctx: Context, battery: str) -> tuple[bool, str | None]:
    """At the start of the section: on AC power, ask for the charger out and wait for it.

    (whether the human was asked, why the Mac is still on AC power: None when it runs on battery).
    """
    directory = f"{POWER_SUPPLY}/{battery}"
    status = _read(ctx, f"{directory}/status") or "unreadable"
    if status == "Discharging":
        return False, None
    blocked = presence.of(ctx.host, ctx.cache).blocks()
    if blocked:
        return False, (f"the Mac is on AC power (battery {status}), where the battery doesn't show what it draws, "
                       f"and nobody may be at the Mac to unplug the charger ({blocked})")
    ui = _ui(ctx)
    ui.text(UNPLUG)
    return True, wait_for_unplug(ctx, directory)


def wait_for_unplug(ctx: Context, directory: str) -> str | None:
    """Wait for the battery to discharge: None once it does, else why not (and the human is told)."""
    terminal = ctx.host.terminal()
    if terminal is not None:
        answer = ctx.host.run_tty(unplug_argv(directory, tty=terminal.width, label=getattr(_ui(ctx), "pad", "") + "Power")).stdout
    else:
        answer = ctx.host.run(unplug_argv(directory)).stdout
    word, _, status = answer.strip().rpartition("\n")[-1].partition(" ")
    if word == "unplugged":
        _ui(ctx).text("On battery.")
        return None
    status = status.strip() or "unreadable"
    _ui(ctx).text(STILL_PLUGGED)
    if word == "skipped":
        return f"you chose not to unplug the charger (battery {status})"
    return f"the charger stayed plugged in for {UNPLUG_SECONDS} s (battery {status})"


def idle_draw(ctx: Context, battery: str, plugged: str | None = None) -> dict:
    directory = f"{POWER_SUPPLY}/{battery}"
    if plugged is not None:
        return _result(IDLE_DRAW, "skip", [f"skipped: {plugged}"])
    status = _read(ctx, f"{directory}/status") or "unreadable"
    if status != "Discharging":
        return _result(IDLE_DRAW, "skip", [f"skipped: the Mac is on AC power (battery {status}), where the battery doesn't show what it draws"])
    _ui(ctx).text(MEASURING)
    return _vs_macos(ctx, judge_idle(parse_samples(ctx.host.run(idle_argv(directory)).stdout)))


def _vs_macos(ctx: Context, result: dict) -> dict:
    """macOS's idle number beside ours, if this Mac has one. They're measured differently, so it's only a description."""
    reference = macos_reference.load(ctx.machine.board)
    there = (reference or {}).get("metrics", {}).get("idle_power_w")
    if result["status"] != "pass" or there is None:
        return result
    return {**result, "evidence": [*result["evidence"], f"vs macOS: {there} W idle on macOS ({macos_reference.conditions(reference)}); measured differently from the battery draw here, so a description, not a comparison"]}


def judge_idle(samples: list[Sample]) -> dict:
    if not samples:
        return _result(IDLE_DRAW, "fail", ["the battery's status and power couldn't be read"])
    on_battery = [s for s in samples if s.status == "Discharging"]
    if len(on_battery) * 2 < len(samples):
        seen = ", ".join(sorted({s.status or "unreadable" for s in samples}))
        return _result(IDLE_DRAW, "skip", [f"skipped: the Mac wasn't on battery for the measurement (battery {seen})"])
    watts = [s.watts for s in on_battery if s.watts]
    if not watts:
        return _result(IDLE_DRAW, "fail", [
            f"on battery for {len(on_battery)} s, but the battery reports no power draw (power_now, current_now and voltage_now)",
        ])
    average = sum(watts) / len(watts)
    source = next(s.source for s in on_battery if s.watts)
    evidence = [f"idle on battery for {len(on_battery)} s: {average:.1f} W average (lowest {min(watts):.1f} W, highest {max(watts):.1f} W), from {source}"]
    if average > IDLE_HIGH_WATTS:
        evidence.append(f"that's more than an idle Apple Silicon laptop usually draws (above {IDLE_HIGH_WATTS:.0f} W): something may be keeping it busy")
    return _result(IDLE_DRAW, "pass", evidence)


# -- drain while asleep ------------------------------------------------------------------

def drain_watch_argv() -> list[str]:
    return sleep.lid_watch_argv(closed=DRAIN_CLOSED_SECONDS)


def reading_argv(battery_dir: str) -> list[str]:
    return ["sh", "-c", READING_SCRIPT, "sh", battery_dir]


@dataclass(frozen=True)
class Reading:
    time: int | None = None  # Unix seconds
    status: str = ""
    capacity: int | None = None  # percent
    energy_now: int | None = None  # microwatt-hours
    energy_full: int | None = None
    charge_now: int | None = None  # microamp-hours
    charge_full: int | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Reading":
        return cls(**{k: data.get(k) for k in ("time", *READING_FIELDS)})


def parse_reading(stdout: str) -> Reading:
    found: dict[str, Any] = {}
    for line in stdout.splitlines():
        name, _, value = line.strip().partition(" ")
        if name == "status":
            found[name] = value.strip()
        elif name in ("time", *READING_FIELDS):
            found[name] = _number(value.strip())
    return Reading(**found)


def _reading(ctx: Context, battery: str) -> Reading:
    return parse_reading(ctx.host.run(reading_argv(f"{POWER_SUPPLY}/{battery}")).stdout)


def sleep_drain(ctx: Context, battery: str, progress: dict, plugged: str | None = None) -> tuple[dict, bool]:
    """(the result, whether the human agreed to close the lid off the charger)."""
    blocked = presence.of(ctx.host, ctx.cache).blocks()
    if blocked:
        return _result(SLEEP_DRAIN, "skip", [f"skipped: {blocked}"]), False
    if human.absent(ctx, sleep.CLAMSHELL):
        return _result(SLEEP_DRAIN, "skip", ["skipped: this Mac has no lid"]), False
    if plugged is not None:
        return _result(SLEEP_DRAIN, "skip", [f"skipped: {plugged}"]), False
    ui = _ui(ctx)
    ui.text(DRAIN_WARNING)
    if not ui.confirm(DRAIN_QUESTION):
        return _result(SLEEP_DRAIN, "skip", ["skipped: you chose not to (it takes about ten minutes)"]), False
    before = _reading(ctx, battery)
    if before.status != "Discharging":  # plugged back in since: ask again, and wait
        ui.text(f"The charger is plugged in again (battery {before.status or 'unreadable'}): unplug it to go on.")
        why = wait_for_unplug(ctx, f"{POWER_SUPPLY}/{battery}")
        if why is not None:
            ui.text(DRAIN_SKIPPED.format(why=why))
            return _result(SLEEP_DRAIN, "skip", [f"skipped: {why}"]), True
        before = _reading(ctx, battery)
    since = (before.time or 1) - 1
    progress["drain"] = {"since": since, "boot": sleep.boot_id(ctx), "battery": battery, "before": before.to_json()}
    ctx.changes.persist()
    lid = sleep.watch_lid(ctx, "Battery drain", closed=DRAIN_CLOSED_SECONDS)
    after = _reading(ctx, battery)
    result = judge_drain(lid, sleep.read_journal(ctx, since), before, after)
    progress.pop("drain", None)
    ctx.changes.persist()
    if result["status"] == "skip":  # said here, not only in the report: the human just spent the time
        ui.text(DRAIN_SKIPPED.format(why=result["evidence"][-1].removeprefix("skipped: ")))
    return result, True


def _recover(ctx: Context, progress: dict, results: dict) -> None:
    """The run stopped while the lid was closed for the drain check: judge it from the system log and the battery now."""
    step = progress.pop("drain")
    try:
        since, battery, before = int(step["since"]), str(step["battery"]), Reading.from_json(step["before"])
    except (KeyError, TypeError, ValueError, AttributeError):
        ctx.changes.persist()
        return  # not a step this tool wrote: the check runs from the start
    _ui(ctx).text(RECOVERED_NOTE)
    boot = sleep.boot_id(ctx)
    if step.get("boot") and boot and boot != step["boot"]:
        results[SLEEP_DRAIN] = _result(SLEEP_DRAIN, "skip", [
            "skipped: the run stopped while the lid was closed for this check, and the Mac was started again since",
        ])
    else:
        results[SLEEP_DRAIN] = judge_drain(sleep.Lid(), sleep.read_journal(ctx, since), before, _reading(ctx, battery), stopped=True)
    ctx.changes.persist()


def _used(before: Reading, after: Reading) -> tuple[float | None, float | None, str]:
    """(percent of a full battery used, watt-hours used or None, how it was measured)."""
    if None not in (before.energy_now, after.energy_now) and before.energy_full:
        used = (before.energy_now - after.energy_now) / 1e6  # type: ignore[operator]
        return used * 1e6 / before.energy_full * 100, used, f"{before.energy_now / 1e6:.2f} Wh to {after.energy_now / 1e6:.2f} Wh"  # type: ignore[operator]
    if None not in (before.charge_now, after.charge_now) and before.charge_full:
        used = before.charge_now - after.charge_now  # type: ignore[operator]
        return used / before.charge_full * 100, None, f"{before.charge_now / 1000:.0f} mAh to {after.charge_now / 1000:.0f} mAh"  # type: ignore[operator]
    if None not in (before.capacity, after.capacity):
        return float(before.capacity - after.capacity), None, f"{before.capacity}% to {after.capacity}%"  # type: ignore[operator]
    return None, None, ""


def judge_drain(lid: sleep.Lid, journal: sleep.Journal | None, before: Reading, after: Reading, stopped: bool = False) -> dict:
    if journal is None:
        return _result(SLEEP_DRAIN, "skip", ["skipped: the system log couldn't be read, so the time asleep can't be seen"])
    if lid.end == "nolid":
        return _result(SLEEP_DRAIN, "skip", ["skipped: logind can't say whether the lid is open or closed"])
    found = sleep.asleep(lid, journal)
    if found.closed is None:
        return _result(SLEEP_DRAIN, "skip", [f"skipped: {sleep.not_closed(lid, stopped)}"])
    if found.entry is None:
        if found.failed is not None:
            return _result(SLEEP_DRAIN, "fail", ["lid closed: the Mac tried to go to sleep and failed (the system log says a device failed to suspend)"])
        if journal.first("lid-closed") is None:
            return _result(SLEEP_DRAIN, "skip", [f"skipped: {sleep.NO_LID_EVENTS}"])
        return _result(SLEEP_DRAIN, "skip", ["skipped: the Mac didn't go to sleep with the lid closed (sleep.lid-suspend checks why)"])
    woke = found.exit if found.exit is not None else after.time
    seconds = (woke or found.entry) - found.entry
    evidence = [f"asleep for {seconds / 60:.1f} min ({found.mode or 'mode not logged'}), on battery"]
    if stopped:
        evidence.append("read from the system log and the battery: the run had stopped while the lid was closed")
    if seconds < DRAIN_MIN_SECONDS:
        return _result(SLEEP_DRAIN, "skip", [*evidence, f"skipped: too short to measure; keep the lid closed for about ten minutes (at least {DRAIN_MIN_SECONDS // 60})"])
    if after.status in ("Charging", "Full"):
        return _result(SLEEP_DRAIN, "skip", [*evidence, f"skipped: the charger was plugged in by the time the lid opened (battery {after.status})"])
    percent, watt_hours, how = _used(before, after)
    if percent is None:
        return _result(SLEEP_DRAIN, "skip", [*evidence, "skipped: the battery's energy, charge and capacity couldn't be read"])
    hours = seconds / 3600
    rate = max(percent, 0.0) / hours
    evidence.append(f"battery: {how}")
    evidence.append(
        f"drain: {rate:.1f}% per hour asleep"
        + (f", {max(watt_hours, 0.0) / hours:.2f} W on average" if watt_hours is not None else "")
        + (f" (a full battery would last about {100 / rate:.0f} h asleep)" if rate > 0 else "")
    )
    if rate > DRAIN_FAIL_PERCENT_PER_HOUR:
        evidence.append(f"more than {DRAIN_FAIL_PERCENT_PER_HOUR:.0f}% per hour: the Mac isn't sleeping deeply enough")
        return _result(SLEEP_DRAIN, "fail", evidence)
    return _result(SLEEP_DRAIN, "pass", evidence)


def _result(check_id: str, status: str, evidence: list[str]) -> dict:
    return {"id": check_id, "kind": "automatic", "status": status, "evidence": evidence}
