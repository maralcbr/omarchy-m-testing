"""The schema-v1 report (schema/report-v1.schema.json)."""

from __future__ import annotations

import json

from . import SCHEMA_VERSION, TOOL_NAME, TOOL_VERSION
from .catalogue import Catalogue
from .consent import CONSENT_VERSION
from .machine import Machine


def build(machine: Machine, system: dict, checks: list[dict], catalogue: Catalogue, inventory: dict | None = None,
          unattended: bool = False) -> dict:
    """The report, with every check result explained against the catalogue.

    `inventory` is the hardware inventory's block (inventory.py); a run that
    skipped the Hardware section has none. An unattended run (nobody at the
    Mac, so no human checks) says so with "unattended": true.
    """
    classified = [{**check, "classification": catalogue.classify(check, machine.soc, machine.board)} for check in checks]
    built = {
        "schema_version": SCHEMA_VERSION,
        "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
        "consent_version": CONSENT_VERSION,
        "catalogue_version": catalogue.version,
        "machine": {
            "model": machine.model,
            "board": machine.board,
            "soc": machine.soc,
            "chip": machine.chip,
            "arch": machine.arch,
            "kernel": machine.kernel,
        },
        "system": system,
        **({"unattended": True} if unattended else {}),
        "checks": classified,
    }
    if inventory:
        built = {**{k: v for k, v in built.items() if k != "checks"}, "inventory": inventory, "checks": classified}
    return built


def to_text(report: dict) -> str:
    """The exact text that is written, shown and uploaded."""
    return json.dumps(report, indent=2, ensure_ascii=False) + "\n"
