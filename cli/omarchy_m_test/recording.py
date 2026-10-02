"""Recordings: capture what a real machine answers at the host boundary, and replay it.

A recording is a JSON file (recording_version 1):

  {
    "recording_version": 1,
    "description": "what machine and install this is",
    "source": "where the answers came from",
    "commands": [
      {"argv": ["uname", "-r"], "returncode": 0, "stdout": "...\\n", "stderr": ""}
    ],
    "files": {
      "/proc/device-tree/model": {"text": "Apple ...\\u0000"},
      "/some/binary": {"base64": "..."},
      "/some/redacted/binary": {"redacted_bytes": 6},
      "/proc/device-tree/compatible": null
    },
    "dirs": {"/proc/device-tree": ["compatible", "model"]},
    "env": {"HOME": "/home/<user>"}
  }

A bundled script's run (Host.run_bundled) is a command whose argv starts with
"bundled:<name>", e.g. ["bundled:mac-check"]. A command the host stopped at its
time limit carries "timed_out": the limit in seconds (CommandResult.timed_out).

A file or directory mapped to null is recorded as absent (FileNotFoundError).
A binary file the recorder could not scrub is kept only as its size and
replays as that many zero bytes. An environment variable the recording
doesn't list is unset; "env" is only saved when a run read one that was set.

Record mode (`omarchy-m-test --record FILE`) wraps the real host in a
RecordingHost: every command, file and directory the CLI asks for is kept,
plus the RECORDED_SOURCES that later checks need (kernel log, device tree,
first-boot, Wi-Fi and lid journals, PCI devices). Before the
recording is written it passes the privacy Scrubber (privacy.py), so no
hostname, username, network name or address is ever saved. Prompts, what the
human typed and uploads are not recorded.

Replay: a RecordedHost answers from a recording. Anything the CLI asks for
that the recording doesn't mention raises RecordingMiss, so a test can never
pass by silently reading this machine. The human side is scripted: `answers`
are returned by prompt() in order (EOF ends input like Ctrl-D; ENDED, left
last, is input that stays closed: every later prompt gets end of input and
every later interactive command exits 1, as gum does with no answer). Uploads get
the scripted `responses` in order; form POSTs (GitHub's device flow) get the
scripted `forms` in order, and sleeps return at once (kept in `slept`); waits for a key
(wait_key) return the scripted `keys` in order, at once. The clock (now) starts at `clock` and
moves on by each sleep and each wait no key ended. GETs (the latest-release lookup) get
the scripted `fetches` by URL; a URL that isn't scripted behaves like a
machine with no network (NetworkError). The answer INTERRUPT at a prompt or an
interactive command is Ctrl-C there (KeyboardInterrupt). Interactive commands
(run_tty, gum) also take their scripted result from `answers`: a
CommandResult, a string (its stdout, exit 0), EOF or INTERRUPT. The host is
not at a terminal unless the test gives one (`terminal`). A file the CLI wrote
reads back what it wrote, until it removes it. Everything shown, prompted,
written and posted is kept for assertions.

The machine key is never recorded. A RecordedHost has none unless the test
hands it a `signer` (a machine_sign function, e.g. a RealHost's with the
key in a temporary directory); without one, signing fails as on a machine
without ssh-keygen. Record mode passes signing straight through.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .host import TOUCHID_SNAPSHOT, CommandResult, Host, HttpResponse, MachineSignature, NetworkError, SigningError, Terminal, bundled_argv
from .privacy import HOME_DIR, HOSTNAME_PATH, SERIAL_FILES, Scrubber

RECORDING_VERSION = 1
# A RecordedHost's clock (Host.now): 2026-09-28 00:00:00 UTC, the golden tester requests' date.
RECORDED_CLOCK = 1790553600

# What record mode captures beyond what the CLI itself asks for, so that
# recordings from real Macs carry the evidence later checks read.
RECORDED_SOURCES: tuple[list[str], ...] = (
    ["journalctl", "--dmesg", "--boot=0", "--no-pager"],
    ["dtc", "-I", "fs", "-O", "dts", "/proc/device-tree"],
    ["journalctl", "--unit=omarchy-provision-hardware.service", "--output=short-iso", "--no-pager"],
    ["journalctl", "--boot=0", "--unit=NetworkManager.service", "--unit=iwd.service", "--output=short-precise", "--no-pager"],
    ["journalctl", "--boot=0", "--unit=systemd-logind.service", "--no-pager"],
    ["uname", "-a"],
    ["lspci", "-nn"],
    ["ip", "-brief", "address"],
)

# Binary files whose content may be kept: only ones a check produced itself,
# such as the video check's screenshots of its test card (video.py).
RECORDABLE_BINARY_PREFIXES: tuple[str, ...] = ("/tmp/omarchy-m-test-video.",)


class RecordingMiss(Exception):
    """The CLI asked for something the recording (or the script) doesn't have."""


