"""The whole CLI run: refuse non-Apple machines, disclaimer, sections, report, upload.

Sections run one after another (session.py): each is checkpointed when it
finishes, and every change it makes is restored when it ends, on an error or
on Ctrl-C. Disruptive sections are skipped over SSH or without a local seat
(presence.py). Everything runs through a Guarded host (safety.py): the tool
never reboots, and never touches disk encryption or boot files.
"""

from __future__ import annotations

import argparse
import json
from typing import Sequence

from . import TOOL_NAME, TOOL_VERSION, presence, privacy, report, signing, system, tester, updates
from .catalogue import CatalogueError
from .consent import ACCEPT_PROMPT, DISCLAIMER, accepted
from .explain import explain, line, load_catalogue
from .host import Bounded, Host, NetworkError
from .machine import NotAppleSilicon, identify
from .recording import RecordingHost
from .safety import Guarded
from .sections import APPLE
from .session import PROGRESS, Changes, Checkpoint, Context, Section, State, run_key, skipped
from .ui import Ui, progress_of

DEFAULT_SITE = "https://omarchy-m-testing.org"
DEFAULT_OUTPUT = "omarchy-m-test-report.json"
UPLOAD_PATH = "/api/v1/reports"

EXIT_OK = 0
EXIT_CANCELLED = 1
EXIT_REFUSED = 2
EXIT_UPLOAD_FAILED = 3
EXIT_BAD_INPUT = 4
EXIT_INTERRUPTED = 130


class _Exit(Exception):
    def __init__(self, status: int):
        self.status = status


class _Parser(argparse.ArgumentParser):
    """argparse, but its help and errors go through the host."""

    def __init__(self, host: Host, **kwargs):
        self._host = host
        super().__init__(**kwargs)

    def _print_message(self, message, file=None):
        if message:
            self._host.show(message.rstrip("\n"))

    def exit(self, status=0, message=None):
        if message:
            self._host.show(message.rstrip("\n"))
        raise _Exit(status)


def _parse(argv: Sequence[str], host: Host) -> argparse.Namespace:
    parser = _Parser(host, prog=TOOL_NAME, description="Test this Mac's hardware under Omarchy.")
    parser.add_argument("--dry-run", action="store_true", help="run and write the report, but never upload it")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help=f"where to write the report (default: {DEFAULT_OUTPUT})")
    parser.add_argument("--site", default=DEFAULT_SITE, help=f"site to upload to (default: {DEFAULT_SITE})")
    parser.add_argument("--catalogue", metavar="FILE", help="use this feature catalogue instead of the bundled one (for trying a catalogue change)")
    parser.add_argument("--explain", metavar="REPORT", help="explain a saved report's results against the catalogue; runs no checks")
    parser.add_argument("--record", metavar="FILE", help="also save what this Mac answered, scrubbed, as a test recording")
    parser.add_argument("--unattended", action="store_true", help="nobody is at the Mac: run the automatic checks only, ask nothing about the hardware and skip anything disruptive; the report says so")
    parser.add_argument("--skip", metavar="SECTION", action="append", default=[], help="skip a section (repeat, or separate with commas); the run lists them")
    parser.add_argument("--sign-in", action="store_true", help="testers: sign this Mac in with GitHub, once; its runs then count as tester runs. Runs no checks")
    parser.add_argument("--sign-out", action="store_true", help="testers: unlink this Mac from its GitHub handle; its later runs are community runs. Runs no checks")
    parser.add_argument("--status", action="store_true", help="show whether this Mac is signed in as a tester, and as whom. Runs no checks")
    parser.add_argument("--version", action="version", version=f"{TOOL_NAME} {TOOL_VERSION}")
    gpu=parser.add_mutually_exclusive_group()
    gpu.add_argument("--gpu-benchmark",action="store_true",help="GPU-only versioned experiment; saves a separate local signed document and never uploads")
    gpu.add_argument("--gpu-compare",nargs=2,metavar=("LEFT","RIGHT"),help="compare two saved GPU experiment documents without running checks")
    parser.add_argument("--benchmark-repetitions",type=int,default=7,help="GPU measured repetitions,5–31 (default7), after two discarded warmups")
    parser.add_argument("--benchmark-block",type=int,default=1,help="anonymous operator-declared independent boot-block number,1–1024; this tool never reboots")
    parser.add_argument("--benchmark-seed",type=int,default=0,help="reproducible interleaved workload-order seed")
    parser.add_argument("--accept-gpu-risk",action="store_true",help="explicitly accept GPU benchmark load without an interactive prompt; use only with saved work and recovery available")
    return parser.parse_args(list(argv))


