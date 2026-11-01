"""The Sleep section: suspend and resume on the lid, clamshell mode, and Wi-Fi and Thunderbolt after resume.

The tool never suspends the Mac itself (safety.py refuses every way to):
the human closes the lid, only after answering y to the prompt, and opens it
again. The section is disruptive (never over SSH or without a local seat).

Each lid step, while the human closes and opens the lid:

  - a few lines of shell on the Mac (LID_WATCH_SCRIPT) poll logind's
    LidClosed and Hyprland's monitors every 0.5 s and print when the lid
    closed and opened and each connector's state (eDP-1=off USB-2=on): only
    connector names, never a monitor's make, model or serial. Its limits
    count polls, not seconds, so the watch is bounded while the Mac is awake
    and simply pauses while it's asleep; the times it prints are the wall
    clock's, so a suspend shows as a jump. The human has CLOSE_SECONDS to
    close the lid; at a terminal the watch runs on it (run_tty) and shows a
    countdown on one line, and any key ends the wait (the step is skipped);
  - then the system log since the step began (JOURNAL_SCRIPT) is reduced on
    the Mac to event words and times: logind's lid and suspend lines, the
    kernel's PM: suspend entry/exit, a failed suspend, Thunderbolt link
    errors. No host name or message text leaves the Mac.

Suspend (sleep.lid-suspend), with no external display: it passes when the
kernel entered suspend after the lid closed; the evidence says how soon, in
which mode (s2idle) and for how long. No suspend in the time the lid was
closed fails, unless an external display was on (the Mac is meant to stay
awake then) or logind is set not to suspend on the lid: those are skipped.

Clamshell (sleep.clamshell, Omarchy's integration), with an external display
on: it passes when the Mac didn't suspend, the built-in screen was off and
an external display on while the lid was closed, and the built-in screen
came back once it opened. The recorded failure (M2 Max, 2026-09-26) is a
Mac that suspended although Hyprland had moved to the USB-C display: logind
counts only HDMI, DisplayPort and similar connectors as external, and
Apple Silicon's DP alt mode displays are DRM connector type "USB", so logind
wasn't Docked. logind's Docked and HandleLidSwitchDocked are in the evidence.

After the first resume (sleep.wifi-after-resume, sleep.thunderbolt-after-resume):
what was up before the lid closed (LINKS_SCRIPT: Wi-Fi interfaces up with an
IPv4 address, Thunderbolt networking interfaces up, the number of
Thunderbolt/USB4 devices) is polled every second for RECOVERY_SECONDS after
the kernel's suspend exit. The human is told first that the run waits up to
that long, and the polls stay out of the live feed, which says only what
came back and when: a feed redrawn with the same poll every second for half
a minute looked like a run stuck in a loop (the M2 Max, 2026-09-27, whose
Thunderbolt networking didn't come back, so the wait ran its full length). Each passes when everything came back, with how
long it took; skipped when nothing of the kind was up before. Only interface
names and counts are printed, never an address.

The run survives the suspend: the process sleeps with the Mac and carries on.
If it's stopped anyway (the terminal closed, the Mac had to be started again),
the step it was in is in the checkpoint (Context.progress): the next run
offers to resume and judges that step from the system log since it began, or
fails it when the Mac was started again (a different boot id) in the
meantime. Finished steps aren't asked again.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from . import human
from .host import LONG_RUNNING
from .session import Context
from .ui import Ui, note, unlogged

LID_SUSPEND = "sleep.lid-suspend"
CLAMSHELL = "sleep.clamshell"
WIFI_AFTER = "sleep.wifi-after-resume"
THUNDERBOLT_AFTER = "sleep.thunderbolt-after-resume"
CHECK_IDS = (LID_SUSPEND, CLAMSHELL, WIFI_AFTER, THUNDERBOLT_AFTER)

CLOSE_SECONDS = 60   # to close the lid after answering y (15 s was too short to reach for it: M2 Max, 2026-09-27)
CLOSED_SECONDS = 20  # the lid may stay closed (with the Mac awake) this long before the watch stops
AFTER_OPEN_SECONDS = 3  # the displays are watched this long after the lid opens
CLAMSHELL_MIN_CLOSED = 3.0  # a clamshell closed for less says nothing
RECOVERY_SECONDS = 30
RECOVERY_POLLS = 40
BOOT_ID = "/proc/sys/kernel/random/boot_id"
BUILT_IN = ("eDP", "LVDS", "DSI")

# Hyprland's monitors as "connector=on|off ..." (off: disabled or its DPMS off), nothing else about them.
MONITORS_SCRIPT = (
    "hyprctl monitors all 2>/dev/null | awk '"
    'function out() { if (n != "") printf "%s=%s ", n, (d == "true" || p == "0") ? "off" : "on" } '
    '/^Monitor / { out(); n = $2; d = ""; p = "" } $1 == "disabled:" { d = $2 } $1 == "dpmsStatus:" { p = $2 } END { out() }'
    "'"
)

# Half a second, as `tick TEXT` in the watches below: with $tty the terminal's width (0: not on one), TEXT on
# one line of stderr, cut to fit, and any key returns 0 (the human skipped); `read -t` is bash's, and any
# other sh just sleeps.
TICK_FUNCTION = LONG_RUNNING + r"""tick() {
  if [ "$tty" = 0 ]; then sleep 0.5; return 1; fi
  printf "\r\033[2K%.$((tty - 1))s" "$1" >&2
  if [ -z "$1" ]; then sleep 0.5; return 1; fi
  read -r -s -n 1 -t 0.5 key 2>/dev/null
  r=$?
  if [ "$r" = 0 ]; then return 0; fi
  [ "$r" -gt 128 ] || sleep 0.5
  return 1
}
done_ticking() { if [ "$tty" != 0 ]; then printf '\r\033[2K' >&2; fi; }
"""

LID_ANGLE = "lid_angle"  # in the shared state

# Run as `sh -c SCRIPT sh CLOSE CLOSED AFTER TTY LABEL` (seconds, counted in 0.5 s polls; TTY the terminal's
# width, 0 off one; LABEL the countdown's first words). Times are hundredths of a second since "start", the wall clock in nanoseconds.
LID_WATCH_SCRIPT = TICK_FUNCTION + r"""wait=$(($1 * 2)) closed=$(($2 * 2)) after=$(($3 * 2)) tty=$4 label=$5
start=$(date +%s%N)
echo "start $start"
state=open seen=0 n=0 last= angle=
while :; do
  t=$(( ($(date +%s%N) - start) / 10000000 ))
  case "$(busctl get-property org.freedesktop.login1 /org/freedesktop/login1 org.freedesktop.login1.Manager LidClosed 2>/dev/null)" in
    "b true") lid=closed ;;
    "b false") lid=open ;;
    *) done_ticking; echo "nolid $t"; exit 0 ;;
  esac
  m=$(@MONITORS@)
  if [ "$lid" != "$state" ]; then echo "$lid $t"; state=$lid n=0; if [ "$lid" = closed ]; then seen=1; fi; fi
  if [ "$m" != "$last" ]; then echo "monitors $t $m"; last=$m; fi
  a=$(cat /sys/bus/iio/devices/iio:device*/in_angl_raw 2>/dev/null | head -n 1)
  if [ -n "$a" ] && [ "$a" != "$angle" ]; then echo "angle $t $a"; angle=$a; fi
  n=$((n + 1))
  if [ "$seen" = 0 ] && [ "$n" -ge "$wait" ]; then done_ticking; echo "timeout $t"; exit 0; fi
  if [ "$state" = closed ] && [ "$n" -ge "$closed" ]; then done_ticking; echo "timeout $t"; exit 0; fi
  if [ "$seen" = 1 ] && [ "$state" = open ] && [ "$n" -ge "$after" ]; then done_ticking; echo "end $t"; exit 0; fi
  if [ "$seen" = 0 ]; then
    if tick "$label: close the lid now, $(( (wait - n + 1) / 2 )) s left (any key skips)"; then done_ticking; echo "skipped $t"; exit 0; fi
  else
    tick ""
  fi
