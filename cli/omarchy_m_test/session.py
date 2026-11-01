"""A run's sections, the restorer registry and the checkpoint.

Sections: a run is a list of sections (sections.py), listed up front. The
human can skip any of them (the picker at a terminal, --skip anywhere); a
skipped section's checks are reported as skipped, never as failures.

Restorers: a section that changes the machine does it through
Changes.make(description, change, restore), which registers the command that
undoes it *before* making the change. Restorers run, newest first, when the
section ends, on an error and on an interrupt. They are kept in the
checkpoint too, so a run killed outright (power loss, a closed terminal
before the signal handler ran) has them run by the next run, before anything
else. A restorer that needs root (sudo=True: removing temporary packages)
runs `sudo -n`; if sudo's cached credentials ran out and the human is at a
terminal, `sudo -v` asks for the password there first. A restorer that
removes packages lists them (packages=...): only those pacman still has
installed are removed, so a run killed before or during the install doesn't
leave a removal that fails on the ones that never arrived. changes.py and
packages.py build the restorers for volume, Wi-Fi and temporary packages.

Disruptive sections (disruptive=True: they could cut the connection or the
session, like dropping Wi-Fi or sleeping) are skipped, with the reason, over
SSH or without a local seat (presence.py).

Checkpoint: after each section, the run's state (the sections chosen, the
results of the finished ones, pending restorers) is written to
$XDG_STATE_HOME/omarchy-m-test/checkpoint.json (~/.local/state/...). A run that
finds one for the same tool, catalogue, Mac and kernel offers to resume: the
finished sections aren't run again, the interrupted one starts over (from the
step it checkpointed, if it keeps its progress: Context.progress). It also
keeps the serial values and device names the scrubber learned from the finished sections, so the
resumed run removes them from its own evidence too. The checkpoint is removed
once the report is written.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Sequence

from .catalogue import Catalogue
from .host import CommandResult, Host
from .machine import Machine

if TYPE_CHECKING:
    from .system import System
    from .ui import Ui

CHECKPOINT_VERSION = 1
CHECKPOINT_NAME = "omarchy-m-test/checkpoint.json"
PROGRESS = "progress"  # in the shared state: each section's own progress (Context.progress)
SKIPPED_EVIDENCE = "skipped: this section wasn't run"


@dataclass(frozen=True)
class Restorer:
    description: str
    argv: tuple[str, ...]
    sudo: bool = False  # argv starts with `sudo -n`; the password may be asked for first
    packages: tuple[str, ...] = ()  # appended to argv, those still installed only

    @property
    def command(self) -> tuple[str, ...]:
        return self.argv + self.packages

SUDO_CACHED = ["sudo", "-n", "true"]
SUDO_ASK = ["sudo", "-v"]


class Changes:
    """The restorer registry."""

    def __init__(self, host: Host, persist: Callable[[], None] = lambda: None):
        self.host = host
        self.persist = persist
        self.pending: list[Restorer] = []
        self.failed: list[tuple[Restorer, str]] = []
        self.may_ask_password = True  # not on an unattended run: nobody is there, so a restore sudo refuses is reported

    def make(self, description: str, change: Sequence[str], restore: Sequence[str], host: Host | None = None) -> CommandResult:
        """Register how to undo a change, then make it."""
        self.register(description, restore)
        return (host or self.host).run(list(change))

    def register(self, description: str, restore: Sequence[str], sudo: bool = False, packages: Sequence[str] = ()) -> Restorer:
        restorer = Restorer(description, tuple(restore), sudo, tuple(packages))
        self.pending.append(restorer)
        self.persist()
        return restorer

    def replace(self, old: Restorer, new: Restorer | None) -> None:
        """Swap a pending restorer for a more exact one (or drop it: nothing to undo after all)."""
        for index, restorer in enumerate(self.pending):
            if restorer is old:
                if new is None:
                    del self.pending[index]
                else:
                    self.pending[index] = new
                self.persist()
                return

    def restore(self) -> None:
        """Undo every pending change, newest first.

        A restorer that fails is reported and dropped (retrying it forever would
        help nobody). If the host itself gives out, the rest stay pending in the
        checkpoint for the next run.
        """
        asked = False
        while self.pending:
            restorer = self.pending[-1]
            try:
                argv = self._command(restorer)
                if argv and restorer.sudo and not asked:
                    asked = True
                    self._authorise()
                result = self.host.run(argv) if argv else CommandResult(0, "", "")
            except Exception:
                return
            self.pending.pop()
            if result.returncode != 0:
                self.failed.append((restorer, (result.stderr or result.stdout).strip()))
            try:
                self.persist()
            except Exception:
                pass  # a checkpoint that can't be written mustn't stop the rest being put back

    def _command(self, restorer: Restorer) -> list[str]:
        """What to run: argv, plus those of its packages still installed; nothing when none are."""
        if not restorer.packages:
            return list(restorer.argv)
        listed = self.host.run(["pacman", "-Qq", *restorer.packages]).stdout.split()
        present = [name for name in restorer.packages if name in listed]
        return [*restorer.argv, *present] if present else []

    def _authorise(self) -> None:
        """Make sure `sudo -n` works: ask for the password at the terminal if sudo forgot it."""
        if self.may_ask_password and self.host.run(SUDO_CACHED).returncode != 0 and self.host.terminal() is not None:
            self.host.run_tty(SUDO_ASK)

    def take_failures(self) -> list[tuple[Restorer, str]]:
        failed, self.failed = self.failed, []
        return failed


@dataclass(frozen=True)
class Context:
    """What a section's checks work with."""

    host: Host
    machine: Machine
    catalogue: Catalogue
    changes: Changes
    system: "System | None" = None
    # What sections share within a run and across a resume (JSON values only),
    # e.g. the boot loader mac-check reported for the report's system block.
    shared: dict[str, Any] = field(default_factory=dict)
    # What sections share within this process only (never checkpointed), e.g.
    # a script's results that feed two sections.
    cache: dict[str, Any] = field(default_factory=dict)
    # How the section asks the human (human.py, packages.py); plain text when None.
    ui: "Ui | None" = None

    def progress(self, section_id: str) -> dict[str, Any]:
        """Where a section keeps how far it got (JSON values only), checkpointed with ctx.changes.persist().

        A section that asks the human to do something long or disruptive (closing
        the lid) keeps its finished steps here, so a resumed run carries on from the
        step it stopped in instead of starting the section over.
        """
        every = self.shared.get(PROGRESS)
        if not isinstance(every, dict):
            every = self.shared[PROGRESS] = {}
        if not isinstance(every.get(section_id), dict):
            every[section_id] = {}
        return every[section_id]

    def change(self, description: str, change: Sequence[str], restore: Sequence[str]) -> CommandResult:
        """Change the machine: `restore` is registered first, and runs when the section ends."""
        return self.changes.make(description, change, restore, self.host)