class _Eof:
    def __repr__(self) -> str:
        return "EOF"


EOF = _Eof()


class _Ended:
    def __repr__(self) -> str:
        return "ENDED"


ENDED = _Ended()


class _Interrupt:
    def __repr__(self) -> str:
        return "INTERRUPT"


INTERRUPT = _Interrupt()


@dataclass(frozen=True)
class Post:
    url: str
    body: str


@dataclass
class RecordedHost:
    recording: dict[str, Any]
    answers: list[Any] = field(default_factory=list)
    responses: list[HttpResponse] = field(default_factory=list)
    forms: list[HttpResponse] = field(default_factory=list)
    fetches: dict[str, HttpResponse] = field(default_factory=dict)
    terminal_size: Terminal | None = None
    signer: Callable[[str, str, bytes], MachineSignature] | None = None
    # What happened, in order: ("show", text) / ("prompt", message, answer) / ("tty", argv, answer)
    transcript: list[tuple] = field(default_factory=list)
    commands_run: list[list[str]] = field(default_factory=list)
    written: dict[str, str] = field(default_factory=dict)
    posts: list[Post] = field(default_factory=list)
    # (url, fields) per post_form call
    form_posts: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    slept: list[float] = field(default_factory=list)
    # Keys the human presses while the CLI waits for one (wait_key), in order: a key, None (no key in
    # that wait) or INTERRUPT (Ctrl-C). With none left, no key comes. Each wait is kept in `waited`.
    keys: list[Any] = field(default_factory=list)
    waited: list[tuple[float, str]] = field(default_factory=list)
    clock: int = RECORDED_CLOCK
    gets: list[str] = field(default_factory=list)
    removed: set[str] = field(default_factory=set)
    private: set[str] = field(default_factory=set)
    # (key_path, namespace, message) per machine_sign call
    signed: list[tuple[str, str, bytes]] = field(default_factory=list)

    def __post_init__(self) -> None:
        version = self.recording.get("recording_version")
        if version != RECORDING_VERSION:
            raise ValueError(f"unsupported recording_version {version!r}")
        self.answers = list(self.answers)
        self.responses = list(self.responses)
        self.forms = list(self.forms)

    @classmethod
    def load(cls, path: str, **kwargs: Any) -> "RecordedHost":
        with open(path, encoding="utf-8") as f:  # test harness: reads the recording, not the host
            return cls(json.load(f), **kwargs)

    # -- machine -------------------------------------------------------

    def run(self, argv: Sequence[str]) -> CommandResult:
        argv = list(argv)
        self.commands_run.append(argv)
        for entry in self.recording.get("commands", []):
            if entry["argv"] == argv:
                return CommandResult(entry["returncode"], entry.get("stdout", ""), entry.get("stderr", ""), entry.get("timed_out", 0))
        raise RecordingMiss(f"command not in recording: {argv}")

    def run_bundled(self, name: str, args: Sequence[str] = ()) -> CommandResult:
        return self.run(bundled_argv(name, args))

    def touchid_snapshot(self) -> dict:
        return json.loads(self.run(TOUCHID_SNAPSHOT).stdout)

    def read_file(self, path: str) -> bytes:
        if path in self.written:
            return self.written[path].encode("utf-8")
        if path in self.removed:
            raise FileNotFoundError(path)
        files = self.recording.get("files", {})
        if path not in files:
            raise RecordingMiss(f"file not in recording: {path}")
        entry = files[path]
        if entry is None:
            raise FileNotFoundError(path)
        if "text" in entry:
            return entry["text"].encode("utf-8")
        if "redacted_bytes" in entry:
            return bytes(entry["redacted_bytes"])
        return base64.b64decode(entry["base64"])

    def list_dir(self, path: str) -> list[str]:
        dirs = self.recording.get("dirs", {})
        if path not in dirs:
            raise RecordingMiss(f"directory not in recording: {path}")
        if dirs[path] is None:
            raise FileNotFoundError(path)
        return sorted(dirs[path])

    def env(self, name: str) -> str | None:
        return self.recording.get("env", {}).get(name)

    # -- human ---------------------------------------------------------

    def terminal(self) -> Terminal | None:
        return self.terminal_size

    def prompt(self, message: str) -> str:
        if not self.answers:
            raise RecordingMiss(f"no scripted answer left for prompt: {message!r}")
        if self.answers[0] is ENDED:
            self.transcript.append(("prompt", message, ENDED))
            raise EOFError
        answer = self.answers.pop(0)
        self.transcript.append(("prompt", message, answer))
        if answer is EOF:
            raise EOFError
        if answer is INTERRUPT:
            raise KeyboardInterrupt
        return answer

    def run_tty(self, argv: Sequence[str], env: dict[str, str] | None = None) -> CommandResult:
        if not self.answers:
            raise RecordingMiss(f"no scripted answer left for interactive command: {list(argv)}")
        if self.answers[0] is ENDED:
            self.transcript.append(("tty", list(argv), ENDED))
            return CommandResult(1, "", "")
        answer = self.answers.pop(0)
        self.transcript.append(("tty", list(argv), answer))
        if answer is INTERRUPT:
            raise KeyboardInterrupt
        if answer is EOF:
            return CommandResult(130, "", "")
        if isinstance(answer, str):
            return CommandResult(0, answer + "\n" if answer else "", "")
        return answer

    def show(self, text: str) -> None:
        self.transcript.append(("show", text))

    # -- outputs -------------------------------------------------------

    def write_file(self, path: str, text: str, private: bool = False) -> None:
        self.written[path] = text
        self.removed.discard(path)
        if private:
            self.private.add(path)

    def remove_file(self, path: str) -> None:
        self.written.pop(path, None)
        self.removed.add(path)

    def machine_sign(self, key_path: str, namespace: str, message: bytes) -> MachineSignature:
        self.signed.append((key_path, namespace, message))
        if self.signer is None:
            raise SigningError("ssh-keygen isn't installed (it comes with openssh)")
        return self.signer(key_path, namespace, message)

    def post_json(self, url: str, body: str) -> HttpResponse:
        self.posts.append(Post(url, body))
        if not self.responses:
            raise RecordingMiss(f"no scripted response left for POST {url}")
        return self.responses.pop(0)

    def post_form(self, url: str, fields: dict[str, str]) -> HttpResponse:
        self.form_posts.append((url, dict(fields)))
        if not self.forms:
            raise RecordingMiss(f"no scripted response left for form POST {url}")
        answer = self.forms.pop(0)
        if isinstance(answer, NetworkError):
            raise answer
        return answer

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.clock += int(seconds)

    def wait_key(self, seconds: float, status: str = "") -> str | None:
        self.waited.append((seconds, status))
        key = self.keys.pop(0) if self.keys else None
        if key is None:
            self.clock += int(seconds)  # the whole wait went by
        if key is INTERRUPT:
            raise KeyboardInterrupt
        return key

    def now(self) -> int:
        return self.clock

    def get(self, url: str) -> HttpResponse:
        self.gets.append(url)
        if url not in self.fetches:
            raise NetworkError(f"no network in this recording: GET {url}")
        return self.fetches[url]

    # -- assertions helpers ---------------------------------------------

    @property
    def output(self) -> str:
        """Everything the human saw, prompts included, as one text."""
        return "\n".join(event[1] for event in self.transcript if isinstance(event[1], str))

    def unused_script(self) -> list[Any]:
        return [answer for answer in self.answers if answer is not ENDED] + self.responses + self.forms