done""".replace("@MONITORS@", MONITORS_SCRIPT)

# Run as `sh -c SCRIPT sh SINCE` (Unix seconds): this boot's log since then, as event words and Unix times.
JOURNAL_SCRIPT = r"""journalctl --boot=0 --since "@$1" --output=short-unix --no-pager 2>/dev/null | awk '
  { t = $1 }
  / systemd-logind\[[0-9]+\]: Lid closed\./ { print "lid-closed", t }
  / systemd-logind\[[0-9]+\]: Lid opened\./ { print "lid-opened", t }
  / systemd-logind\[[0-9]+\]: (Suspending\.\.\.|The system will suspend now!)/ { print "logind-suspend", t }
  / kernel: PM: suspend entry \(/ { m = $0; sub(/.*suspend entry \(/, "", m); sub(/\).*/, "", m); print "suspend-entry", t, m }
  / kernel: PM: suspend exit/ { print "suspend-exit", t }
  / kernel: PM: Some devices failed to suspend/ || /: Failed to put system to sleep/ { print "suspend-failed", t }
  / kernel: [^ ]*(thunderbolt|acio|atcphy)/ && /(error|disconnected|timeout|timed out)/ { print "thunderbolt-error", t }
  END { print "lines", NR }'"""

# Run as `sh -c SCRIPT`: the wall clock, Wi-Fi and Thunderbolt networking interfaces, Thunderbolt/USB4 devices.
LINKS_SCRIPT = r"""echo "time $(date +%s%N)"
for d in /sys/class/net/*; do
  [ -e "$d" ] || continue
  i=${d##*/}
  if [ -d "$d/wireless" ]; then
    n=$(ip -4 -o address show dev "$i" 2>/dev/null | grep -v " inet 169\.254\." | grep -c " inet ")
    echo "wifi $i $(cat "$d/operstate" 2>/dev/null) $n"
  else
    case "$(readlink "$d/device/driver" 2>/dev/null)" in
      */thunderbolt-net|*/thunderbolt_net) echo "tbnet $i $(cat "$d/operstate" 2>/dev/null)" ;;
    esac
  fi
done
n=0
for d in /sys/bus/thunderbolt/devices/*; do
  case ${d##*/} in *-0|*[!0-9-]*) ;; [0-9]*-[0-9]*) n=$((n + 1)) ;; esac
done
echo "tbdevices $n"
"""

LOGIND_PROPERTIES = ("Docked", "HandleLidSwitch", "HandleLidSwitchDocked")
LOGIND = [
    "busctl", "get-property", "org.freedesktop.login1", "/org/freedesktop/login1", "org.freedesktop.login1.Manager",
    *LOGIND_PROPERTIES,
]
MONITORS = ["sh", "-c", MONITORS_SCRIPT]
LINKS = ["sh", "-c", LINKS_SCRIPT]
SLEEP_ONE = ["sleep", "1"]

SUSPEND_WARNING = (
    "Sleep: unplug any external display first. When you answer y, close the lid (the run waits up to "
    f"{CLOSE_SECONDS} s for it), count to ten, and open it again: the Mac should go to sleep and wake up. "
    f"After it wakes up the run waits up to {RECOVERY_SECONDS} s for Wi-Fi and Thunderbolt to come back. "
    "If it doesn't carry on after the lid opens, run omarchy-m-test again: it resumes here."
)
SUSPEND_QUESTION = "Close the lid to put the Mac to sleep now?"
CLAMSHELL_CONNECT = (
    "Clamshell: connect an external display (USB-C or HDMI) and wait until the desktop shows on it. Is it showing?"
)
CLAMSHELL_WARNING = (
    "Keep the lid open for now. When you answer y, close the lid (the run waits up to "
    f"{CLOSE_SECONDS} s for it), wait about ten seconds and open it again: "
    "the Mac should stay awake on the external display, with the built-in screen off."
)
CLAMSHELL_QUESTION = "Close the lid with the external display on now?"
# logind logs every lid switch; none at all means this user can't read the system's log, only their own.
NO_LID_EVENTS = (
    "the system log shows no lid events, so a suspend can't be seen (reading it needs the wheel, adm or systemd-journal group)"
)
RECOVERED_NOTE = "The last run stopped during the lid check; its result is read from the system log."
RECOVERY_WAIT = (
    f"The Mac woke up. Waiting up to {RECOVERY_SECONDS} s for what was up before it slept to come back "
    "({what}); the run carries on by itself."
)
LID_SKIPPED = "you skipped closing the lid"


# -- what the Mac printed -------------------------------------------------------------

@dataclass
class Lid:
    """What LID_WATCH_SCRIPT saw; times in seconds since it started."""

    start: float | None = None  # Unix time
    closed: float | None = None
    opened: float | None = None
    monitors: list[tuple[float, dict[str, bool]]] = field(default_factory=list)
    angles: list[float] = field(default_factory=list)  # the lid-angle sensor's raw readings, as they changed
    end: str = ""  # "end", "timeout", "nolid" or "skipped" (a key while waiting for the lid)

    def first_displays(self) -> dict[str, bool] | None:
        """The monitors when the watch started."""
        return self.monitors[0][1] if self.monitors else None

    def displays(self, until: float | None = None, after: float | None = None, at: float | None = None) -> dict[str, bool] | None:
        """The last reading before `until` (or at `at`, inclusive), counting only readings after `after`.

        A reading taken in the same poll as the lid opening shares its time: `until` leaves it out.
        """
        found = None
        for t, state in self.monitors:
            if (until is not None and t >= until) or (at is not None and t > at):
                break
            if after is None or t > after:
                found = state
        return found

    def epoch(self, t: float | None) -> float | None:
        return None if t is None or self.start is None else self.start + t


_ANGLE = re.compile(r"-?[0-9]{1,12}(\.[0-9]{1,6})?")


def parse_lid(stdout: str) -> Lid:
    lid = Lid()
    for line in stdout.splitlines():
        word, _, rest = line.strip().partition(" ")
        value, _, tail = rest.partition(" ")
        if word == "start" and value.isdigit():
            lid.start = int(value) / 1e9
            continue
        if not value.isdigit():
            continue
        t = int(value) / 100
        if word == "closed" and lid.closed is None:
            lid.closed = t
        elif word == "open" and lid.closed is not None and lid.opened is None:
            lid.opened = t
        elif word == "monitors":
            lid.monitors.append((t, _monitors(tail)))
        elif word == "angle" and _ANGLE.fullmatch(tail):
            lid.angles.append(float(tail))
        elif word in ("end", "timeout", "nolid", "skipped"):
            lid.end = word
    return lid


def _monitors(text: str) -> dict[str, bool]:
    found = {}
    for item in text.split():
        name, _, state = item.partition("=")
        if re.fullmatch(r"[A-Za-z]+(?:-[A-Za-z]+)*-\d+", name) and state in ("on", "off"):
            found[name] = state == "on"
    return found


def built_in(name: str) -> bool:
    return name.startswith(BUILT_IN)


def describe(displays: dict[str, bool]) -> str:
    return ", ".join(f"{name} {'on' if on else 'off'}" for name, on in displays.items()) or "no monitors"


def externals_on(displays: dict[str, bool] | None) -> list[str]:
    return [name for name, on in (displays or {}).items() if on and not built_in(name)]


@dataclass
class Journal:
    events: list[tuple[str, float, str]] = field(default_factory=list)
    lines: int = 0

    def first(self, word: str, after: float | None = None, before: float | None = None) -> tuple[float, str] | None:
        for kind, t, extra in self.events:
            if kind == word and (after is None or t >= after) and (before is None or t <= before):
                return t, extra
        return None

    def count(self, word: str, after: float | None = None) -> int:
        return sum(1 for kind, t, _ in self.events if kind == word and (after is None or t >= after))


def parse_journal(stdout: str) -> Journal:
    journal = Journal()
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "lines" and parts[1].isdigit():
            journal.lines = int(parts[1])
            continue
        if len(parts) < 2:
            continue
        try:
            t = float(parts[1])
        except ValueError:
            continue
        extra = parts[2] if len(parts) > 2 and re.fullmatch(r"[a-z0-9]+", parts[2]) else ""
        journal.events.append((parts[0], t, extra))
    return journal


@dataclass
class Links:
    """What LINKS_SCRIPT saw."""

    time: float | None = None  # Unix time
    wifi: dict[str, tuple[bool, int]] = field(default_factory=dict)  # interface: (up, IPv4 addresses)
    tbnet: dict[str, bool] = field(default_factory=dict)  # Thunderbolt networking interface: up
    tbdevices: int = 0

    def wifi_connected(self) -> list[str]:
        return [name for name, (up, addresses) in self.wifi.items() if up and addresses]

    def tbnet_up(self) -> list[str]:
        return [name for name, up in self.tbnet.items() if up]

    def to_json(self) -> dict[str, Any]:
        return {"time": self.time, "wifi": {k: list(v) for k, v in self.wifi.items()}, "tbnet": self.tbnet, "tbdevices": self.tbdevices}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Links":
        return cls(data.get("time"), {k: (bool(v[0]), int(v[1])) for k, v in data.get("wifi", {}).items()},
                   {k: bool(v) for k, v in data.get("tbnet", {}).items()}, int(data.get("tbdevices", 0)))


def parse_links(stdout: str) -> Links:
    links = Links()
    for line in stdout.splitlines():
        parts = line.split()
        if parts[:1] == ["time"] and len(parts) == 2 and parts[1].isdigit():
            links.time = int(parts[1]) / 1e9
        elif parts[:1] == ["wifi"] and len(parts) == 4 and parts[3].isdigit():
            links.wifi[parts[1]] = (parts[2] == "up", int(parts[3]))
        elif parts[:1] == ["tbnet"] and len(parts) == 3:
            links.tbnet[parts[1]] = parts[2] == "up"
        elif parts[:1] == ["tbdevices"] and len(parts) == 2 and parts[1].isdigit():
            links.tbdevices = int(parts[1])
    return links


def lid_watch_argv(close: int = CLOSE_SECONDS, closed: int = CLOSED_SECONDS, after: int = AFTER_OPEN_SECONDS,
                   tty: int = 0, label: str = "") -> list[str]:
    return ["sh", "-c", LID_WATCH_SCRIPT, "sh", str(close), str(closed), str(after), str(tty), label]


def watch_lid(ctx: Context, label: str, closed: int = CLOSED_SECONDS) -> Lid:
    """The lid watch: on the terminal when there is one (a countdown, and any key skips), else in the background."""
    ui = _ui(ctx)
    terminal = ctx.host.terminal()
    if terminal is not None:
        pad = getattr(ui, "pad", "")
        return parse_lid(ctx.host.run_tty(lid_watch_argv(closed=closed, tty=terminal.width, label=pad + label)).stdout)
    return parse_lid(ctx.host.run(lid_watch_argv(closed=closed)).stdout)


def not_closed(lid: Lid, stopped: bool) -> str:
    """Why a step whose lid never closed says nothing."""
    if stopped:
        return "the run stopped before the lid was closed"
    if lid.end == "skipped":
        return LID_SKIPPED
    return f"the lid wasn't closed within {CLOSE_SECONDS} s"


def journal_argv(since: int) -> list[str]:
    return ["sh", "-c", JOURNAL_SCRIPT, "sh", str(since)]


# -- the section -----------------------------------------------------------------------

@dataclass
class Step:
    """One lid step, as the checkpoint keeps it while the lid may be closed."""

    name: str  # "suspend" or "clamshell"
    since: int  # Unix seconds, a little before the prompt was answered
    boot: str | None
    links: Links
    before: dict[str, Any] = field(default_factory=dict)  # displays and logind's settings before the lid closed

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "since": self.since, "boot": self.boot, "links": self.links.to_json(), "before": self.before}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Step":
        return cls(str(data["name"]), int(data["since"]), data.get("boot"), Links.from_json(data.get("links", {})), dict(data.get("before", {})))


