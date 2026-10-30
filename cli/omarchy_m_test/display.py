"""The Display and Input sections' live checks: the notch bar, brightness steps, the cursor and the keyboard light.

Notch bar (human): on a Mac with a notch, is the bar's every icon clear of
the camera cutout? The catalogue says which Macs have one; the others skip
it. Omarchy integration, so a reference run doesn't ask.

Brightness steps (human): the built-in panel's backlight (apple-panel-bl,
else the first backlight brightnessctl lists) is stepped three times, a
second apart, then put back; its restorer is registered before the first
step and runs again when the section ends if putting it back failed. It
steps down to 70%, 45% and 25% of where it was (never below a tenth of its
range, never off), or up when it's already dim. brightnessctl sets it as the
user through its udev rules or setuid bit, or logind; where it can't (over
SSH, without a seat), the check is skipped, and when not even the first step
took there is nothing to put back. A backlight that doesn't read back the value it was set to
fails without asking.

Cursor (human): is the pointer visible everywhere on the built-in screen,
over the bar too, without trails or flicker?

Keyboard light (human): with the ambient light sensor
(/sys/bus/iio/devices/iio:device*/in_illuminance_input) and the keyboard
light (brightnessctl's kbd_backlight) both there, the human covers the
camera and notch, where the sensor is, and says whether the keyboard light
came on. The sensor and the keys are read before and while covered, as
evidence. Omarchy integration (its light loop), so a reference run doesn't
ask.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import human, presence
from .session import Context

LIST_LIGHTS = ["brightnessctl", "--list", "--machine-readable"]
PANEL = "apple-panel-bl"
KEYBOARD = "kbd_backlight"
IIO = "/sys/bus/iio/devices"
LUX_FILE = "in_illuminance_input"
PAUSE = ["sleep", "1"]
LIGHTS_CACHE = "brightnessctl_lights"

NOTCH_QUESTION = "Look at the top bar on the built-in screen: is every icon and label clear of the camera notch?"
BRIGHTNESS_QUESTION = "Did the built-in screen dim (or brighten) in three steps, then go back to how it was?"
CURSOR_QUESTION = "Move the pointer all over the built-in screen, over the top bar too: is it visible everywhere, without trails or flicker?"
KEYBOARD_QUESTION = "Keep it covered: did the keyboard light come on, or get brighter, within about 10 seconds?"


@dataclass(frozen=True)
class Light:
    name: str
    kind: str   # brightnessctl's class: backlight, leds
    value: int
    top: int

    def describe(self) -> str:
        return f"{self.value}/{self.top}"


def parse_light(line: str) -> Light | None:
    """One line of brightnessctl --machine-readable: name,class,value,percent,max."""
    fields = line.strip().split(",")
    if len(fields) != 5 or not fields[2].isdigit() or not fields[4].isdigit():
        return None
    return Light(fields[0], fields[1], int(fields[2]), int(fields[4]))


def lights(ctx: Context) -> dict[str, Light] | str:
    """Every light brightnessctl knows, by name, read once per run; why not, when it can't list them."""
    if LIGHTS_CACHE not in ctx.cache:
        listed = ctx.host.run(LIST_LIGHTS)
        if listed.returncode != 0:
            ctx.cache[LIGHTS_CACHE] = f"brightnessctl couldn't list the lights ({_why(listed)})"
        else:
            found = [parse_light(line) for line in listed.stdout.splitlines()]
            ctx.cache[LIGHTS_CACHE] = {light.name: light for light in found if light}
    return ctx.cache[LIGHTS_CACHE]


def _say(ctx: Context, text: str) -> None:
    if ctx.ui:
        ctx.ui.text(text)
    else:
        ctx.host.show(text)


def _why(result) -> str:
    lines = (result.stderr or result.stdout).strip().splitlines()
    return lines[-1] if lines else f"exit {result.returncode}"


def _reference(ctx: Context) -> str | None:
    if ctx.system is not None and not ctx.system.is_omarchy:
        return f"reference run on {ctx.system.distro}: Omarchy integration isn't checked"
    return None


# -- the notch -------------------------------------------------------------------

def notch_bar(ctx: Context) -> dict:
    check_id = "display.notch-bar"
    if human.absent(ctx, check_id):
        return human.skip(check_id, "this Mac has no notch")
    reference = _reference(ctx)
    if reference:
        return human.skip(check_id, reference)
    return human.check(ctx, check_id, NOTCH_QUESTION)


# -- brightness ----------------------------------------------------------------------

