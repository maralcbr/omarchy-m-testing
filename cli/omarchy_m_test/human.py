"""Human checks: the tool asks, the human answers yes, no or skip, with an optional note.

    result = human.check(ctx, "audio.speaker-tone", "Did you hear a short tone from both speakers?",
                         evidence=["played a 1 kHz tone at 30% volume"])

The result is a schema-v1 check of kind "human": yes passes, no fails, skip
(or no answer at all: end of input, three answers that aren't y/n/s) is
skipped, never a failure. A bare Enter is yes, the default, and passes too,
but nobody typed it: the result says answered_by_default, and the site shows
it as unconfirmed and never lets it colour the matrix. Its evidence holds what was asked, the
answer and the note, which passes the privacy scrubber like any evidence
before it reaches the checkpoint or the report. The check id must be in the
catalogue's checks, and in its section's human_checks so a skipped section
reports it as human too.

An unattended run (--unattended) asks nothing: every human check is skipped,
with that reason, and the checks that act before they ask (a tone, brightness
steps, the camera window) don't act.

Ctrl-C at the question interrupts the run like anywhere else: the section's
restorers run and the run can resume.
"""

from __future__ import annotations

from typing import Sequence

from . import presence
from .session import Context
from .ui import Ui

STATUSES = {"yes": "pass", "no": "fail", "skip": "skip"}
NO_ANSWER = "answer: none (counted as skipped)"
DEFAULT_ANSWER = "answer: yes, by pressing Enter (the default; unconfirmed)"
# On a result whose yes came from a bare Enter (schema: checks[].answered_by_default).
DEFAULTED = "answered_by_default"
NOT_BACKED = "note: answer not backed by readings"


def check(ctx: Context, check_id: str, question: str, evidence: Sequence[str] = ()) -> dict:
    """Ask the human `question` and return their answer as a human check result."""
    if unattended(ctx):
        return skip(check_id, presence.UNATTENDED, evidence)
    ui = ctx.ui or Ui(ctx.host)
    answer, note, defaulted = ui.human(question)
    lines = [*evidence, f"asked: {question}"]
    lines.append((DEFAULT_ANSWER if defaulted else f"answer: {answer}") if answer else NO_ANSWER)
    if note:
        lines.append(f"note: {note}")
    result = {"id": check_id, "kind": "human", "status": STATUSES.get(answer or "skip", "skip"), "evidence": lines}
    if defaulted:
        result[DEFAULTED] = True
    return result


def unattended(ctx: Context) -> bool:
    """Nobody is at the Mac (--unattended): ask nothing, and do nothing only a person would see or hear."""
    return bool(ctx.cache.get(presence.UNATTENDED_KEY))


def absent(ctx: Context, check_id: str) -> bool:
    """The catalogue says this Mac doesn't have what the check tests (no notch, no headphone jack): don't ask."""
    feature = ctx.catalogue.feature_for_check(check_id)
    if feature is None:
        return False
    states = ctx.catalogue.expected(feature, ctx.catalogue.chip_for_soc(ctx.machine.soc), ctx.machine.board)
    return any(state.get("status") == "absent" for state in (states or {}).values())


def skip(check_id: str, reason: str, evidence: Sequence[str] = ()) -> dict:
    """A human check that wasn't asked (its setup failed, the human declined a package): skipped, with why."""
    return {"id": check_id, "kind": "human", "status": "skip", "evidence": [*evidence, f"skipped: {reason}"]}


def check_each(ctx: Context, check_id: str, parts: Sequence[tuple[str, str | None, str]], evidence: Sequence[str] = ()) -> dict:
    """One human check asked as several questions, one per part: (label, question, why it isn't asked).

    A part with no question is recorded as skipped with its reason (never a
    failure). Any no fails the check; otherwise any yes passes it; otherwise
    it's skipped. A pass with any yes from a bare Enter is answered_by_default.
    """
    if unattended(ctx):
        return skip(check_id, presence.UNATTENDED, evidence)
    ui = ctx.ui or Ui(ctx.host)
    lines, answers, defaulted = list(evidence), [], False
    for label, question, why in parts:
        if question is None:
            lines.append(f"{label}: skipped: {why}")
            continue
        answer, note, by_default = ui.human(question)
        answers.append(answer)
        defaulted = defaulted or (answer == "yes" and by_default)
        said = ("yes, by pressing Enter (the default; unconfirmed)" if by_default else answer) if answer else "none (counted as skipped)"
        lines.append(f"{label}: asked: {question}")
        lines.append(f"{label}: answer: {said}" + (f"; note: {note}" if note else ""))
    status = "fail" if "no" in answers else "pass" if "yes" in answers else "skip"
    result = {"id": check_id, "kind": "human", "status": status, "evidence": lines}
    if status == "pass" and defaulted:
        result[DEFAULTED] = True
    return result


def not_backed(result: dict, why: str) -> dict:
    """Note that the human's answer disagrees with what the Mac read; the status stays the human's."""
    result["evidence"].append(f"{NOT_BACKED} ({why})")
    return result