def main(argv: Sequence[str], host: Host, sections: Sequence[Section] = APPLE) -> int:
    """Run the CLI. `sections` is the platform's list (Apple Silicon's by default)."""
    try:
        args = _parse(argv, host)
    except _Exit as done:
        return done.status

    if args.gpu_benchmark or args.gpu_compare:
        from . import gpu_benchmark
        # Dedicated mode cannot fall through to hardware/disruptive sections,
        # online update notices, tester sign-in or the upload path.
        return gpu_benchmark.cli(args,Guarded(host))

    if args.sign_in:
        return tester.sign_in(Guarded(host), args.site.rstrip("/"))
    if args.sign_out:
        return tester.sign_out(Guarded(host), args.site.rstrip("/"))
    if args.status:
        return tester.status(Guarded(host), args.site.rstrip("/"))

    if not args.record:
        return _interruptible(args, Guarded(host), sections)

    # Record mode: nothing more runs after the disclaimer is declined, and a
    # refused machine's recording holds only what identifying it read.
    recorder = RecordingHost(host)
    status = _interruptible(args, Guarded(recorder), sections)
    if status == EXIT_INTERRUPTED:
        return status
    if status == EXIT_CANCELLED:
        return status
    if status != EXIT_REFUSED:
        recorder.capture_sources()
    recorder.save(args.record, learn=status != EXIT_REFUSED)
    host.show(f"Recording saved to {args.record} (scrubbed).")
    return status


def _interruptible(args: argparse.Namespace, host: Host, sections: Sequence[Section]) -> int:
    args.resumable = False
    try:
        return _run(args, host, sections)
    except KeyboardInterrupt:
        host.show(
            f"\nInterrupted. Everything {TOOL_NAME} changed was put back and nothing was uploaded."
            + (f"\nRun {TOOL_NAME} again to resume where it stopped." if args.resumable else "")
        )
        return EXIT_INTERRUPTED


def _skips(requested: list[str], sections: Sequence[Section]) -> set[str] | None:
    wanted = {name.strip() for value in requested for name in value.split(",") if name.strip()}
    known = {section.id for section in sections}
    return wanted if wanted <= known else None


