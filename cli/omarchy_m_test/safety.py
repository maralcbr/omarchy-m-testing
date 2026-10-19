"""What the tool never does: reboot, touch disk encryption, touch boot files.

Every run goes through a Guarded host (app.py wraps the host it's given), so
the rule holds whatever a check asks for. A command it refuses never reaches
the machine: the check gets exit status 126 and the reason on stderr. A file
write or removal it refuses raises PermissionError.

Refused, after unwrapping sudo, env, timeout and similar launchers (and in
`sh -c` scripts, by command name):

  - rebooting or powering off: reboot, poweroff, shutdown, halt, kexec,
    anything named like them (omarchy-system-reboot), systemctl's
    reboot/poweroff/halt/kexec/isolate/hibernate verbs and targets, logind's
    Reboot, PowerOff, Halt and KExec methods over D-Bus, rtcwake -m off/disk;
  - putting the Mac to sleep: only the human does that, by closing the lid
    when the Sleep section asks (sleep.py). Refused: systemctl and loginctl
    suspend/sleep verbs and targets, logind's Suspend, Hibernate and Sleep
    methods over D-Bus, rtcwake (other than -m show), anything named like
    suspend, and writes to /sys/power/state;
  - disk encryption: cryptsetup other than status/isLuks, systemd-cryptenroll,
    clevis, anything with "luks" in its name;
  - boot files and disks: initramfs, UKI and boot loader tools (mkinitcpio,
    dracut, kernel-install, ukify, limine*, grub*, m1n1/u-boot updaters,
    efibootmgr changes, bootctl other than status/list), partitioning and
    formatting tools and dd, and file tools (rm, cp, mv, tee, sed, ...) given
    a path under PROTECTED_PATHS;
  - package changes that would rewrite the kernel or boot files: pacman
    -U, system upgrades (-Sy, -Su), and installing or removing a package
    that is_boot_package() names (a kernel, firmware, m1n1 or aura, boot loaders,
    initramfs tools: their hooks rewrite /boot).

Writes are refused under PROTECTED_PATHS, to /proc/sysrq-trigger, /sys/power's sleep files and to
devices (/dev, except the standard streams), whether through the host's
write_file, a file tool's argument or a `sh -c` redirection. Bundled
scripts (run_bundled) are the tool's own, reviewed with it, and not
inspected command by command.
"""

from __future__ import annotations

import fnmatch
import posixpath
import re
from typing import Sequence

from .host import CommandResult, Host

REFUSED_STATUS = 126

PROTECTED_PATHS = (
    "/boot", "/efi", "/etc/crypttab", "/etc/kernel", "/etc/mkinitcpio.conf", "/etc/mkinitcpio.d",
    "/etc/default/limine", "/etc/limine-entry-tool.conf", "/etc/default/grub", "/etc/fstab",
    "/sys/firmware/efi", "/usr/lib/modules", "/usr/lib/firmware", "/lib/modules", "/lib/firmware",
)
WRITABLE_DEVICES = ("/dev/stdout", "/dev/stderr", "/dev/null", "/dev/tty")

# Package names whose install or removal rewrites the kernel, initramfs or boot loader.
BOOT_PACKAGE_PATTERNS = (
    "linux", "linux-*", "*-dkms", "*firmware*", "mkinitcpio*", "dracut*", "booster", "systemd-ukify",
    "m1n1*", "aura*", "uboot*", "u-boot*", "limine*", "grub*", "efibootmgr", "asahi-*boot*", "omarchy-mac-boot",
    "cryptsetup", "systemd", "systemd-libs", "lvm2", "btrfs-progs", "snapper", "snap-pac",
)

_LAUNCHERS = {
    "sudo", "doas", "pkexec", "run0", "env", "nice", "ionice", "nohup", "setsid", "timeout", "stdbuf",
    "systemd-run", "systemd-inhibit", "unbuffer", "time", "command", "exec", "xargs", "chrt", "taskset", "flock",
}
# Launcher options that take the next argument as their value.
_VALUE_OPTIONS = {
    "sudo": {"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U", "-T", "--user", "--group", "--prompt", "--chdir"},
    "doas": {"-u", "-C"},
    "run0": {"-u", "-g", "-D", "--user", "--group", "--chdir"},
    "env": {"-u", "-C", "-S", "--unset", "--chdir"},
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p", "--class", "--classdata"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "systemd-run": {"-p", "-u", "-E", "--property", "--unit", "--setenv", "--uid", "--gid", "--description", "--slice"},
    "systemd-inhibit": {"--what", "--who", "--why", "--mode"},
    "chrt": {"-p"},
    "flock": {"-w", "-E"},
}
_SHELLS = {"sh", "bash", "zsh", "dash", "fish", "ash"}