def run(ctx: Context) -> list[dict]:
    progress = ctx.progress("sleep")
    results: dict[str, dict] = progress.setdefault("results", {})
    if not isinstance(results, dict) or not all(isinstance(r, dict) and r.get("id") == k for k, r in results.items()):
        results = progress["results"] = {}
    if "step" in progress:
        _recover(ctx, progress, results)
    if human.absent(ctx, CLAMSHELL):
        reason = "this Mac has no lid"
        for check_id in (LID_SUSPEND, CLAMSHELL):
            results.setdefault(check_id, _result(check_id, "skip", [f"skipped: {reason}"]))
    if LID_SUSPEND not in results:
        _suspend(ctx, progress, results)
    if CLAMSHELL not in results:
        _clamshell(ctx, progress, results)
    why = "the Mac didn't go to sleep and wake up in this run"
    for check_id in (WIFI_AFTER, THUNDERBOLT_AFTER):
        results.setdefault(check_id, _result(check_id, "skip", [f"skipped: {why}"]))
    return [results[check_id] for check_id in CHECK_IDS]


def _note_angle(ctx: Context, lid: Lid) -> None:
    """For the Sensors section, which runs later: what the lid-angle sensor did while the lid closed."""
    if lid.closed is not None and lid.angles:
        ctx.shared[LID_ANGLE] = {"before": lid.angles[0], "lowest": min(lid.angles)}