def _run(args: argparse.Namespace, host: Host, sections: Sequence[Section]) -> int:
    try:
        catalogue = load_catalogue(host, args.catalogue)
    except CatalogueError as problem:
        host.show(f"The feature catalogue {args.catalogue or '(bundled)'} can't be used: {problem}. Nothing was run.")
        return EXIT_BAD_INPUT

    if args.explain:
        return EXIT_OK if explain(host, args.explain, catalogue) else EXIT_BAD_INPUT

    skip = _skips(args.skip, sections)
    if skip is None:
        host.show(f"--skip takes section names: {', '.join(section.id for section in sections)}. Nothing was run.")
        return EXIT_BAD_INPUT

    updates.notify(host)

    try:
        machine = identify(host)
    except NotAppleSilicon as reason:
        host.show(
            f"{TOOL_NAME} only runs on Apple Silicon Macs running Linux, "
            f"and this machine isn't one ({reason}). Nothing was run."
        )
        return EXIT_REFUSED

    # Record mode neither resumes nor checkpoints: a recording must hold every read of a whole run.
    checkpoint = Checkpoint.for_host(host, enabled=not args.record)
    saved = checkpoint.load()
    changes = Changes(host)
    if saved and saved.restorers:
        # A run was killed before it could put things back: do that before anything else.
        host.show(f"An earlier {TOOL_NAME} run was stopped before it could undo its changes. Putting them back:")
        for restorer in saved.restorers:
            host.show(f"  - {restorer.description}")
        changes.pending = list(saved.restorers)
        changes.restore()
        saved.restorers = changes.pending
        _report_failed_restores(host, changes)
        checkpoint.save(saved)

    ui = Ui.for_host(host)
    ui.intro()
    ui.text(DISCLAIMER)
    try:
        answer = ui.ask(ACCEPT_PROMPT)
    except EOFError:
        answer = None
    if answer is None or not accepted(answer):
        if not changes.pending:
            checkpoint.clear()  # declining ends any earlier run too
        host.show("Cancelled. Nothing was run.")
        return EXIT_CANCELLED

    if not (args.dry_run or args.record or args.unattended):
        tester.offer(host, ui.text, ui.confirm, args.site.rstrip("/"))

    # The Scrubber learns this Mac's hostname, accounts and networks: nothing
    # a check found reaches the checkpoint or the report unscrubbed.
    scrubber = privacy.Scrubber.for_host(host)
    key = run_key(machine, catalogue, TOOL_VERSION)
    cache: dict = {presence.UNATTENDED_KEY: True} if args.unattended else {}
    blocked = _blocked(host, sections, cache)
    state = _resume(ui, saved, key, sections, skip | set(blocked))
    if state is None:
        state = State(_choose(ui, sections, skip, blocked), key=key)
    # Serials and device names an interrupted run's checks named, to remove from this run's too.
    scrubber.remember(serials=state.serials, names=state.names)
    # Anything an earlier run left that couldn't be put back yet stays registered.
    state.restorers[:0] = changes.pending
    changes.pending = state.restorers
    changes.persist = lambda: checkpoint.save(state)
    if args.unattended:
        # Kept with the run, so a report any part of which ran unattended says so.
        state.shared[presence.UNATTENDED_KEY] = True
    checkpoint.save(state)
    args.resumable = checkpoint.path is not None

    found = system.detect(host)
    ui.text(f"Checking {machine.model} ({found.describe()})...")
    if found.build():
        ui.text(f"Build: {found.build()}.")
    ui.text("Checks that need root use passwordless sudo when it's set up, and are skipped otherwise.")
    if args.unattended:
        ui.text("Unattended: only the automatic checks run; nothing asks you to look, listen or plug anything in.")
    shared = state.shared
    running = [section for section in sections if section.id in state.selected and section.id not in blocked]
    try:
        for number, section in enumerate(running, 1):
            if section.id in state.done:
                continue
            section_host = ui.section(section.title, section.description, progress_of(running, number, set(state.done)))
            try:
                results = section.run(Context(Bounded(section_host), machine, catalogue, changes, found, shared, cache, ui))
            finally:
                ui.end_section()
                changes.restore()
                _report_failed_restores(host, changes)
            state.done[section.id] = [privacy.scrub_check(result, scrubber) for result in results]
            state.serials, state.names = scrubber.serials, scrubber.names
            checkpoint.save(state)
            for result in state.done[section.id]:
                classification = catalogue.classify(result, machine.soc, machine.board)
                ui.result(line(result, classification, catalogue), classification["outcome"])
    finally:
        ui.close()
        changes.restore()
        _report_failed_restores(host, changes)

    results = []
    for section in sections:
        if section.id in state.done:
            results += state.done[section.id]
        else:
            results += skipped(section, blocked.get(section.id))
    built = report.build(machine, found.report(shared.get("boot_loader", "unknown")), results, catalogue, shared.get("inventory"),
                         unattended=bool(shared.get(presence.UNATTENDED_KEY)))

    signed, unsigned_because = signing.sign(host, privacy.enforce(built, scrubber))
    text = report.to_text(signed)
    host.write_file(args.output, text)
    checkpoint.clear()
    args.resumable = False
    build = found.build()
    host.show(f"\nBuild tested: {build}; {TOOL_NAME} {TOOL_VERSION}." if build else f"\nTested with {TOOL_NAME} {TOOL_VERSION}.")
    host.show(f"\nReport written to {args.output}. This is exactly what would be uploaded:\n")
    host.show(text)
    if unsigned_because:
        host.show(
            f"This report isn't signed with this Mac's key: {unsigned_because}. "
            "The site only accepts signed reports."
        )

    if args.dry_run:
        host.show("Dry run: the report was not uploaded.")
        return EXIT_OK

    site = args.site.rstrip("/")
    if not ui.confirm(f"Upload this report to {site}?"):
        host.show(f"Not uploaded. The report is saved at {args.output}.")
        return EXIT_OK

    return _upload(host, site, text)