_POWER_WORDS = ("reboot", "poweroff", "shutdown", "kexec")
_POWER_PROGRAMS = {"halt", "telinit", "init", "systemd-reboot"}
# Hibernation powers the Mac off and boots it again to resume.
_SYSTEMCTL_POWER = {
    "reboot", "poweroff", "halt", "kexec", "soft-reboot", "isolate", "rescue", "emergency", "default", "exit",
    "hibernate", "hybrid-sleep", "suspend-then-hibernate",
}
_POWER_TARGETS = re.compile(r"^(reboot|poweroff|halt|kexec|shutdown|soft-reboot|rescue|emergency|hibernate|hybrid-sleep|suspend-then-hibernate)\.target$")
_RTCWAKE_OFF = {"off", "disk", "no"}
_SYSTEMCTL_SLEEP = {"suspend", "sleep"}
_SLEEP_TARGETS = re.compile(r"^(suspend|sleep)\.target$")
_LOGIND_SLEEP = re.compile(r"(^|\.)(Suspend|Hibernate|HybridSleep|SuspendThenHibernate|Sleep)(WithFlags)?$")
_SUSPEND_PROGRAM = re.compile(r"[\w.+-]*suspend[\w.+-]*")  # a command's name, not a word in a script's text
_SLEEP_FILES = ("/sys/power/state", "/sys/power/disk", "/sys/power/mem_sleep")
_SLEEPS = "it never puts the Mac to sleep itself: you do, by closing the lid when asked"
_DBUS_TOOLS = {"busctl", "dbus-send", "gdbus", "qdbus", "dbus-daemon"}
_LOGIND_POWER = re.compile(r"(Reboot|PowerOff|Halt|KExec|SoftReboot|ScheduleShutdown)")
_BOOT_TOOLS = {
    "mkinitcpio", "dracut", "kernel-install", "ukify", "installkernel", "update-initramfs", "mkinitramfs",
    "booster", "sbctl", "efibootmgr", "bootctl", "update-grub", "sbsign", "fwupdmgr", "fwupdtool",
    "systemd-cryptenroll", "systemd-cryptsetup", "cryptsetup", "cryptsetup-reencrypt",
    "dd", "wipefs", "fdisk", "sfdisk", "cfdisk", "gdisk", "sgdisk", "cgdisk", "parted", "partprobe",
    "blkdiscard", "mkswap", "tune2fs", "resize2fs", "btrfstune", "losetup",
}
_BOOT_WORDS = ("limine", "grub", "m1n1", "uboot", "u-boot", "initramfs", "initcpio", "luks", "clevis", "mkfs", "asahi-fwupdate")
_READ_ONLY_VERBS = {"cryptsetup": {"status", "isLuks"}, "bootctl": {"status", "list", "is-installed"}}
_FILE_TOOLS = {
    "rm", "cp", "mv", "tee", "install", "ln", "truncate", "chmod", "chown", "chgrp", "chattr", "touch", "mkdir",
    "rmdir", "rsync", "shred", "sed", "mount", "umount", "tar", "unzip", "patch", "ed", "perl", "python", "python3",
}


def is_boot_package(name: str) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in BOOT_PACKAGE_PATTERNS)


def protected(path: str) -> bool:
    path = posixpath.normpath(path) if path.startswith("/") else path
    return any(path == p or path.startswith(p + "/") or (p.endswith(".conf") and path.startswith(p)) for p in PROTECTED_PATHS)


def refusal(argv: Sequence[str]) -> str | None:
    """Why the tool mustn't run `argv`; None when it may."""
    argv = _unwrap(list(argv))
    if not argv:
        return None
    program = posixpath.basename(argv[0])
    args = argv[1:]

    if program in _SHELLS and "-c" in args[:-1]:
        script = args[args.index("-c") + 1]
        for target in re.findall(r">>?\s*([^\s;&|()]+)", script):
            reason = write_refusal(target)
            if reason:
                return reason
        for command in re.split(r"[;&|\n()`]+|\$\(", script):
            reason = refusal(command.split())
            if reason:
                return reason
        return None

    if program in _POWER_PROGRAMS or any(word in program for word in _POWER_WORDS):
        return "it never reboots or powers off the Mac"
    if program in ("systemctl", "loginctl"):
        verbs = [a for a in args if not a.startswith("-")]
        if (verbs and verbs[0] in _SYSTEMCTL_POWER) or any(_POWER_TARGETS.match(a) for a in verbs):
            return "it never reboots or powers off the Mac"
    if program == "rtcwake" and any(a in _RTCWAKE_OFF or a.split("=")[-1] in _RTCWAKE_OFF for a in args):
        return "it never reboots or powers off the Mac"
    if program in _DBUS_TOOLS and any("login1" in a for a in args) and any(_LOGIND_POWER.search(a) for a in args):
        return "it never reboots or powers off the Mac"

    if _SUSPEND_PROGRAM.fullmatch(program):
        return _SLEEPS
    if program in ("systemctl", "loginctl"):
        verbs = [a for a in args if not a.startswith("-")]
        if (verbs and verbs[0] in _SYSTEMCTL_SLEEP) or any(_SLEEP_TARGETS.match(a) for a in verbs):
            return _SLEEPS
    if program == "rtcwake" and not any(a in ("show", "--list-modes") or a.split("=")[-1] == "show" for a in args):
        return _SLEEPS
    if program in _DBUS_TOOLS and any("login1" in a for a in args) and any(_LOGIND_SLEEP.search(a) for a in args):
        return _SLEEPS

    if program in _READ_ONLY_VERBS:
        verbs = [a for a in args if not a.startswith("-")]
        if verbs and verbs[0] in _READ_ONLY_VERBS[program]:
            return None
    if program == "efibootmgr" and all(a in ("-v", "--verbose") for a in args):
        return None
    if program in _BOOT_TOOLS or any(word in program for word in _BOOT_WORDS):
        return "it never touches disk encryption, boot files or disks"

    if program == "pacman":
        return _pacman(args)

    if program in _FILE_TOOLS:
        for arg in args:
            reason = write_refusal(arg.split("=", 1)[-1]) if "/" in arg else None
            if reason:
                return reason
    return None


