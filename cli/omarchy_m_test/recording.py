"""Recordings: capture what a real machine answers at the host boundary, and replay it.

A recording is a JSON file (recording_version 1, or 2 when it holds a sequence; see below):

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
    "regular_files": {"/some/dir": ["a.lua"]},
    "env": {"HOME": "/home/<user>"}
  }

A bundled script's run (Host.run_bundled) is a command whose argv starts with
"bundled:<name>", e.g. ["bundled:mac-check"]. A command the host stopped at its
time limit carries "timed_out": the limit in seconds (CommandResult.timed_out).

Monitor intent is recorded as ["read:monitor-intent"] with per-output policy
flags. User configuration stays local. EDIDs retain only an anonymous
preferred timing block; their identity fields and other descriptors are zeroed.

A file or directory mapped to null is recorded as absent (FileNotFoundError).
A file that was there but couldn't be read is {"error": "permission"} (PermissionError),
{"error": "timeout"} (TimeoutError) or {"error": "unreadable"} (OSError): the
kind only, never a message. A directory that couldn't be listed is recorded the same way.

Successive observations: when a command, file or directory answered
differently the next time it was asked for (a display that settled), each
answer is kept in order. A command appears once per answer in "commands"; a
file or directory maps to a list of its answers. Replay gives the nth request
the nth answer, then repeats the last one. Repeats of the last answer are not
saved, so a recording with no sequence is version 1 exactly as before; one
with a sequence is version 2.

What record mode keeps of the native-mode check's sources is an allowlisted
projection (external_display.py): hyprctl monitors keeps output names, modes,
transform, scale, the disabled/DPMS/mirror flags and ids, never make, model,
description or serial; drm_info and debugfs DRM state keep numeric connector
and CRTC ids only; apple-dcp kernel findings become fixed categories with
validated numbers, in the native-mode check's kernel log and wherever else a
kernel log is recorded.
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

from .host import MONITOR_INTENT, TOUCHID_SNAPSHOT, CommandResult, Host, HttpResponse, MachineSignature, NetworkError, SigningError, Terminal, bundled_argv
from .privacy import HOME_DIR, HOSTNAME_PATH, SERIAL_FILES, Scrubber

RECORDING_VERSION = 1
SEQUENCE_VERSION = 2  # a recording that holds successive observations
READ_VERSIONS = (RECORDING_VERSION, SEQUENCE_VERSION)
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

    # how many times each command, file and directory was asked for (successive observations)
    asked: dict[tuple, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        version = self.recording.get("recording_version")
        if version not in READ_VERSIONS:
            raise ValueError(f"unsupported recording_version {version!r}")
        self.answers = list(self.answers)
        self.responses = list(self.responses)
        self.forms = list(self.forms)

    @classmethod
    def load(cls, path: str, **kwargs: Any) -> "RecordedHost":
        with open(path, encoding="utf-8") as f:  # test harness: reads the recording, not the host
            return cls(json.load(f), **kwargs)

    # -- machine -------------------------------------------------------

    def _next(self, key: tuple, answers: list) -> Any:
        """The nth time something is asked for, its nth recorded answer; then the last one again."""
        n = self.asked.get(key, 0)
        self.asked[key] = n + 1
        return answers[min(n, len(answers) - 1)]

    def run(self, argv: Sequence[str]) -> CommandResult:
        argv = list(argv)
        self.commands_run.append(argv)
        answers = [entry for entry in self.recording.get("commands", []) if entry["argv"] == argv]
        if not answers:
            raise RecordingMiss(f"command not in recording: {argv}")
        entry = self._next(("run", *argv), answers)
        return CommandResult(entry["returncode"], entry.get("stdout", ""), entry.get("stderr", ""), entry.get("timed_out", 0))

    def run_bundled(self, name: str, args: Sequence[str] = ()) -> CommandResult:
        return self.run(bundled_argv(name, args))

    def monitor_intent(self, outputs: list[dict]) -> dict[str, dict]:
        return json.loads(self.run(MONITOR_INTENT).stdout)

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
        if isinstance(entry, list):
            entry = self._next(("file", path), entry)
        if entry is None:
            raise FileNotFoundError(path)
        if "error" in entry:
            raise READ_ERRORS.get(entry["error"], OSError)(path)
        if "text" in entry:
            return entry["text"].encode("utf-8")
        if "redacted_bytes" in entry:
            return bytes(entry["redacted_bytes"])
        return base64.b64decode(entry["base64"])

    def list_dir(self, path: str) -> list[str]:
        dirs = self.recording.get("dirs", {})
        if path not in dirs:
            raise RecordingMiss(f"directory not in recording: {path}")
        names = dirs[path]
        if _is_sequence(names):
            names = self._next(("dir", path), names)
        if names is None:
            raise FileNotFoundError(path)
        if isinstance(names, dict):
            raise READ_ERRORS.get(names.get("error"), OSError)(path)
        return sorted(names)

    def regular_files(self, path: str) -> list[str]:
        """From "regular_files" when the recording has it for path, else every name "dirs" lists."""
        if path in self.recording.get("regular_files", {}):
            names = self.recording["regular_files"][path]
            if _is_sequence(names):
                names = self._next(("regular", path), names)
            if names is None:
                raise FileNotFoundError(path)
            if isinstance(names, dict):
                raise READ_ERRORS.get(names.get("error"), OSError)(path)
            return sorted(names)
        return self.list_dir(path)

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
        self.commands: list[dict[str, Any]] = []  # every answer, in order (repeats dropped when saved)
        self.files: dict[str, list[Any]] = {}  # path -> its answers in order: bytes, None (absent) or an error kind
        self.dirs: dict[str, list[Any]] = {}
        self.regular: dict[str, list[Any]] = {}
        self.env_read: dict[str, str | None] = {}

    # -- machine (recorded) ---------------------------------------------

    def run(self, argv: Sequence[str]) -> CommandResult:
        argv = list(argv)
        result = self.inner.run(argv)
        self._keep(argv, result)
        return result

    def touchid_snapshot(self) -> dict:
        found = self.inner.touchid_snapshot()
        self._keep(TOUCHID_SNAPSHOT, CommandResult(0, json.dumps(found, sort_keys=True), ""))
        return found

    def run_bundled(self, name: str, args: Sequence[str] = ()) -> CommandResult:
        argv = bundled_argv(name, args)
        result = self.inner.run_bundled(name, args)
        self._keep(argv, result)
        return result

    def monitor_intent(self, outputs: list[dict]) -> dict[str, dict]:
        result = self.inner.monitor_intent(outputs)
        self._keep(MONITOR_INTENT, CommandResult(0, json.dumps(result), ""))
        return result

    def _keep(self, argv: list[str], result: CommandResult) -> None:
        entry = {"argv": argv, "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
        if result.timed_out:
            entry["timed_out"] = result.timed_out
        self.commands.append(entry)

    def read_file(self, path: str) -> bytes:
        try:
            data = self.inner.read_file(path)
        except FileNotFoundError:
            self.files.setdefault(path, []).append(None)
            raise
        except OSError as problem:
            self.files.setdefault(path, []).append(ReadError(_error_kind(problem)))
            raise
        self.files.setdefault(path, []).append(data)
        return data

    def list_dir(self, path: str) -> list[str]:
        try:
            names = self.inner.list_dir(path)
        except FileNotFoundError:
            self.dirs.setdefault(path, []).append(None)
            raise
        except OSError as problem:
            self.dirs.setdefault(path, []).append(ReadError(_error_kind(problem)))
            raise
        self.dirs.setdefault(path, []).append(list(names))
        return names

    def regular_files(self, path: str) -> list[str]:
        try:
            names = self.inner.regular_files(path)
        except FileNotFoundError:
            self.regular.setdefault(path, []).append(None)
            raise
        except OSError as problem:
            self.regular.setdefault(path, []).append(ReadError(_error_kind(problem)))
            raise
        self.regular.setdefault(path, []).append(list(names))
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
        # A serial or device name one output names is removed from all of them: learnt from what the machine
        # answered, before projection drops the field that named it (hyprctl's "serial").
        for entry in self.commands:
            scrubber.learn(entry["stdout"] + "\n" + entry["stderr"])
        for answers in self.files.values():
            for data in answers:
                if isinstance(data, bytes):
                    scrubber.learn(data.decode("utf-8", "replace"))
        commands = [_projected(entry) for entry in _without_repeats(self.commands)]
        files = {path: [_file_projected(path, data) for data in answers] for path, answers in self.files.items()}
        saved_files = {scrubber.scrub(path): _answers([_file_entry(path, data, scrubber) for data in answers])
                       for path, answers in files.items()}
        saved_dirs = {scrubber.scrub(path): _answers([_dir_entry(path, names, scrubber) for names in answers])
                      for path, answers in self.dirs.items()}
        saved_regular = {scrubber.scrub(path): _answers([_dir_entry(path, names, scrubber) for names in answers])
                         for path, answers in self.regular.items()}
        sequence = (len({json.dumps(entry["argv"]) for entry in commands}) < len(commands)
                    or any(isinstance(entry, list) for entry in saved_files.values())
                    or any(_is_sequence(names) for names in [*saved_dirs.values(), *saved_regular.values()]))
        recording = {
            "recording_version": SEQUENCE_VERSION if sequence else RECORDING_VERSION,
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
                for entry in commands
            ],
            "files": saved_files,
            "dirs": saved_dirs,
        }
        if saved_regular:
            recording["regular_files"] = saved_regular
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


@dataclass(frozen=True)
class ReadError:
    """A file that was there but couldn't be read: only the kind is kept."""
    kind: str