def _report_failed_restores(host: Host, changes: Changes) -> None:
    for restorer, reason in changes.take_failures():
        host.show(
            f"Couldn't undo: {restorer.description} ({reason or 'it failed'}). "
            f"To put it back yourself, run: {' '.join(restorer.command)}"
        )


def _resume(ui: Ui, saved: State | None, key: dict, sections: Sequence[Section], skip: set[str]) -> State | None:
    """The saved state to carry on from, if there is one for this run and the human wants it."""
    if saved is None or saved.key != key or not (saved.done or saved.shared.get(PROGRESS)):
        return None
    known = {section.id for section in sections}
    if not set(saved.selected) <= known or not set(saved.done) <= known:
        return None
    left = [section.title for section in sections if section.id in saved.selected and section.id not in saved.done]
    if not left:
        return None
    ui.text(f"An earlier run stopped with {len(saved.done)} section(s) done; left to run: {', '.join(left)}.")
    if ui.confirm("Resume where it stopped?", on_eof=True):
        selected = [s for s in saved.selected if s in saved.done or s not in skip]
        return State(selected, dict(saved.done), [], key, dict(saved.shared), list(saved.serials), list(saved.names))
    return None


def _blocked(host: Host, sections: Sequence[Section], cache: dict) -> dict[str, str]:
    """The disruptive sections this run can't do, and why (over SSH, no local seat)."""
    if not any(section.disruptive for section in sections):
        return {}
    reason = presence.of(host, cache).blocks()
    return {section.id: reason for section in sections if section.disruptive and reason}


def _choose(ui: Ui, sections: Sequence[Section], skip: set[str], blocked: dict[str, str]) -> list[str]:
    """List the sections up front and let the human skip any (at a terminal; --skip everywhere)."""
    ui.text("This run has these sections" + (" (skip any with --skip NAME):" if not ui.styled else ":"))
    for number, section in enumerate(sections, 1):
        mark = "skip" if section.id in skip or section.id in blocked else "    "
        why = f" Skipped: {blocked[section.id]}." if section.id in blocked else ""
        ui.text(f"  {number:2}. {mark} {section.title} ({section.id}): {section.description}{why}")
    offered = [section for section in sections if section.id not in blocked]
    kept = ui.choose(
        "Sections to run (space toggles, Enter starts)",
        [section.title for section in offered],
        [section.title for section in offered if section.id not in skip],
    )
    return [section.id for section in offered if section.title in kept]


def _upload(host: Host, site: str, text: str) -> int:
    try:
        response = host.post_json(site + UPLOAD_PATH, text)
    except NetworkError as error:
        host.show(f"Upload failed: couldn't reach {site} ({error}). The report is saved locally.")
        return EXIT_UPLOAD_FAILED

    try:
        body = json.loads(response.body)
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}

    if response.status == 201 and body.get("report_url") and body.get("deletion_url"):
        host.show(f"Uploaded. Your report: {body['report_url']}")
        host.show(f"Keep this link to delete it later: {body['deletion_url']}")
        if body.get("tester") is True:
            host.show("It counts as a tester run.")
        return EXIT_OK

    message = body.get("error") or f"the site answered HTTP {response.status}"
    host.show(f"Upload failed: {message}")
    details = body.get("details")
    for detail in details[:10] if isinstance(details, list) else []:
        host.show(f"  - {detail}")
    return EXIT_UPLOAD_FAILED