@dataclass(frozen=True)
class Section:
    id: str
    title: str                 # the section's heading: short, letters and spaces
    description: str
    check_ids: tuple[str, ...]  # reported as skipped when the section is
    run: Callable[[Context], list[dict]]
    human_checks: tuple[str, ...] = ()  # which of check_ids ask the human (kind "human")
    disruptive: bool = False  # could cut the connection or session: never over SSH or without a local seat


def skipped(section: Section, reason: str | None = None) -> list[dict]:
    evidence = f"skipped: {reason}" if reason else SKIPPED_EVIDENCE
    return [
        {"id": check_id, "kind": "human" if check_id in section.human_checks else "automatic", "status": "skip", "evidence": [evidence]}
        for check_id in section.check_ids
    ]


@dataclass
class State:
    """What the checkpoint holds."""

    selected: list[str]
    done: dict[str, list[dict]] = field(default_factory=dict)
    restorers: list[Restorer] = field(default_factory=list)
    key: dict[str, Any] = field(default_factory=dict)
    shared: dict[str, Any] = field(default_factory=dict)
    # Serial values the scrubber learned from finished sections (privacy.Scrubber.learn),
    # so a resumed run still removes them from the sections it runs; the file is only the user's.
    serials: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)  # device names likewise

    def to_text(self) -> str:
        return json.dumps({
            "checkpoint_version": CHECKPOINT_VERSION,
            "key": self.key,
            "selected": self.selected,
            "done": self.done,
            "shared": self.shared,
            **({"serials": self.serials} if self.serials else {}),
            **({"names": self.names} if self.names else {}),
            "restorers": [
                {
                    "description": r.description, "argv": list(r.argv),
                    **({"sudo": True} if r.sudo else {}), **({"packages": list(r.packages)} if r.packages else {}),
                }
                for r in self.restorers
            ],
        }, indent=2, ensure_ascii=False) + "\n"

    @classmethod
    def from_text(cls, text: str | bytes) -> "State | None":
        try:
            data = json.loads(text)
            if data.get("checkpoint_version") != CHECKPOINT_VERSION:
                return None
            restorers = [
                Restorer(
                    str(r["description"]), tuple(str(a) for a in r["argv"]), r.get("sudo") is True,
                    tuple(str(p) for p in r.get("packages", [])),
                )
                for r in data.get("restorers", [])
            ]
            done = {str(k): list(v) for k, v in data.get("done", {}).items()}
            return cls([str(s) for s in data.get("selected", [])], done, restorers, dict(data.get("key", {})), dict(data.get("shared", {})),
                       [str(s) for s in data.get("serials", [])], [str(n) for n in data.get("names", [])])
        except (ValueError, TypeError, KeyError, AttributeError):
            return None


def run_key(machine: Machine, catalogue: Catalogue, version: str) -> dict[str, Any]:
    """A checkpoint is only resumed by the same tool and catalogue on the same Mac and kernel."""
    return {"tool_version": version, "catalogue_version": catalogue.version, "board": machine.board, "kernel": machine.kernel}


def state_dir(host: Host) -> str | None:
    """$XDG_STATE_HOME (~/.local/state), where the checkpoint and the machine key live; None without a home."""
    base = host.env("XDG_STATE_HOME")
    if not base:
        home = host.env("HOME")
        base = f"{home}/.local/state" if home else None
    return base


class Checkpoint:
    def __init__(self, host: Host, path: str | None):
        self.host = host
        self.path = path

    @classmethod
    def for_host(cls, host: Host, enabled: bool = True) -> "Checkpoint":
        if not enabled:
            return cls(host, None)
        base = state_dir(host)
        return cls(host, f"{base}/{CHECKPOINT_NAME}" if base else None)

    def load(self) -> State | None:
        if not self.path:
            return None
        try:
            text = self.host.read_file(self.path)
        except OSError:
            return None
        return State.from_text(text)

    def save(self, state: State) -> None:
        if self.path:
            self.host.write_file(self.path, state.to_text(), private=True)

    def clear(self) -> None:
        if self.path:
            self.host.remove_file(self.path)