def _ui(ctx: Context) -> Ui:
    return ctx.ui or Ui(ctx.host)


def _suspend(ctx: Context, progress: dict, results: dict) -> None:
    ui = _ui(ctx)
    ui.text(SUSPEND_WARNING)
    if not ui.confirm(SUSPEND_QUESTION):
        results[LID_SUSPEND] = _result(LID_SUSPEND, "skip", ["skipped: you chose not to close the lid"])
        return
    step = _begin(ctx, progress, "suspend", {"logind": _logind(ctx)})
    lid = watch_lid(ctx, "Sleep")
    _note_angle(ctx, lid)
    journal = read_journal(ctx, step.since)
    results[LID_SUSPEND] = judge_suspend(lid, journal, step.before.get("logind", {}))
    _after_resume(ctx, results, step, _resumed(lid, journal))
    _finish(ctx, progress)


def _clamshell(ctx: Context, progress: dict, results: dict) -> None:
    system = ctx.system
    if system is not None and not system.is_omarchy:
        results[CLAMSHELL] = _result(CLAMSHELL, "skip", [f"skipped: reference run on {system.distro}: Omarchy integration isn't checked"])
        return
    ui = _ui(ctx)
    if not ui.confirm(CLAMSHELL_CONNECT):
        results[CLAMSHELL] = _result(CLAMSHELL, "skip", ["skipped: no external display was connected"])
        return
    displays = _monitors(ctx.host.run(MONITORS).stdout)
    if not externals_on(displays):
        results[CLAMSHELL] = _result(CLAMSHELL, "skip", [
            f"displays: {describe(displays)}", "skipped: Hyprland shows no external display on",
        ])
        return
    ui.text(CLAMSHELL_WARNING)
    if not ui.confirm(CLAMSHELL_QUESTION):
        results[CLAMSHELL] = _result(CLAMSHELL, "skip", ["skipped: you chose not to close the lid"])
        return
    step = _begin(ctx, progress, "clamshell", {"displays": displays, "logind": _logind(ctx)})
    lid = watch_lid(ctx, "Clamshell")
    journal = read_journal(ctx, step.since)
    results[CLAMSHELL] = judge_clamshell(lid, journal, displays, step.before.get("logind", {}))
    _after_resume(ctx, results, step, _resumed(lid, journal))
    _finish(ctx, progress)


