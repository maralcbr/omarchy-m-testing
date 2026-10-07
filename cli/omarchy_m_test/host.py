"""The host boundary: the CLI's only seam.

Every interaction with the machine, the human and the network goes through a
Host. Nothing else in the package may import subprocess, open files, read
input, print or open network connections (scripts/check_boundary.py enforces
this). Tests swap RealHost for a RecordedHost (see recording.py) that replays
what a real Mac answered.

Operations:
  run(argv)                  run a command (no shell), capture its output
  run_bundled(name, args)    run one of the tool's own bundled scripts (bundled.py),
                             e.g. omarchy-mac's mac-check; recorded as bundled_argv()
  read_file(path)            read a regular file's bytes; FileNotFoundError if absent. Bounded:
                             anything but a regular file (a FIFO, a device, a directory) is
                             refused, more than READ_LIMIT_BYTES is EFBIG, and a read that
                             takes longer than READ_TIMEOUT_SECONDS is TimeoutError (all OSError)
  list_dir(path)             list a directory's entry names, sorted (within READ_TIMEOUT_SECONDS too)
  regular_files(path)        the names of a directory's regular files, sorted, symlinks not followed
                             (what `find PATH -maxdepth 1 -type f` lists: nothing when PATH itself is a
                             symlink); FileNotFoundError if absent
  monitor_intent(outputs)   read monitor rules locally; only policy flags leave the host,
                             never user configuration text or monitor description selectors
  touchid_snapshot()         passive SEP state and firmware provenance, with only
                             allowlisted facts and kernel event categories retained
  prompt(message)            ask the human; returns the typed line; EOFError on end of input
  show(text)                 show text to the human
  write_file(path, text, private=False)
                             write a file the CLI produces (the report); private: only this
                             user can read it (0600, in a 0700 directory: the checkpoint)
  post_json(url, body)       POST a JSON text body; returns the HTTP response
  post_form(url, fields)     POST form fields, asking for JSON back (GitHub's device flow); returns
                             the HTTP response
  sleep(seconds)             wait (between polls of GitHub's device flow)
  wait_key(seconds, status)  wait up to `seconds` for a key the human presses on the terminal; the key,
                             or None. `status` is shown on one line meanwhile (a countdown) and cleared
                             after, when the output is a terminal; with no terminal to read, it just waits
  now()                      the time, in whole seconds since the epoch (a signed tester request's date)
  get(url)                   GET a small text (the latest release's version); NetworkError if unreachable
  env(name)                  an environment variable's value; None when unset
  terminal()                 the terminal's size when the human is at one (stdin and stdout
                             a TTY), else None: output is then plain text, no colours or redraws
  run_tty(argv, env)         run an interactive command on the terminal (gum): it draws on
                             the terminal and reads keys; only its stdout is captured
  remove_file(path)          remove a file the CLI wrote itself (the checkpoint); no error if absent
  machine_sign(key_path, namespace, message)
                             sign bytes with this machine's ed25519 key at key_path (ssh-keygen -Y
                             sign), creating it silently on first use: 0600, in a 0700 directory.
                             Returns the public key and the signature; SigningError if it can't.
                             Only the public key and signatures ever leave the machine.

Nothing a command runs may hold up the run (a pager, a password prompt, a
PipeWire or D-Bus call to a wedged server). Every command RealHost runs:
  - gets a hard time limit: COMMAND_TIMEOUT_SECONDS, or its program's in
    PROGRAM_TIMEOUT_SECONDS, a bundled script's in BUNDLED_TIMEOUTS; when it
    runs out the whole process group is killed and the result says so
    (CommandResult.timed_out, exit 124). A section's host (Bounded) turns that
    into TimedOut, which the section reports as skipped, never as a failure;
  - reads stdin from /dev/null, runs in its own process group (so a child
    that opens the terminal to ask for a password is stopped, never shown,
    and a Ctrl-C reaches only this tool), and has PAGER/SYSTEMD_PAGER=cat;
  - runs sudo non-interactively: `sudo` without -n gets it (the one password
    prompt is `sudo -v`, on the terminal through run_tty).
Bundled scripts also run with a PATH shim (SHIMMED_PROGRAMS): each IPC tool
they call (pactl, wpctl, pw-dump, journalctl, systemctl...) gets its own time
limit, and a timed-out one prints "TIMEOUT <program> <seconds>" into the
script's output (SHIM_MARKER), so the result it was for is skipped
(scripts.py) while the script's other checks still report.
"""

from __future__ import annotations