class RecordingHost:
    """Record mode: a host that passes everything to `inner` and keeps what the machine answered."""

    def __init__(self, inner: Host):
        self.inner = inner
        self.commands: list[dict[str, Any]] = []
        self.files: dict[str, Any] = {}
        self.dirs: dict[str, Any] = {}
        self.env_read: dict[str, str | None] = {}

    # -- machine (recorded) ---------------------------------------------

    def run(self, argv: Sequence[str]) -> CommandResult:
        argv = list(argv)
        result = self.inner.run(argv)
        self._keep(argv, result)
        return result

    def touchid_snapshot(self) -> dict:
        found = self.inner.touchid_snapshot()
        self.commands = [entry for entry in self.commands if entry["argv"] != TOUCHID_SNAPSHOT]
        self._keep(TOUCHID_SNAPSHOT, CommandResult(0, json.dumps(found, sort_keys=True), ""))
        return found

    def run_bundled(self, name: str, args: Sequence[str] = ()) -> CommandResult:
        argv = bundled_argv(name, args)
        result = self.inner.run_bundled(name, args)
        self._keep(argv, result)
        return result

    def _keep(self, argv: list[str], result: CommandResult) -> None:
        command = argv[2:] if argv[:1] == ["timeout"] else argv
        if command and command[0] in ("journalctl", "dmesg"):
            from .touchid import log_event, redact_log
            if any(log_event(line) for line in (result.stdout + result.stderr).splitlines()):
                result = CommandResult(result.returncode, redact_log(result.stdout), redact_log(result.stderr), result.timed_out)
        if not any(entry["argv"] == argv for entry in self.commands):
            entry = {"argv": argv, "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
            if result.timed_out:
                entry["timed_out"] = result.timed_out
            self.commands.append(entry)

    def read_file(self, path: str) -> bytes:
        try:
            data = self.inner.read_file(path)
        except FileNotFoundError:
            self.files.setdefault(path, None)
            raise
        self.files.setdefault(path, data)
        return data

    def list_dir(self, path: str) -> list[str]:
        try:
            names = self.inner.list_dir(path)
        except FileNotFoundError:
            self.dirs.setdefault(path, None)
            raise
        self.dirs.setdefault(path, list(names))
        return names

    def env(self, name: str) -> str | None:
        value = self.inner.env(name)
        self.env_read.setdefault(name, value)
        return value

    # -- human and outputs (passed through, not recorded) -----------------

    def terminal(self) -> Terminal | None:
        return self.inner.terminal()

    def prompt(self, message: str) -> str:
        return self.inner.prompt(message)

    def run_tty(self, argv: Sequence[str], env: dict[str, str] | None = None) -> CommandResult:
        return self.inner.run_tty(argv, env)

    def remove_file(self, path: str) -> None:
        self.inner.remove_file(path)

    def show(self, text: str) -> None:
        self.inner.show(text)

    def write_file(self, path: str, text: str, private: bool = False) -> None:
        self.inner.write_file(path, text, private)

    def machine_sign(self, key_path: str, namespace: str, message: bytes) -> MachineSignature:
        return self.inner.machine_sign(key_path, namespace, message)

    def post_json(self, url: str, body: str) -> HttpResponse:
        return self.inner.post_json(url, body)

    def post_form(self, url: str, fields: dict[str, str]) -> HttpResponse:
        return self.inner.post_form(url, fields)

    def sleep(self, seconds: float) -> None:
        self.inner.sleep(seconds)

    def wait_key(self, seconds: float, status: str = "") -> str | None:
        return self.inner.wait_key(seconds, status)

    def now(self) -> int:
        return self.inner.now()

    def get(self, url: str) -> HttpResponse:
        return self.inner.get(url)

    # -- saving ------------------------------------------------------------

    def capture_sources(self, sources: Sequence[Sequence[str]] = RECORDED_SOURCES) -> None:
        """Run each extra source so the recording carries its answer."""
        for argv in sources:
            try:
                self.run(argv)
            except RecordingMiss:
                continue  # recording from a recording that doesn't have this source

    def recording(self, scrubber: Scrubber) -> dict[str, Any]:
        """The scrubbed recording of everything captured so far."""
        for entry in self.commands:  # a serial or device name one output names is removed from all of them
            scrubber.learn(entry["stdout"] + "\n" + entry["stderr"])
        for data in self.files.values():
            if data is not None:
                scrubber.learn(data.decode("utf-8", "replace"))
        recording = {
            "recording_version": RECORDING_VERSION,
            "description": "Recorded by omarchy-m-test --record",
            "source": "omarchy-m-test --record",
            "commands": [
                {
                    "argv": [scrubber.scrub(arg) for arg in entry["argv"]],
                    "returncode": entry["returncode"],
                    "stdout": scrubber.scrub(entry["stdout"]),
                    "stderr": scrubber.scrub(entry["stderr"]),
                    **({"timed_out": entry["timed_out"]} if entry.get("timed_out") else {}),
                }
                for entry in self.commands
            ],
            "files": {scrubber.scrub(path): _file_entry(path, data, scrubber) for path, data in self.files.items()},
            "dirs": {scrubber.scrub(path): _dir_entry(path, names, scrubber) for path, names in self.dirs.items()},
        }
        env = {name: scrubber.scrub(value) for name, value in self.env_read.items() if value is not None}
        if env:
            recording["env"] = env
        return recording

    def save(self, path: str, learn: bool = True) -> None:
        """Scrub what was captured, then write it through the inner host.

        With learn, the scrubber also learns this machine's hostname, accounts
        and saved networks (reading them through this recorder); without it,
        only its patterns apply.
        """
        scrubber = Scrubber.for_host(self) if learn else Scrubber()
        self.inner.write_file(path, json.dumps(self.recording(scrubber), indent=2, ensure_ascii=False) + "\n")


def _dir_entry(path: str, names: list[str] | None, scrubber: Scrubber) -> list[str] | None:
    if names is None:
        return None
    if path == HOME_DIR:  # account names, however short
        return ["<user>" if not name.startswith(".") else name for name in sorted(names)]
    return sorted(scrubber.scrub(name) for name in names)


def _file_entry(path: str, data: bytes | None, scrubber: Scrubber) -> dict[str, Any] | None:
    if data is None:
        return None
    if path == HOSTNAME_PATH:  # the hostname, however short
        return {"text": "<hostname>\n"}
    if path.endswith(SERIAL_FILES):  # a sysfs serial (/sys/bus/usb/devices/1-1/serial), however it looks
        return {"text": "<serial>\n"}
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        if path.startswith(RECORDABLE_BINARY_PREFIXES):
            return {"base64": base64.b64encode(data).decode("ascii")}
        return {"redacted_bytes": len(data)}
    return {"text": scrubber.scrub(text)}