def steps(value: int, top: int) -> list[int]:
    """Three visible steps from `value`: down when it's bright enough, else up; never off."""
    floor = max(1, top // 10)
    if value >= top * 3 // 10:
        wanted = [max(floor, value * percent // 100) for percent in (70, 45, 25)]
    else:
        wanted = [min(top, value + top * percent // 100) for percent in (15, 30, 45)]
    return [level for index, level in enumerate(wanted) if level != value and level not in wanted[:index]]


def set_argv(name: str, level: int) -> list[str]:
    return ["brightnessctl", "--machine-readable", f"--device={name}", "set", str(level)]


def brightness_steps(ctx: Context) -> dict:
    check_id = "display.brightness-steps"
    if human.unattended(ctx):
        return human.skip(check_id, f"{presence.UNATTENDED}, so the brightness wasn't changed")
    known = lights(ctx)
    if isinstance(known, str):
        return human.skip(check_id, known)
    panels = [light for light in known.values() if light.kind == "backlight"]
    if not panels:
        return human.skip(check_id, "no built-in screen backlight (brightnessctl lists none)")
    panel = next((light for light in panels if light.name == PANEL), panels[0])
    levels = steps(panel.value, panel.top)
    evidence = [f"backlight {panel.name}: {panel.describe()} at the start"]
    if len(levels) < 3:
        return human.skip(check_id, "the backlight has too few levels to step", evidence)

    back = set_argv(panel.name, panel.value)
    restorer = ctx.changes.register("the screen brightness", back)
    _say(ctx, "Watch the built-in screen: its brightness changes in three steps, then goes back.")
    read_back = []
    for level in levels:
        result = ctx.host.run(set_argv(panel.name, level))
        if result.returncode != 0:
            # Nothing changed when the first step failed (no permission over SSH, say): nothing to put back.
            if not read_back or ctx.host.run(back).returncode == 0:
                ctx.changes.replace(restorer, None)
            return human.skip(check_id, f"brightnessctl couldn't set the backlight ({_why(result)})", evidence)
        now = next((light for light in map(parse_light, result.stdout.splitlines()) if light), None)
        read_back.append((level, now.value if now else None))
        ctx.host.run(PAUSE)
    evidence.append("stepped to " + ", ".join(f"{level} (read back {'?' if got is None else got})" for level, got in read_back))
    if ctx.host.run(back).returncode == 0:
        ctx.changes.replace(restorer, None)
        evidence.append(f"put back to {panel.value}")
    wrong = [level for level, got in read_back if got is not None and got != level]
    if wrong:
        return {"id": check_id, "kind": "human", "status": "fail",
                "evidence": [*evidence, f"not asked: the backlight didn't take {', '.join(map(str, wrong))}"]}
    return human.check(ctx, check_id, BRIGHTNESS_QUESTION, evidence)


# -- the cursor ------------------------------------------------------------------------

def cursor(ctx: Context) -> dict:
    return human.check(ctx, "display.cursor", CURSOR_QUESTION)


# -- the keyboard light ------------------------------------------------------------------

def lux_file(ctx: Context) -> str | None:
    """The ambient light sensor's reading file, if there is one."""
    try:
        devices = ctx.host.list_dir(IIO)
    except OSError:
        return None
    for device in devices:
        if device.startswith("iio:device"):
            path = f"{IIO}/{device}/{LUX_FILE}"
            try:
                ctx.host.read_file(path)
            except OSError:
                continue
            return path
    return None


def read_lux(ctx: Context, path: str) -> str:
    try:
        text = ctx.host.read_file(path).decode("ascii", "replace").strip()
        return f"{float(text):.0f}"
    except (OSError, ValueError):
        return "unreadable"


def keyboard_light_now(ctx: Context) -> Light | None:
    result = ctx.host.run(["brightnessctl", "--machine-readable", f"--device={KEYBOARD}", "info"])
    return next((light for light in map(parse_light, result.stdout.splitlines()) if light), None) if result.returncode == 0 else None


def keyboard_now(ctx: Context) -> str:
    light = keyboard_light_now(ctx)
    return light.describe() if light else "unreadable"


def keyboard_light(ctx: Context) -> dict:
    check_id = "input.keyboard-light-follows-room"
    reference = _reference(ctx)
    if reference:
        return human.skip(check_id, reference)
    sensor = lux_file(ctx)
    if sensor is None:
        return human.skip(check_id, "no ambient light sensor")
    known = lights(ctx)
    if isinstance(known, str):
        return human.skip(check_id, known)
    if KEYBOARD not in known:
        return human.skip(check_id, "no keyboard light (brightnessctl lists no kbd_backlight)")
    if human.unattended(ctx):
        return human.skip(check_id, presence.UNATTENDED)
    lux_before, light_before = read_lux(ctx, sensor), keyboard_light_now(ctx)
    before = f"before: ambient light {lux_before} lux, keyboard light {light_before.describe() if light_before else 'unreadable'}"
    _say(ctx, "Cover the camera and notch at the top of the screen with your hand, where the light sensor is, and keep it covered.")
    result = human.check(ctx, check_id, KEYBOARD_QUESTION, [before])
    lux_after, light_after = read_lux(ctx, sensor), keyboard_light_now(ctx)
    result["evidence"].append(f"while covered: ambient light {lux_after} lux, keyboard light {light_after.describe() if light_after else 'unreadable'}")
    _say(ctx, "You can uncover it now.")
    if light_before and light_after:
        readings = f"ambient light {lux_before} to {lux_after} lux, keyboard light {light_before.describe()} to {light_after.describe()}"
        if result["status"] == "pass" and light_after.value <= light_before.value:
            human.not_backed(result, f"the keyboard light didn't come on or get brighter: {readings}")
        elif result["status"] == "fail" and light_after.value > light_before.value:
            human.not_backed(result, f"the keyboard light got brighter: {readings}")
    return result


def display(ctx: Context) -> list[dict]:
    return [notch_bar(ctx), brightness_steps(ctx), cursor(ctx)]