def _begin(ctx: Context, progress: dict, name: str, before: dict[str, Any]) -> Step:
    """Checkpoint the step before the lid closes: a run stopped while it's closed picks it up from here."""
    links = parse_links(ctx.host.run(LINKS).stdout)
    since = int(links.time) - 1 if links.time is not None else 0
    step = Step(name, since, boot_id(ctx), links, before)
    progress["step"] = step.to_json()
    ctx.changes.persist()
    return step


def _finish(ctx: Context, progress: dict) -> None:
    progress.pop("step", None)
    ctx.changes.persist()


def _recover(ctx: Context, progress: dict, results: dict) -> None:
    """The run stopped during a lid step: judge it from the system log since it began."""
    try:
        step = Step.from_json(progress["step"])
    except (KeyError, TypeError, ValueError, AttributeError, IndexError):
        progress.pop("step", None)  # not a step this tool wrote: the section runs from the start
        return
    check_id = LID_SUSPEND if step.name == "suspend" else CLAMSHELL
    _ui(ctx).text(RECOVERED_NOTE)
    boot = boot_id(ctx)
    if step.boot and boot and boot != step.boot:
        results[check_id] = _result(check_id, "fail", [
            "the run stopped while the lid was closed for this check, and the Mac was started again since: "
            "it didn't come back from sleep",
        ])
        _finish(ctx, progress)
        return
    journal = read_journal(ctx, step.since)
    if check_id == LID_SUSPEND:
        results[check_id] = judge_suspend(Lid(), journal, step.before.get("logind", {}), stopped=True)
    else:
        results[check_id] = judge_clamshell(Lid(), journal, step.before.get("displays", {}), step.before.get("logind", {}), stopped=True)
    _after_resume(ctx, results, step, _resumed(Lid(), journal))
    _finish(ctx, progress)


