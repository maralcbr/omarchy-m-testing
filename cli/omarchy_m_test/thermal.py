"""The Thermal section: can Linux see the Mac's temperatures and fans? Read only.

  thermal.sensors  hwmon temperature channels, thermal zones with their trip
                   points and governors, and cooling devices. Fails only when
                   no temperature can be read at all.
  thermal.fans     each fan's speed, target and limits. A Mac the catalogue says
                   has no fans skips; one that has them and shows none fails.

Readings are evidence and never decide a result: a hot Mac still passes. Nothing
here writes to a hwmon, thermal or fan attribute.
"""

from __future__ import annotations

from .session import Context

SENSORS, FANS = "thermal.sensors", "thermal.fans"
CHECK_IDS = (SENSORS, FANS)
NOT_READ = "not read in this run (it predates this check)"
TEMPERATURE_LINES = 20
ZONE_LINES = 12
COOLING_LINES = 10


def run(ctx: Context) -> list[dict]:
    found = ctx.host.hardware_snapshot("thermal")
    if not found.get("observed"):
        return [_result(SENSORS, "skip", [NOT_READ]), _result(FANS, "skip", [NOT_READ])]
    return [_sensors(found), _fans(found, ctx.catalogue.hardware_fact(ctx.machine.board, "fans"))]


def _sensors(found: dict) -> dict:
    temps = sorted(((temp["c"], temp["label"], device["name"]) for device in found["hwmon"] for temp in device["temps"]), key=lambda t: -t[0])
    zones = found["zones"]
    readable = [zone for zone in zones if zone["c"] is not None]
    if not temps and not readable:
        devices = ", ".join(sorted({device["name"] for device in found["hwmon"]})) or "none"
        return _result(SENSORS, "fail", [f"no temperature can be read: no readable hwmon temperature channel (hwmon devices: {devices}), no readable thermal zone"])
    evidence = [f"{len(temps)} hwmon temperature channels on {len([d for d in found['hwmon'] if d['temps']])} devices, {len(readable)} readable thermal zones"]
    unreadable = sum(device["unreadable"] for device in found["hwmon"])
    if unreadable:
        evidence.append(f"{unreadable} hwmon temperature channels don't read")
    if temps:
        evidence.append(f"hottest: {temps[0][1]} ({temps[0][2]}) {temps[0][0]} °C")
    evidence += [f"{name} ({device}): {c} °C" for c, name, device in temps[:TEMPERATURE_LINES]]
    if len(temps) > TEMPERATURE_LINES:
        evidence.append(f"[{len(temps) - TEMPERATURE_LINES} more temperatures]")
    for zone in zones[:ZONE_LINES]:
        trips = ", ".join(f"{trip['type']} {trip['c']} °C" for trip in zone["trips"]) or "no trip points"
        reading = "unreadable" if zone["c"] is None else f"{zone['c']} °C"
        evidence.append(f"zone {zone['type']}: {reading}, governor {zone['policy'] or 'unknown'}, {trips}")
    evidence += [f"cooling {device['type']}: state {device['state']} of {device['max']}" for device in found["cooling"][:COOLING_LINES]]
    return _result(SENSORS, "pass", evidence)


def _fans(found: dict, has_fans: bool | None) -> dict:
    fans = [fan for device in found["hwmon"] for fan in device["fans"]]
    if has_fans is False and not fans:
        return _result(FANS, "skip", ["this Mac has no fans"])
    readable = [fan for fan in fans if fan["rpm"] is not None]
    if readable:
        return _result(FANS, "pass", [f"{len(readable)} fans read"] + [_fan_line(fan) for fan in fans])
    devices = ", ".join(sorted({device["name"] for device in found["hwmon"]})) or "none"
    why = f"no readable fan channel on any hwmon device (hwmon devices: {devices})"
    if has_fans is None:
        return _result(FANS, "skip", [why, "the catalogue doesn't say whether this Mac has fans"])
    return _result(FANS, "fail", [why, "this Mac has fans"])


def _fan_line(fan: dict) -> str:
    def shown(value: int | None) -> str:
        return "unknown" if value is None else str(value)

    return f"{fan['label']}: {shown(fan['rpm'])} rpm, target {shown(fan['target'])}, limits {shown(fan['min'])} to {shown(fan['max'])}"


def _result(check_id: str, status: str, evidence: list[str]) -> dict:
    return {"id": check_id, "kind": "automatic", "status": status, "evidence": evidence}