import atexit
import errno
import http.client
import os
import shutil
import signal
import subprocess
import sys
import select
import stat
import tempfile
import termios
import threading
import time
import tty
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Protocol, Sequence

from . import bundled

COMMAND_TIMEOUT_SECONDS = 60
# IPC to a session server (PipeWire, PulseAudio, D-Bus, systemd): an answer takes well under a second, or never comes.
IPC_TIMEOUT_SECONDS = 15
PROGRAM_TIMEOUT_SECONDS = {
    **{name: IPC_TIMEOUT_SECONDS for name in (
        "pactl", "pacat", "parec", "paplay", "pw-dump", "pw-cli", "pw-cat", "pw-play", "pw-record", "pw-metadata", "wpctl",
        "busctl", "gdbus", "dbus-send", "bluetoothctl", "iwctl", "hyprctl", "brightnessctl",
    )},
    "systemctl": 20,
    "nmcli": 20,
}
# A script that starts with LONG_RUNNING waits on the human (the lid, the charger): it gets WATCH_TIMEOUT_SECONDS.
# The clock the timeout counts stops while the Mac is asleep (CLOCK_MONOTONIC), so a suspend doesn't use it up.
LONG_RUNNING = ": omarchy-m-test waits on the human\n"
WATCH_TIMEOUT_SECONDS = 1800
# Bundled check scripts run many commands (mac-check's boot check rebuilds and
# compares the m1n1 image), so they get longer.
BUNDLED_TIMEOUT_SECONDS = 300
# The read-only omarchy-mac checks take seconds; a wedged audio or display server must not hold the run for minutes.
BUNDLED_TIMEOUTS = {"apple-audio-check": 90, "apple-display-check": 90}
# Inside a bundled script, the programs that get their own limit (the shim), and how long.
SHIMMED_PROGRAMS = {**PROGRAM_TIMEOUT_SECONDS, "journalctl": 30, "pacman": 60}
SHIM_MARKER = "TIMEOUT"  # "TIMEOUT pactl 15" (or "TIMEOUT pactl -": the script's own timeout stopped it): a line the shim added
SHIM_FD = 9  # where the shim writes its marker: the script's own stdout, even where a check discards it
# Everything a command runs sees these: no pager, no prompt for a password or a Git credential.
QUIET_ENV = {"PAGER": "cat", "SYSTEMD_PAGER": "cat", "GIT_PAGER": "cat", "SYSTEMD_PAGERSECURE": "1", "GIT_TERMINAL_PROMPT": "0"}
# Installing and removing temporary test packages downloads them first.
PACKAGE_TIMEOUT_SECONDS = 1800
HTTP_TIMEOUT_SECONDS = 30
# GETs only look up the latest release; a slow or absent network must not hold up a run.
GET_TIMEOUT_SECONDS = 5
# File reads (sysfs, debugfs, configuration): a driver that never answers or a FIFO a config names must not hold the run.
READ_TIMEOUT_SECONDS = 5
READ_LIMIT_BYTES = 16 * 1024 * 1024
# Reads still stuck past their deadline (their threads can't be cancelled); past this many, reads are refused.
READ_STUCK_LIMIT = 8
BUNDLED_PREFIX = "bundled:"
MONITOR_INTENT = ["read:monitor-intent"]  # the derived policy flags in a recording
TOUCHID_SNAPSHOT = ["read:touch-id"]


def bundled_argv(name: str, args: Sequence[str] = ()) -> list[str]:
    """How a bundled script's run appears in a recording: ["bundled:mac-check", *args]."""
    return [BUNDLED_PREFIX + name, *args]


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: int = 0  # the time limit, in seconds, the host stopped it at (0: it ended by itself)


class TimedOut(Exception):
    """A command in a section ran out of time (Bounded): the section reports its checks as skipped."""

    def __init__(self, argv: Sequence[str], seconds: int):
        super().__init__(f"{' '.join(argv)} timed out after {seconds}s")
        self.argv = list(argv)
        self.seconds = seconds


class Bounded:
    """A section's host: a command the host stopped at its time limit raises TimedOut.

    A bundled script's timeout doesn't raise: scripts.py keeps what it printed
    and skips the rest. Everything else passes straight through.
    """

    def __init__(self, inner: "Host"):
        self.inner = inner

    def run(self, argv: Sequence[str]) -> CommandResult:
        result = self.inner.run(argv)
        if result.timed_out:
            raise TimedOut(argv, result.timed_out)
        return result

    def __getattr__(self, name: str):
        return getattr(self.inner, name)