READ_ERRORS: dict[str, type[OSError]] = {"permission": PermissionError, "timeout": TimeoutError, "unreadable": OSError}


def _error_kind(problem: OSError) -> str:
    if isinstance(problem, PermissionError):
        return "permission"
    if isinstance(problem, TimeoutError):
        return "timeout"
    return "unreadable"


def _without_repeats(commands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each command's answers in order, without the repeats of its last answer (replay repeats it anyway)."""
    by_argv: dict[str, list[int]] = {}
    for index, entry in enumerate(commands):
        by_argv.setdefault(json.dumps(entry["argv"]), []).append(index)
    dropped = set()
    for indexes in by_argv.values():
        while len(indexes) > 1 and commands[indexes[-1]] == commands[indexes[-2]]:
            dropped.add(indexes.pop())
    return [entry for index, entry in enumerate(commands) if index not in dropped]


def _answers(answers: list[Any]) -> Any:
    """One answer as itself; several (after dropping repeats of the last) as a list."""
    while len(answers) > 1 and answers[-1] == answers[-2]:
        answers = answers[:-1]
    return answers[0] if len(answers) == 1 else answers


def _is_sequence(names: Any) -> bool:
    """A directory's answers in order (each a listing, None or an error), not one listing of names."""
    return isinstance(names, list) and bool(names) and (names[0] is None or isinstance(names[0], (list, dict)))


def _projected(entry: dict[str, Any]) -> dict[str, Any]:
    from .external_display import recorded_output, recorded_text
    from .safety import _unwrap
    from .touchid import redact_log

    kept = recorded_output(entry["argv"], entry["stdout"], entry["stderr"])
    stdout, stderr = kept if kept is not None else (recorded_text(entry["stdout"]), recorded_text(entry["stderr"]))
    if _unwrap(entry["argv"])[:1] in (["journalctl"], ["dmesg"]):
        stdout, stderr = redact_log(stdout), redact_log(stderr)
    return {**entry, "stdout": stdout, "stderr": stderr}


def _file_projected(path: str, data: Any) -> Any:
    from .external_display import debugfs_projection, is_debugfs_state

    if isinstance(data, bytes) and is_debugfs_state(path):
        return debugfs_projection(data.decode("utf-8", "replace")).encode("utf-8")
    return data


def _dir_entry(path: str, names: Any, scrubber: Scrubber) -> Any:
    if names is None:
        return None
    if isinstance(names, ReadError):
        return {"error": names.kind}
    if path == HOME_DIR:  # account names, however short
        return ["<user>" if not name.startswith(".") else name for name in sorted(names)]
    return sorted(scrubber.scrub(name) for name in names)


def _file_entry(path: str, data: Any, scrubber: Scrubber) -> dict[str, Any] | None:
    if data is None:
        return None
    if isinstance(data, ReadError):
        return {"error": data.kind}
    if path == HOSTNAME_PATH:  # the hostname, however short
        return {"text": "<hostname>\n"}
    if path.endswith(SERIAL_FILES):  # a sysfs serial (/sys/bus/usb/devices/1-1/serial), however it looks
        return {"text": "<serial>\n"}
    if path.startswith("/sys/class/drm/") and path.endswith("/edid"):
        from .external_display import anonymous_edid

        timing = anonymous_edid(data)
        return {"base64": base64.b64encode(timing).decode("ascii")} if timing else {"redacted_bytes": len(data)}
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        if path.startswith(RECORDABLE_BINARY_PREFIXES):
            return {"base64": base64.b64encode(data).decode("ascii")}
        return {"redacted_bytes": len(data)}
    return {"text": scrubber.scrub(text)}
