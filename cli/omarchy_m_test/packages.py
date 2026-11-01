"""Temporary test packages: installed without a question (the disclaimer covers them), removed when the section ends.

    ready = packages.temporary(ctx, ["glmark2", "vkmark"], "The GPU benchmarks")
    if ready.skipped:
        return [human.skip(...) or an automatic skip with ready.skipped as the reason]

What happens:

  1. Packages already installed are left alone: they are never installed,
     and never removed.
  2. pacman works out exactly which packages installing the rest brings in,
     dependencies included (`pacman -Sp --needed`), from the repositories as
     they are synced now: the tool never syncs or upgrades (-Sy would be a
     partial upgrade). None of them may be a kernel, firmware or boot
     package (safety.py).
  3. The human is shown that list and it is installed without a question:
     accepting the disclaimer (consent.py) agreed to temporary test
     packages. Nothing else is ever installed this way.
  4. sudo: its cached credentials, or at a terminal `sudo -v`, which asks
     for the password there (the human is at the Mac, running the tool).
     Off a terminal without cached credentials the checks are skipped,
     and so they are for a caller that passes ask=False (nobody is at the
     Mac to type it).
     If any of them is installed already (a dependency needs a newer
     version: an upgrade), nothing is installed.
  5. The restorer (`sudo -n pacman -R --noconfirm` exactly that list, those
     still installed when it runs) is registered, then
     `sudo -n pacman -S --needed --noconfirm --asdeps` runs.
     --asdeps: should removing them ever fail, they are orphans pacman
     offers to clean up, not packages the user seems to have chosen.
  6. What is actually installed afterwards is checked with pacman, and the
     restorer narrowed to exactly that (dropped if nothing was).

Removal runs with the section's other restorers: when it ends, on an error,
on Ctrl-C, or first thing in the next run if this one was killed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from . import safety
from .session import SUDO_ASK, SUDO_CACHED, Context, Restorer
from .ui import Ui


def plan_query(names: Sequence[str]) -> list[str]:
    return ["pacman", "-Sp", "--needed", "--print-format", "%n", *names]


def install_command(names: Sequence[str]) -> list[str]:
    return ["sudo", "-n", "pacman", "-S", "--needed", "--noconfirm", "--asdeps", *names]


REMOVE = ("sudo", "-n", "pacman", "-R", "--noconfirm")  # + the packages still installed (session.py)


@dataclass(frozen=True)
class Temporary:
    skipped: str | None = None               # why the packages aren't there; None when they are
    installed: tuple[str, ...] = ()          # what this run installed (and will remove)
    already: tuple[str, ...] = field(default=())  # what was installed before and stays

    @property
    def ready(self) -> bool:
        return self.skipped is None

    def evidence(self) -> list[str]:
        lines = []
        if self.installed:
            lines.append(f"installed for this run, removed at its end: {' '.join(self.installed)}")
        if self.already:
            lines.append(f"already installed: {' '.join(self.already)}")
        return lines


def installed(ctx: Context, names: Sequence[str]) -> set[str]:
    """Which of `names` pacman has installed."""
    if not names:
        return set()
    answer = ctx.host.run(["pacman", "-Q", *names])
    if answer.returncode == 127:
        return set()
    return {line.split()[0] for line in answer.stdout.splitlines() if line.split() and line.split()[0] in names}


def temporary(ctx: Context, names: Sequence[str], purpose: str, ask: bool = True) -> Temporary:
    """Make sure `names` are installed for this section, with consent; see the module docstring."""
    names = list(dict.fromkeys(names))
    if ctx.system is not None and not ctx.system.has_pacman:
        return Temporary(skipped="needs pacman to install its test packages")
    already = installed(ctx, names)
    missing = [name for name in names if name not in already]
    kept = tuple(name for name in names if name in already)
    if not missing:
        return Temporary(already=kept)

    boot = [name for name in missing if safety.is_boot_package(name)]
    if boot:
        return Temporary(skipped=f"won't install kernel, firmware or boot packages ({' '.join(boot)})", already=kept)
    planned = ctx.host.run(plan_query(missing))
    plan = list(dict.fromkeys(line.strip() for line in planned.stdout.splitlines() if line.strip()))
    if planned.returncode != 0 or not plan:
        problem = (planned.stderr or planned.stdout).strip().splitlines()
        return Temporary(skipped=f"pacman can't install {' '.join(missing)} ({problem[-1] if problem else 'no packages found'})", already=kept)
    boot = [name for name in plan if safety.is_boot_package(name)]
    if boot:
        return Temporary(skipped=f"installing {' '.join(missing)} would bring in boot packages ({' '.join(boot)})", already=kept)
    upgraded = sorted(installed(ctx, plan))
    if upgraded:  # a dependency needs a newer version of something installed: that's an upgrade, not a test package
        return Temporary(skipped=f"installing {' '.join(missing)} would upgrade installed packages ({' '.join(upgraded)}); update the system first",
                         already=kept)

    ui = ctx.ui or Ui(ctx.host)
    extra = [name for name in plan if name not in missing]
    ui.text(
        f"{purpose} needs {'a package that isn' if len(missing) == 1 else 'packages that aren'}'t installed: {' '.join(missing)}"
        + (f" (with {' '.join(extra)})" if extra else "")
        + f". Installing {len(plan)} temporary test package(s) from this system's package repositories with sudo pacman, "
        "for this run only: exactly these are removed when the section ends."
    )

    if not sudo_ready(ctx, ask):
        why = "it wasn't given" if ask else "this unattended run doesn't ask for a password"
        return Temporary(skipped=f"installing test packages needs sudo, and {why}", already=kept)

    restorer = ctx.changes.register(f"the temporary packages {' '.join(plan)}", REMOVE, sudo=True, packages=plan)
    done = ctx.host.run(install_command(missing))
    present = installed(ctx, plan)
    actual = tuple(name for name in plan if name in present)
    ctx.changes.replace(restorer, Restorer(f"the temporary packages {' '.join(actual)}", REMOVE, True, actual) if actual else None)
    if done.returncode != 0 or any(name not in present for name in missing):
        problem = (done.stderr or done.stdout).strip().splitlines()
        return Temporary(skipped=f"pacman couldn't install {' '.join(missing)} ({problem[-1] if problem else done.returncode})",
                         installed=actual, already=kept)
    return Temporary(installed=actual, already=kept)


def sudo_ready(ctx: Context, ask: bool = True) -> bool:
    """sudo works without a prompt now: cached, or the human just typed the password at the terminal (also changes.py)."""
    if ctx.host.run(SUDO_CACHED).returncode == 0:
        return True
    if not ask or ctx.host.terminal() is None:
        return False
    return ctx.host.run_tty(SUDO_ASK).returncode == 0