def boot_id(ctx: Context) -> str | None:
    try:
        return ctx.host.read_file(BOOT_ID).decode("ascii", "replace").strip() or None
    except OSError:
        return None


def _logind(ctx: Context) -> dict[str, str]:
    answer = ctx.host.run(LOGIND)
    if answer.returncode != 0:
        return {}
    values = [line.split(" ", 1)[-1].strip().strip('"') for line in answer.stdout.splitlines() if line.strip()]
    return dict(zip(LOGIND_PROPERTIES, values)) if len(values) == len(LOGIND_PROPERTIES) else {}


def read_journal(ctx: Context, since: int) -> Journal | None:
    journal = parse_journal(ctx.host.run(journal_argv(since)).stdout)
    return journal if journal.lines else None


# -- judging ------------------------------------------------------------------------------

@dataclass
class Sleep:
    closed: float | None  # Unix times
    entry: float | None = None
    mode: str = ""
    exit: float | None = None
    failed: float | None = None


def asleep(lid: Lid, journal: Journal, window_end: float | None = None) -> Sleep:
    """When the lid closed and whether (and how) the kernel suspended after that, before `window_end`."""
    logged = journal.first("lid-closed", after=lid.epoch(lid.closed) - 2 if lid.closed is not None and lid.start else None)
    closed = logged[0] if logged else lid.epoch(lid.closed)
    after = closed - 1 if closed is not None else None
    entry = journal.first("suspend-entry", after=after, before=window_end)
    failed = journal.first("suspend-failed", after=after, before=window_end)
    found = Sleep(closed, failed=failed[0] if failed else None)
    if entry:
        found.entry, found.mode = entry[0], entry[1]
        exit_ = journal.first("suspend-exit", after=entry[0])
        found.exit = exit_[0] if exit_ else None
    return found


def _resumed(lid: Lid, journal: Journal | None) -> float | None:
    """When the Mac woke up (Unix time), if it went to sleep in this step."""
    if journal is None:
        return None
    found = asleep(lid, journal)
    if found.entry is None:
        return None
    return found.exit or found.entry


def _logind_line(logind: dict[str, str]) -> str | None:
    if not logind:
        return None
    return "logind: " + ", ".join(f"{k}={'yes' if v == 'true' else 'no' if v == 'false' else v}" for k, v in logind.items())


def judge_suspend(lid: Lid, journal: Journal | None, logind: dict[str, str], stopped: bool = False) -> dict:
    if journal is None:
        return _result(LID_SUSPEND, "skip", ["skipped: the system log couldn't be read, so suspend and resume can't be seen"])
    if lid.end == "nolid":
        return _result(LID_SUSPEND, "skip", ["skipped: logind can't say whether the lid is open or closed"])
    first = lid.first_displays()
    evidence = [f"before: {describe(first)}"] if first is not None else []
    found = asleep(lid, journal)
    if found.closed is None:
        return _result(LID_SUSPEND, "skip", [*evidence, f"skipped: {not_closed(lid, stopped)}"])
    if found.entry is None and journal.first("lid-closed") is None:
        return _result(LID_SUSPEND, "skip", [*evidence, f"skipped: {NO_LID_EVENTS}"])
    if found.entry is not None:
        evidence.append(f"lid closed: the Mac went to sleep {max(found.entry - found.closed, 0):.2f} s later ({found.mode or 'mode not logged'})")
        if found.exit is not None:
            evidence.append(f"woke up after {found.exit - found.entry:.1f} s asleep" + (", when the lid opened" if journal.first("lid-opened", after=found.exit - 5) or lid.opened is not None else ""))
        else:
            evidence.append("the kernel logged no suspend exit, but the run carried on")
        if stopped:
            evidence.append("read from the system log: the run had stopped while the lid was closed")
        return _result(LID_SUSPEND, "pass", evidence)
    if found.failed is not None:
        return _result(LID_SUSPEND, "fail", [*evidence, "lid closed: the Mac tried to go to sleep and failed (the system log says a device failed to suspend)"])
    if stopped:
        return _result(LID_SUSPEND, "skip", [*evidence, "skipped: the run stopped before the Mac could be seen going to sleep"])
    external = externals_on(first)
    if external:
        return _result(LID_SUSPEND, "skip", [*evidence, f"skipped: an external display was on ({', '.join(external)}), so the Mac is meant to stay awake; unplug it for this check"])
    handle = logind.get("HandleLidSwitch")
    if handle in ("ignore", "lock"):
        return _result(LID_SUSPEND, "skip", [*evidence, f"skipped: logind is set not to sleep on the lid (HandleLidSwitch={handle})"])
    end = lid.opened if lid.opened is not None else (lid.monitors[-1][0] if lid.monitors else lid.closed)
    closed_for = max((end or 0) - (lid.closed or 0), 0)
    line = _logind_line(logind)
    return _result(LID_SUSPEND, "fail", [*evidence, *([line] if line else []), f"lid closed: the Mac didn't go to sleep in the {closed_for:.1f} s it was closed"])


