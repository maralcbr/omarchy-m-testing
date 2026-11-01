"""The Sensors section: motion, lid angle and light, read at rest, with nothing for the human to do.

  sensors.motion     the accelerometer's magnitude is gravity (9.8 m/s² ±15%) and
                     the gyroscope is near still. No accelerometer, or channels
                     that don't read, fail and say why. Desktop Macs skip.
  sensors.lid-angle  a reading within 0-360°. If the Sleep section watched the
                     lid close in this run, the reading has to have fallen too.
                     Macs without a lid skip.
  sensors.light      ambient light readable (colour temperature when exposed).
                     Macs without a built-in screen skip.

The readings are evidence; only these plausibility limits decide a result. The
catalogue's boards say which Mac has a lid or a screen (camera).
"""

from __future__ import annotations

import math

from .session import Context
from .sleep import LID_ANGLE

MOTION, LID, LIGHT = "sensors.motion", "sensors.lid-angle", "sensors.light"
CHECK_IDS = (MOTION, LID, LIGHT)
NOT_READ = "not read in this run (it predates this check)"
GRAVITY = 9.8
GRAVITY_TOLERANCE = 0.15
STILL_RAD_S = 0.35  # about 20 degrees a second: a Mac on a lap still passes
FELL = 0.9  # the lowest reading is at most this much of the first, while the lid closed


def run(ctx: Context) -> list[dict]:
    found = ctx.host.hardware_snapshot("sensors")
    if not found.get("observed"):
        return [_result(check_id, "skip", [NOT_READ]) for check_id in CHECK_IDS]
    board = ctx.machine.board
    return [
        _motion(found, ctx.catalogue.hardware_fact(board, "lid")),
        _lid(found, ctx.catalogue.hardware_fact(board, "lid"), ctx.shared.get(LID_ANGLE)),
        _light(found, ctx.catalogue.hardware_fact(board, "camera")),
    ]


def _devices(found: dict, key: str) -> list[dict]:
    return [device for device in found["iio"] if key in device]


def _why_absent(found: dict, what: str) -> str:
    node = "its always-on processor (AOP) is in the device tree" if found.get("aop_node") else "there's no always-on processor (AOP) node in the device tree"
    return f"no {what} among this Mac's {len(found['iio'])} IIO devices; {node}"


def _unexpected(check_id: str, why: str, expected: bool | None, absent: str) -> dict:
    """Something missing: a failure only on a Mac the catalogue says has it."""
    if expected is False:
        return _result(check_id, "skip", [absent])
    if expected is None:
        return _result(check_id, "skip", [why, "the catalogue doesn't say whether this Mac has one"])
    return _result(check_id, "fail", [why])


def _axes(values: dict) -> str:
    return ", ".join(f"{axis} {value}" for axis, value in sorted(values.items()))


def _motion(found: dict, has_lid: bool | None) -> dict:
    accelerometers = _devices(found, "accel")
    if not accelerometers:
        return _unexpected(MOTION, _why_absent(found, "accelerometer"), has_lid, "a desktop Mac has no motion sensors")
    device = accelerometers[0]
    evidence = []
    if not device["accel"]:
        return _result(MOTION, "fail", [f"accelerometer {device['name']} is registered but its channels don't read (the always-on processor has sent no report)"])
    magnitude = math.sqrt(sum(value * value for value in device["accel"].values()))
    evidence.append(f"accelerometer {device['name']}: {_axes(device['accel'])} m/s², magnitude {magnitude:.2f}")
    status = "pass"
    if len(device["accel"]) < 3 or abs(magnitude - GRAVITY) > GRAVITY * GRAVITY_TOLERANCE:
        status = "fail"
        evidence.append(f"at rest it should read gravity: {GRAVITY} m/s² ±{GRAVITY_TOLERANCE:.0%} over three axes")
    gyroscopes = _devices(found, "gyro")
    if not gyroscopes:
        evidence.append(_why_absent(found, "gyroscope"))
    elif not gyroscopes[0]["gyro"] or gyroscopes[0]["gyro_unreadable"]:
        status = "fail"
        evidence.append(f"gyroscope {gyroscopes[0]['name']} is registered but {gyroscopes[0]['gyro_unreadable']} of its channels don't read")
    else:
        evidence.append(f"gyroscope {gyroscopes[0]['name']}: {_axes(gyroscopes[0]['gyro'])} rad/s")
        if any(abs(value) > STILL_RAD_S for value in gyroscopes[0]["gyro"].values()):
            status = "fail"
            evidence.append(f"at rest it should be near still: under {STILL_RAD_S} rad/s on each axis")
    return _result(MOTION, status, evidence)


def _lid(found: dict, has_lid: bool | None, closing: dict | None) -> dict:
    sensors = _devices(found, "angle")
    if not sensors:
        return _unexpected(LID, _why_absent(found, "lid-angle sensor"), has_lid, "this Mac has no lid")
    device = sensors[0]
    if device["angle"] is None:
        return _result(LID, "fail", [f"lid-angle sensor {device['name']} is registered but doesn't read"])
    evidence = [f"lid-angle sensor {device['name']}: {device['angle']}°"]
    if not 0 <= device["angle"] <= 360:
        return _result(LID, "fail", [*evidence, "a lid angle is between 0 and 360°"])
    if not closing:
        return _result(LID, "pass", [*evidence, "the lid wasn't closed in this run, so whether it follows the lid wasn't seen"])
    words = f"while the lid closed its raw reading went from {closing['before']} to a lowest {closing['lowest']}"
    if closing["before"] > 0 and closing["lowest"] <= closing["before"] * FELL:
        return _result(LID, "pass", [*evidence, f"it followed the lid: {words}"])
    return _result(LID, "fail", [*evidence, f"it didn't follow the lid: {words}"])


def _light(found: dict, has_screen: bool | None) -> dict:
    sensors = _devices(found, "lux")
    if not sensors:
        return _unexpected(LIGHT, _why_absent(found, "ambient light sensor"), has_screen, "this Mac has no built-in screen")
    device = sensors[0]
    if device["lux"] is None:
        return _result(LIGHT, "fail", [f"ambient light sensor {device['name']} is registered but doesn't read"])
    temperature = device.get("colour_temperature")
    colour = f"colour temperature {temperature} K" if temperature is not None else "colour temperature not exposed"
    return _result(LIGHT, "pass", [f"ambient light sensor {device['name']}: {device['lux']} lux, {colour}"])


def _result(check_id: str, status: str, evidence: list[str]) -> dict:
    return {"id": check_id, "kind": "automatic", "status": status, "evidence": evidence}