def _pacman(args: list[str]) -> str | None:
    short = "".join(a[1:] for a in args if a.startswith("-") and not a.startswith("--"))
    long = {a for a in args if a.startswith("--")}
    targets = [a for a in args if not a.startswith("-")]
    if "U" in short or "--upgrade" in long:
        return "it only installs packages from the repositories, never package files"
    if "S" in short or "--sync" in long:
        if "y" in short or "u" in short or long & {"--refresh", "--sysupgrade"}:
            return "it never updates the system (that rewrites the kernel and boot files)"
        if set(short) & set("psilg") or long & {"--print", "--search", "--info", "--list", "--groups"}:
            return None  # only reads the repositories
    if "S" in short or "R" in short or long & {"--sync", "--remove"}:
        boot = [t for t in targets if is_boot_package(t)]
        if boot:
            return f"it never installs or removes kernel, firmware or boot packages ({', '.join(boot)})"
    return None


def _unwrap(argv: list[str]) -> list[str]:
    """The command a launcher chain (sudo -n env LANG=C timeout 5 ...) ends up running."""
    while argv and posixpath.basename(argv[0]) in _LAUNCHERS:
        launcher = posixpath.basename(argv[0])
        rest = argv[1:]
        values = _VALUE_OPTIONS.get(launcher, set())
        while rest:
            arg = rest[0]
            if arg == "--":
                rest = rest[1:]
                break
            if arg in values:
                rest = rest[2:]
            elif arg.startswith("-") or (launcher == "env" and "=" in arg):
                rest = rest[1:]
            elif launcher == "timeout" and re.fullmatch(r"[0-9.]+[smhd]?", arg):
                rest = rest[1:]
                break
            else:
                break
        argv = rest
    return argv


def write_refusal(path: str) -> str | None:
    if path.startswith("/") and posixpath.normpath(path) in _SLEEP_FILES:
        return _SLEEPS
    if protected(path) or path == "/proc/sysrq-trigger":
        return "it never touches disk encryption, boot files or disks"
    if path.startswith("/dev/") and not (path in WRITABLE_DEVICES or path.startswith("/dev/fd/")):
        return "it never writes to devices"
    return None


class Guarded:
    """A host that refuses what the tool must never do, and passes everything else through."""

    def __init__(self, inner: Host):
        self.inner = inner

    def run(self, argv: Sequence[str]) -> CommandResult:
        reason = refusal(argv)
        if reason:
            return _refused(argv, reason)
        return self.inner.run(argv)

    def run_tty(self, argv: Sequence[str], env: dict[str, str] | None = None) -> CommandResult:
        reason = refusal(argv)
        if reason:
            return _refused(argv, reason)
        return self.inner.run_tty(argv, env)

    def write_file(self, path: str, text: str, private: bool = False) -> None:
        reason = write_refusal(path)
        if reason:
            raise PermissionError(f"omarchy-m-test won't write {path}: {reason}")
        self.inner.write_file(path, text, private)

    def remove_file(self, path: str) -> None:
        reason = write_refusal(path)
        if reason:
            raise PermissionError(f"omarchy-m-test won't remove {path}: {reason}")
        self.inner.remove_file(path)

    def __getattr__(self, name: str):
        return getattr(self.inner, name)


def _refused(argv: Sequence[str], reason: str) -> CommandResult:
    return CommandResult(REFUSED_STATUS, "", f"omarchy-m-test refused to run {' '.join(argv)}: {reason}\n")
