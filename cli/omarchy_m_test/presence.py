"""Whether the human is at this Mac: an SSH session, and a local seat.

Disruptive sections (Section.disruptive: dropping Wi-Fi, sleeping) could cut
the connection or the session the run is shown in, so they are skipped,
with the reason, when:

  - the run is over SSH: SSH_CONNECTION, SSH_CLIENT or SSH_TTY is set, or
    logind says the session is remote; or
  - there is no local seat: logind doesn't place the session on a seat, or
    the session isn't the active one there, or logind can't be asked.

logind is asked about the run's own session:
`loginctl show-session ID --property=Remote --property=Seat --property=Active`
with ID XDG_SESSION_ID, else (or if logind no longer knows it) "auto": the
caller's session, or, for a process outside any session such as a terminal
Omarchy's uwsm started as a systemd user unit, the user's graphical session.
The helpers that make disruptive changes (changes.py) check this again
themselves, so a section that forgot to say it's disruptive still can't cut
an SSH connection.

An unattended run (--unattended) is nobody at the Mac: of() detects nothing,
and every disruptive section, plug-in prompt and human question is skipped
with that reason. The on-screen video check (video.py) needs a screen, not a
person, so it calls detect() itself.

of() detects once per run, only when something needs it, and keeps it in the
run's cache.
"""

from __future__ import annotations

from dataclasses import dataclass

from .host import Host

SSH_VARIABLES = ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")
CACHE_KEY = "presence"
# Set in the run's cache by --unattended.
UNATTENDED_KEY = "unattended"
UNATTENDED = "an unattended run (--unattended): nobody is at the Mac"


def session_query(session: str) -> list[str]:
    return ["loginctl", "show-session", session, "--property=Remote", "--property=Seat", "--property=Active"]


@dataclass(frozen=True)
class Presence:
    ssh: bool
    seat: bool
    why_no_seat: str = ""
    unattended: bool = False

    def blocks(self) -> str | None:
        """Why a disruptive change mustn't happen now; None when it may."""
        if self.unattended:
            return UNATTENDED
        if self.ssh:
            return "running over SSH, where it could cut the connection"
        if not self.seat:
            return f"no local desktop session to come back to ({self.why_no_seat})"
        return None


def detect(host: Host) -> Presence:
    ssh = any(host.env(name) for name in SSH_VARIABLES)
    sessions = list(dict.fromkeys(s for s in (host.env("XDG_SESSION_ID"), "auto") if s))
    answer = None
    for session in sessions:
        answer = host.run(session_query(session))
        if answer.returncode == 0:
            break
    if answer is None or answer.returncode != 0:
        return Presence(ssh, False, "logind doesn't know this session")
    properties = dict(line.partition("=")[::2] for line in answer.stdout.splitlines() if "=" in line)
    if properties.get("Remote") == "yes":
        return Presence(True, False, "a remote session")
    if not properties.get("Seat"):
        return Presence(ssh, False, "the session has no seat")
    if properties.get("Active") != "yes":
        return Presence(ssh, False, "the session isn't the active one on its seat")
    return Presence(ssh, True)


def of(host: Host, cache: dict) -> Presence:
    """This run's presence, detected on first use."""
    if CACHE_KEY not in cache:
        cache[CACHE_KEY] = Presence(False, False, UNATTENDED, unattended=True) if cache.get(UNATTENDED_KEY) else detect(host)
    return cache[CACHE_KEY]