def judge_clamshell(lid: Lid, journal: Journal | None, before: dict[str, bool], logind: dict[str, str], stopped: bool = False) -> dict:
    external = externals_on(before)
    evidence = [f"before: {describe(before)}"]
    line = _logind_line(logind)
    if line:
        evidence.append(line)
    if journal is None:
        return _result(CLAMSHELL, "skip", [*evidence, "skipped: the system log couldn't be read, so a suspend can't be seen"])
    if lid.end == "nolid":
        return _result(CLAMSHELL, "skip", [*evidence, "skipped: logind can't say whether the lid is open or closed"])
    found = asleep(lid, journal, window_end=lid.epoch(lid.opened))
    if found.closed is None:
        return _result(CLAMSHELL, "skip", [*evidence, f"skipped: {not_closed(lid, stopped)}"])
    if found.entry is None and journal.first("lid-closed") is None:
        return _result(CLAMSHELL, "skip", [*evidence, f"skipped: {NO_LID_EVENTS}"])
    closed_view = lid.displays(until=lid.opened, after=lid.closed) if lid.closed is not None else None
    if found.entry is not None:
        evidence.append(f"lid closed: the Mac went to sleep {max(found.entry - found.closed, 0):.2f} s later ({found.mode or 'mode not logged'}), with {', '.join(external)} on")
        if closed_view is not None:
            evidence.append(f"while the lid was closed: {describe(closed_view)}")
        if found.exit is not None:
            evidence.append(f"woke up after {found.exit - found.entry:.1f} s asleep")
        usb = [name for name in external if name.startswith("USB-")]
        if usb and logind.get("Docked") == "false":
            evidence.append(
                f"logind didn't count {', '.join(usb)} as an external display (DRM connector type USB, as USB-C displays are "
                "on Apple Silicon), so it wasn't docked and HandleLidSwitchDocked didn't apply"
            )
        if stopped:
            evidence.append("read from the system log: the run had stopped while the lid was closed")
        return _result(CLAMSHELL, "fail", evidence)
    if stopped:
        return _result(CLAMSHELL, "skip", [*evidence, "skipped: the run stopped before the displays could be seen with the lid closed"])
    end = lid.opened if lid.opened is not None else (lid.monitors[-1][0] if lid.monitors else lid.closed)
    closed_for = max((end or 0) - (lid.closed or 0), 0)
    if closed_for < CLAMSHELL_MIN_CLOSED:
        return _result(CLAMSHELL, "skip", [*evidence, f"skipped: the lid was open again after {closed_for:.1f} s; keep it closed for about ten seconds"])
    evidence.append(f"lid closed for {closed_for:.1f} s: the Mac stayed awake")
    problems = []
    if closed_view is None:
        closed_view = lid.displays(at=lid.closed)
    evidence.append(f"while the lid was closed: {describe(closed_view or {})}")
    if any(on for name, on in (closed_view or {}).items() if built_in(name)):
        problems.append("the built-in screen stayed on with the lid closed")
    if not externals_on(closed_view):
        problems.append("no external display was on with the lid closed")
    if lid.opened is not None:
        after_view = lid.displays(after=lid.opened - 0.001) or lid.displays()
        evidence.append(f"after the lid opened: {describe(after_view or {})}")
        if not any(on for name, on in (after_view or {}).items() if built_in(name)):
            problems.append(f"the built-in screen didn't come back within {AFTER_OPEN_SECONDS} s of the lid opening")
    return _result(CLAMSHELL, "fail" if problems else "pass", [*evidence, *problems])


# -- Wi-Fi and Thunderbolt after resume --------------------------------------------------

def _after_resume(ctx: Context, results: dict, step: Step, resumed: float | None) -> None:
    """After the first time the Mac woke up in this run: poll until what was up before is back."""
    if resumed is None or WIFI_AFTER in results:
        return
    before = step.links
    wifi, tb = before.wifi_connected(), before.tbnet_up()
    what = _awaited(before)
    if what:
        _ui(ctx).text(RECOVERY_WAIT.format(what=what))
    quiet = unlogged(ctx.host)  # the feed says what came back, not every poll (see the module docstring)
    polls: list[Links] = []
    seen = None
    for attempt in range(RECOVERY_POLLS):
        now = parse_links(quiet.run(LINKS).stdout)
        polls.append(now)
        elapsed = (now.time - resumed) if now.time is not None else attempt
        state = _state(now, before)
        if state != seen:
            note(ctx.host, f"{max(elapsed, 0):.0f} s after waking up: {state}")
            seen = state
        if (_wifi_back(now, wifi) and _tb_back(now, before)) or elapsed >= RECOVERY_SECONDS:
            break
        quiet.run(SLEEP_ONE)
    results[WIFI_AFTER] = judge_wifi(before, polls, resumed)
    results[THUNDERBOLT_AFTER] = judge_thunderbolt(before, polls, resumed, _thunderbolt_errors(ctx, step.since, resumed))