@dataclass(frozen=True)
class Terminal:
    width: int
    height: int


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: str


class NetworkError(Exception):
    """The request never got an HTTP response (DNS, TLS, refused, timeout)."""


@dataclass(frozen=True)
class MachineSignature:
    public_key: str  # "ssh-ed25519 AAAA...", without a comment
    signature: str  # the armored SSH signature ssh-keygen -Y sign prints


class SigningError(Exception):
    """The machine key couldn't be created or used (no ssh-keygen, no home directory)."""


class Host(Protocol):
    def run(self, argv: Sequence[str]) -> CommandResult: ...

    def run_bundled(self, name: str, args: Sequence[str] = ()) -> CommandResult: ...

    def read_file(self, path: str) -> bytes: ...

    def list_dir(self, path: str) -> list[str]: ...

    def regular_files(self, path: str) -> list[str]: ...

    def monitor_intent(self, outputs: list[dict]) -> dict[str, dict]: ...

    def touchid_snapshot(self) -> dict: ...

    def prompt(self, message: str) -> str: ...

    def show(self, text: str) -> None: ...

    def write_file(self, path: str, text: str, private: bool = False) -> None: ...

    def post_json(self, url: str, body: str) -> HttpResponse: ...

    def post_form(self, url: str, fields: dict[str, str]) -> HttpResponse: ...

    def sleep(self, seconds: float) -> None: ...

    def wait_key(self, seconds: float, status: str = "") -> str | None: ...

    def now(self) -> int: ...

    def get(self, url: str) -> HttpResponse: ...

    def env(self, name: str) -> str | None: ...

    def terminal(self) -> Terminal | None: ...

    def run_tty(self, argv: Sequence[str], env: dict[str, str] | None = None) -> CommandResult: ...

    def remove_file(self, path: str) -> None: ...

    def machine_sign(self, key_path: str, namespace: str, message: bytes) -> MachineSignature: ...


def _changes_packages(argv: list[str]) -> bool:
    """sudo -n pacman -S/-R ...: the temporary test packages (packages.py)."""
    command = argv[2:] if argv[:2] == ["sudo", "-n"] else argv
    return command[:1] == ["pacman"] and any(a.startswith(("-S", "-R")) and not a.startswith("-Sp") for a in command[1:2])


def timeout_for(argv: Sequence[str]) -> int:
    """How long RealHost.run gives a command."""
    argv = list(argv)
    if _changes_packages(argv):
        return PACKAGE_TIMEOUT_SECONDS
    if argv[:2] == ["sh", "-c"] and len(argv) > 2 and argv[2].startswith(LONG_RUNNING):
        return WATCH_TIMEOUT_SECONDS
    command = argv[2:] if argv[:2] == ["sudo", "-n"] else argv
    return PROGRAM_TIMEOUT_SECONDS.get(os.path.basename(command[0]) if command else "", COMMAND_TIMEOUT_SECONDS)


def non_interactive(argv: Sequence[str]) -> list[str]:
    """sudo never asks for a password: `sudo ...` becomes `sudo -n ...`."""
    argv = list(argv)
    if argv[:1] == ["sudo"] and "-n" not in argv[1:2] and "--non-interactive" not in argv[1:2]:
        return ["sudo", "-n", *argv[1:]]
    return argv


# A shimmed program: the real one (found on the PATH the script started with), under its own time limit.
# timeout(1) runs it in a process group of its own and kills all of it. It runs in the background so the
# shim can still say it timed out when the script's own `timeout 10 ...` stops the shim first (TERM);
# stdin is handed on explicitly (a background job's would be /dev/null). If the script is killed outright,
# timeout still ends its program within the limit.
_SHIM = """#!/bin/sh
exec 7<&0
PATH=$OMARCHY_M_TEST_PATH timeout -k 2 {seconds} {command} "$@" <&7 7<&- &
child=$!
trap 'kill -TERM "$child" 2>/dev/null; echo "{marker} {name} -" 2>/dev/null >&{fd}; exit 124' TERM INT
wait "$child"
status=$?
if [ "$status" -eq 124 ] || [ "$status" -eq 137 ]; then
  echo "{marker} {name} {seconds}" 2>/dev/null >&{fd}
fi
exit "$status"
"""

_WITH_MARKER_FD = f'exec bash "$0" "$@" {SHIM_FD}>&1'


def _shim(name: str, seconds: int | str, command: str) -> str:
    return _SHIM.format(seconds=seconds, name=name, command=command, marker=SHIM_MARKER, fd=SHIM_FD)


