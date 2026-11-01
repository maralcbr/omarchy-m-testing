#!/usr/bin/env python3
"""Fail if CLI code talks to the machine, the human or the network outside the host boundary.

Only omarchy_m_test/host.py (the real host), recording.py (the recorded host),
__main__.py (the process entry point) and bundled.py (the tool's own data
files, never the machine's) may do I/O. Everything else must go
through a Host. Also fails on any import outside the standard library.
"""

import ast
import pathlib
import sys

PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "omarchy_m_test"
# The isolated bundled GPU supervisor is an executable host operation, reached
# through Host.run_bundled only, never imported by the CLI's pure modules.
ALLOWED_IO = {"host.py", "recording.py", "__main__.py", "bundled.py", "gpu_native/launcher.py", "gl_spec/glprobe.py"}
IO_MODULES = {
    "os", "subprocess", "socket", "urllib", "http", "shutil", "pathlib", "glob", "tempfile",
    "platform", "getpass", "termios", "tty", "select", "pty", "ctypes", "io", "importlib",
    "builtins", "codecs", "logging", "fileinput", "webbrowser", "sqlite3", "zipfile", "tarfile",
    "mmap", "multiprocessing", "threading", "asyncio", "signal", "ssl", "ftplib", "smtplib",
}
# Only the entry point may build the real host; everything else is handed a Host.
REAL_HOST_ALLOWED = {"host.py", "__main__.py"}
IO_CALLS = {"open", "input", "print", "exec", "eval", "__import__"}
IO_SYS_ATTRS = {"stdin", "stdout", "stderr", "argv", "exit"}


def _identifiers(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Attribute):
        return {node.attr}
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return {alias.name.split(".")[-1] for alias in node.names} | {alias.asname for alias in node.names if alias.asname}
    return set()


def violations(rel: str, tree: ast.AST) -> list[str]:
    found = []
    for node in ast.walk(tree):
        modules = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            modules = [node.module or ""]
        for module in modules:
            root = module.split(".")[0]
            if root not in sys.stdlib_module_names:
                found.append(f"{rel}:{node.lineno}: non-stdlib import {module}")
            elif rel not in ALLOWED_IO and root in IO_MODULES:
                found.append(f"{rel}:{node.lineno}: imports {module}; go through the Host instead")
        if rel not in REAL_HOST_ALLOWED and "RealHost" in _identifiers(node):
            found.append(f"{rel}:{node.lineno}: uses RealHost; take a Host argument instead")
        if rel in ALLOWED_IO:
            continue
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in IO_CALLS:
            found.append(f"{rel}:{node.lineno}: calls {node.func.id}(); go through the Host instead")
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "sys" and node.attr in IO_SYS_ATTRS:
            found.append(f"{rel}:{node.lineno}: uses sys.{node.attr}; go through the Host instead")
    return found


def main() -> int:
    files = sorted(PACKAGE.rglob("*.py"))
    problems = []
    for path in files:
        rel = path.relative_to(PACKAGE).as_posix()
        problems += violations(rel, ast.parse(path.read_text(), str(path)))
    if problems:
        print("Host boundary violations:\n  " + "\n  ".join(problems))
        return 1
    print(f"host boundary ok ({len(files)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