def _awaited(before: Links) -> str:
    """What the recovery wait waits for, in words ("" when nothing was up)."""
    parts = [f"{name} with an address" for name in before.wifi_connected()]
    parts += [f"{name} (Thunderbolt networking)" for name in before.tbnet_up()]
    if before.tbdevices:
        parts.append(f"{before.tbdevices} Thunderbolt/USB4 device(s)")
    return ", ".join(parts)


def _state(now: Links, before: Links) -> str:
    """What of `before` is up in `now`, in words, for the feed."""
    parts = [f"{name} {'connected' if name in now.wifi_connected() else 'no address'}" for name in before.wifi_connected()]
    parts += [f"{name} {'up' if name in now.tbnet_up() else 'down'}" for name in before.tbnet_up()]
    if before.tbdevices:
        parts.append(f"{now.tbdevices} of {before.tbdevices} Thunderbolt/USB4 device(s)")
    return ", ".join(parts) or "nothing to wait for"


def _thunderbolt_errors(ctx: Context, since: int, resumed: float) -> int:
    journal = read_journal(ctx, since)
    return journal.count("thunderbolt-error", after=resumed - 5) if journal else 0


def _wifi_back(now: Links, wifi: list[str]) -> bool:
    return all(name in now.wifi_connected() for name in wifi)


def _tb_back(now: Links, before: Links) -> bool:
    return all(name in now.tbnet_up() for name in before.tbnet_up()) and now.tbdevices >= before.tbdevices


def _first(polls: list[Links], back, resumed: float) -> float | None:
    for index, now in enumerate(polls):
        if back(now):
            return max(now.time - resumed, 0) if now.time is not None else float(index)
    return None


def _waited(polls: list[Links], resumed: float) -> float:
    last = polls[-1] if polls else None
    return max(last.time - resumed, 0) if last is not None and last.time is not None else float(len(polls))


def judge_wifi(before: Links, polls: list[Links], resumed: float) -> dict:
    wifi = before.wifi_connected()
    if not wifi:
        return _result(WIFI_AFTER, "skip", ["skipped: Wi-Fi wasn't connected with an address before the Mac went to sleep"])
    evidence = [f"before sleeping: {', '.join(wifi)} connected with an IPv4 address"]
    back = _first(polls, lambda now: _wifi_back(now, wifi), resumed)
    if back is not None:
        return _result(WIFI_AFTER, "pass", [*evidence, f"after waking up: an IPv4 address again {back:.1f} s after the Mac woke up"])
    last = polls[-1] if polls else Links()
    states = ", ".join(f"{name} {'up' if last.wifi.get(name, (False, 0))[0] else 'down'} with {last.wifi.get(name, (False, 0))[1]} IPv4 address(es)" for name in wifi)
    return _result(WIFI_AFTER, "fail", [*evidence, f"after waking up: no IPv4 address {_waited(polls, resumed):.1f} s after the Mac woke up ({states})"])


def judge_thunderbolt(before: Links, polls: list[Links], resumed: float, errors: int) -> dict:
    nets = before.tbnet_up()
    if not nets and not before.tbdevices:
        return _result(THUNDERBOLT_AFTER, "skip", ["skipped: nothing was connected over Thunderbolt or USB4 before the Mac went to sleep"])
    evidence = [f"before sleeping: {before.tbdevices} Thunderbolt/USB4 device(s)" + (f", {', '.join(nets)} up (Thunderbolt networking)" if nets else "")]
    back = _first(polls, lambda now: _tb_back(now, before), resumed)
    if back is not None:
        evidence.append(f"after waking up: all back {back:.1f} s after the Mac woke up")
        if errors:
            evidence.append(f"the kernel logged {errors} Thunderbolt link error(s) or timeout(s) at wake-up")
        return _result(THUNDERBOLT_AFTER, "pass", evidence)
    last = polls[-1] if polls else Links()
    waited = _waited(polls, resumed)
    down = [name for name in nets if name not in last.tbnet_up()]
    if down:
        evidence.append(f"after waking up: {', '.join(down)} still down {waited:.1f} s after the Mac woke up")
    evidence.append(f"Thunderbolt/USB4 devices: {before.tbdevices} before, {last.tbdevices} {waited:.1f} s after waking up")
    if errors:
        evidence.append(f"the kernel logged {errors} Thunderbolt link error(s) or timeout(s) at wake-up")
    return _result(THUNDERBOLT_AFTER, "fail", evidence)


def _result(check_id: str, status: str, evidence: list[str]) -> dict:
    return {"id": check_id, "kind": "automatic", "status": status, "evidence": evidence}