def _sudo_shim() -> str:
    """sudo, always -n; a shimmed program run through it (`sudo -n journalctl -k`) keeps its limit (sudo's own PATH skips the shim)."""
    limits = "\n".join(f"  {program}) limit={seconds} ;;" for program, seconds in SHIMMED_PROGRAMS.items())
    shimmed = _shim("$program", "$limit", "sudo -n").replace("#!/bin/sh\n", "")
    return f"""#!/bin/sh
program=
for arg in "$@"; do
  case $arg in -*) ;; *) program=${{arg##*/}}; break ;; esac
done
limit=
case $program in
{limits}
esac
if [ -z "$limit" ]; then PATH=$OMARCHY_M_TEST_PATH exec sudo -n "$@"; fi
{shimmed}"""


class RealHost:
    """The host backed by this machine, its terminal and the network."""

    def touchid_snapshot(self) -> dict:
        from . import touchid
        return touchid.collect(self)

    def __init__(self) -> None:
        self._shims: str | None = None

    def run(self, argv: Sequence[str]) -> CommandResult:
        argv = non_interactive(argv)
        return self._run(argv, timeout_for(argv))

    def run_bundled(self, name: str, args: Sequence[str] = ()) -> CommandResult:
        try:
            path = bundled.script_path(name)
        except FileNotFoundError as missing:
            return CommandResult(127, "", f"{missing}\n")
        timeout = BUNDLED_TIMEOUTS.get(name, BUNDLED_TIMEOUT_SECONDS)
        env = {"OMARCHY_M_TEST_PATH": os.environ.get("PATH", os.defpath)}
        shims = self._shim_dir()
        if shims:
            env["PATH"] = shims + os.pathsep + env["OMARCHY_M_TEST_PATH"]
        return self._run(["bash", "-c", _WITH_MARKER_FD, path, *args], timeout, name, env)

    def _shim_dir(self) -> str | None:
        """The shim directory, made once per run and removed at exit; None if it can't be made (no shim then)."""
        if self._shims is None:
            if shutil.which("timeout") is None:  # coreutils' timeout(1) does the limiting; without it, no shim
                return None
            try:
                directory = tempfile.mkdtemp(prefix="omarchy-m-test-shims.")
                atexit.register(shutil.rmtree, directory, True)
                for program, seconds in SHIMMED_PROGRAMS.items():
                    self._write_shim(directory, program, _shim(program, seconds, program))
                self._write_shim(directory, "sudo", _sudo_shim())
            except OSError:
                return None
            self._shims = directory
        return self._shims

    @staticmethod
    def _write_shim(directory: str, program: str, text: str) -> None:
        path = os.path.join(directory, program)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(path, 0o755)

    def _run(self, argv: list[str], timeout: int, name: str | None = None, env: dict[str, str] | None = None) -> CommandResult:
        name = name or argv[0]
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={**os.environ, **QUIET_ENV, **(env or {})},
                process_group=0,
            )
        except FileNotFoundError:
            return CommandResult(127, "", f"{name}: command not found\n")
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as expired:
            _kill_group(process)
            stdout, stderr = _leftovers(process, expired)
            text = _text(stderr)
            if text and not text.endswith("\n"):
                text += "\n"
            return CommandResult(124, _text(stdout), text + f"{name}: timed out after {timeout}s\n", timed_out=timeout)
        except BaseException:  # Ctrl-C: the command's group doesn't get the terminal's signal, so end it here
            _kill_group(process)
            process.wait()
            raise
        return CommandResult(process.returncode, _text(stdout), _text(stderr))

    def read_file(self, path: str) -> bytes:
        return bounded_read(path)

    def list_dir(self, path: str) -> list[str]:
        return bounded(lambda: sorted(os.listdir(path)), path)

    def regular_files(self, path: str) -> list[str]:
        return bounded(lambda: _regular_files(path), path)

    def monitor_intent(self, outputs: list[dict]) -> dict[str, dict]:
        from .monitor_rules import intent

        return intent(self, outputs)

    def prompt(self, message: str) -> str:
        return input(message)

    def show(self, text: str) -> None:
        try:
            print(text)
            sys.stdout.flush()
        except OSError:
            pass  # the terminal is gone (closed window, broken pipe); restoring must still go on

    def write_file(self, path: str, text: str, private: bool = False) -> None:
        """Write whole or not at all: a checkpoint cut short by a crash must not be half a file."""
        if os.path.exists(path) and not os.path.isfile(path):  # /dev/stdout, a FIFO
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            return
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, mode=0o700 if private else 0o777, exist_ok=True)
            if private:
                os.chmod(directory, 0o700)
        partial = path + ".partial"
        if os.path.lexists(partial):
            os.remove(partial)
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600 if private else 0o666)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(partial, path)

    def remove_file(self, path: str) -> None:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    def env(self, name: str) -> str | None:
        return os.environ.get(name)

    def terminal(self) -> Terminal | None:
        if os.environ.get("TERM") == "dumb" or not (sys.stdin.isatty() and sys.stdout.isatty()):
            return None
        try:
            size = os.get_terminal_size(sys.stdout.fileno())
        except OSError:
            return None
        return Terminal(size.columns, size.lines)

    def run_tty(self, argv: Sequence[str], env: dict[str, str] | None = None) -> CommandResult:
        """On the terminal, in the foreground (it reads keys). A prompt waits on the human for as long as it takes;
        a watch (LONG_RUNNING: the lid, the charger) gets WATCH_TIMEOUT_SECONDS, like in run()."""
        argv = list(argv)
        watch = argv[:2] == ["sh", "-c"] and len(argv) > 2 and argv[2].startswith(LONG_RUNNING)
        try:
            process = subprocess.Popen(argv, stdout=subprocess.PIPE, env={**os.environ, **QUIET_ENV, **(env or {})})
        except FileNotFoundError:
            return CommandResult(127, "", f"{argv[0]}: command not found\n")
        try:
            stdout, _ = process.communicate(timeout=WATCH_TIMEOUT_SECONDS if watch else None)
        except subprocess.TimeoutExpired as expired:
            process.kill()
            stdout, _ = _leftovers(process, expired)
            return CommandResult(124, _text(stdout), f"{argv[0]}: timed out after {WATCH_TIMEOUT_SECONDS}s\n", timed_out=WATCH_TIMEOUT_SECONDS)
        except BaseException:
            process.kill()
            process.wait()
            raise
        return CommandResult(process.returncode, _text(stdout), "")

    def post_json(self, url: str, body: str) -> HttpResponse:
        return self._post(url, body.encode("utf-8"), "application/json")

    def post_form(self, url: str, fields: dict[str, str]) -> HttpResponse:
        return self._post(url, urllib.parse.urlencode(fields).encode("ascii"), "application/x-www-form-urlencoded")

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def wait_key(self, seconds: float, status: str = "") -> str | None:
        if not sys.stdin.isatty():
            time.sleep(seconds)
            return None
        shown = bool(status) and sys.stdout.isatty()  # keys are read from the terminal even when the output is piped
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        try:
            if shown:
                sys.stdout.write("\r\x1b[K" + status)
                sys.stdout.flush()
            # A key without Enter, not echoed; Ctrl-C still interrupts. TCSANOW: a key pressed before this
            # wait (while the CLI polled GitHub) stays queued, where the default TCSAFLUSH would drop it.
            tty.setcbreak(fd, termios.TCSANOW)
            ready, _, _ = select.select([fd], [], [], seconds)
            # Up to 32 bytes: an arrow or function key's whole escape sequence is one key.
            return os.read(fd, 32).decode("utf-8", "replace") if ready else None
        finally:
            termios.tcsetattr(fd, termios.TCSANOW, saved)
            if shown:
                sys.stdout.write("\r\x1b[K")
                sys.stdout.flush()

    def now(self) -> int:
        return int(time.time())

    def _post(self, url: str, data: bytes, content_type: str) -> HttpResponse:
        request = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={"Content-Type": content_type, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                return HttpResponse(response.status, response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as error:
            return HttpResponse(error.code, error.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
            raise NetworkError(str(getattr(error, "reason", error))) from error

    def machine_sign(self, key_path: str, namespace: str, message: bytes) -> MachineSignature:
        directory = os.path.dirname(key_path)
        try:
            os.makedirs(directory, mode=0o700, exist_ok=True)
            os.chmod(directory, 0o700)
            if not os.path.exists(key_path):
                # No passphrase: the key only ever signs reports, and a run must never prompt for it.
                self._ssh_keygen(["-q", "-t", "ed25519", "-N", "", "-C", "", "-f", key_path])
            os.chmod(key_path, 0o600)  # ssh-keygen refuses a key others can read
            public_key_path = key_path + ".pub"
            if not os.path.exists(public_key_path):  # only the private half survived: derive it again
                self.write_file(public_key_path, self._ssh_keygen(["-y", "-f", key_path]))
            with open(public_key_path, encoding="utf-8") as f:
                public_key = " ".join(f.read().split()[:2])
        except OSError as error:
            raise SigningError(f"couldn't keep the machine key in {directory} ({error.strerror or error})") from error
        signature = self._ssh_keygen(["-Y", "sign", "-f", key_path, "-n", namespace], message)
        return MachineSignature(public_key, signature.strip())

    def _ssh_keygen(self, args: list[str], message: bytes = b"") -> str:
        try:
            done = subprocess.run(["ssh-keygen", *args], input=message, capture_output=True, timeout=COMMAND_TIMEOUT_SECONDS)
        except FileNotFoundError as error:
            raise SigningError("ssh-keygen isn't installed (it comes with openssh)") from error
        except subprocess.TimeoutExpired as error:
            raise SigningError(f"ssh-keygen timed out after {COMMAND_TIMEOUT_SECONDS}s") from error
        if done.returncode != 0:
            reason = done.stderr.decode("utf-8", "replace").strip().splitlines()
            raise SigningError(f"ssh-keygen failed ({reason[-1] if reason else f'exit {done.returncode}'})")
        return done.stdout.decode("utf-8", "replace")

    def get(self, url: str) -> HttpResponse:
        request = urllib.request.Request(url, headers={"Accept": "text/plain"})
        try:
            with urllib.request.urlopen(request, timeout=GET_TIMEOUT_SECONDS) as response:
                return HttpResponse(response.status, response.read(4096).decode("utf-8", "replace"))
        except urllib.error.HTTPError as error:
            return HttpResponse(error.code, "")
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as error:
            raise NetworkError(str(getattr(error, "reason", error))) from error


_stuck = threading.BoundedSemaphore(READ_STUCK_LIMIT)


def _read_regular(path: str, limit: int) -> bytes:
    """Open without blocking (a FIFO with no writer, a tty), refuse anything but a regular file, read at most limit."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "not a regular file", path)
        chunks, size = [], 0
        while size <= limit:  # pseudo-files report st_size 0 or 4096: read to the end, never trust it
            chunk = os.read(fd, min(65536, limit + 1 - size))
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            size += len(chunk)
        raise OSError(errno.EFBIG, f"larger than {limit} bytes", path)
    finally:
        os.close(fd)


def bounded(work, path: str, timeout: float | None = None):
    """work() within a deadline, in a worker thread: TimeoutError when it doesn't answer (the stuck worker is
    dropped; past READ_STUCK_LIMIT of them, everything is refused at once)."""
    if not _stuck.acquire(blocking=False):
        raise TimeoutError(errno.ETIMEDOUT, "too many file reads still stuck", path)
    outcome: list = []

    def run() -> None:
        try:
            outcome.append((True, work()))
        except BaseException as problem:  # handed to the caller
            outcome.append((False, problem))
        finally:
            _stuck.release()

    worker = threading.Thread(target=run, name="omarchy-m-test-read", daemon=True)
    worker.start()
    timeout = READ_TIMEOUT_SECONDS if timeout is None else timeout
    worker.join(timeout)
    if not outcome:
        raise TimeoutError(errno.ETIMEDOUT, f"timed out after {timeout:g}s", path)
    ok, value = outcome[0]
    if ok:
        return value
    raise value


def bounded_read(path: str, timeout: float | None = None, limit: int = READ_LIMIT_BYTES) -> bytes:
    """A regular file's bytes within a deadline; the worker owns its descriptor and a late answer is dropped."""
    return bounded(lambda: _read_regular(path, limit), path, timeout)


def _regular_files(path: str) -> list[str]:
    if os.path.islink(path):
        return []  # find without -L doesn't descend into a symlinked starting point
    with os.scandir(path) as entries:
        return sorted(entry.name for entry in entries if entry.is_file(follow_symlinks=False))


def _kill_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        pass  # it ended already
    try:
        process.kill()
    except OSError:
        pass


def _leftovers(process: subprocess.Popen, expired: subprocess.TimeoutExpired) -> tuple[bytes, bytes]:
    """What a killed command printed. A daemon it started in a session of its own may keep the pipes open: don't wait for it."""
    try:
        return process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass
        process.wait()
        return expired.stdout or b"", expired.stderr or b""


def _text(data: bytes | str | None) -> str:
    if data is None:
        return ""
    return data if isinstance(data, str) else data.decode("utf-8", "replace")
