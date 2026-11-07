"""AA-1048 — where the claim-label tools may write. Export, panel, finals, detector rows and R1 report files hold real
tour and agency text (or claim hashes joined to it); this repo is public. An output path inside a git work tree is
refused unless git ignores it there (the workspace scratch folder `.tmp-session/` is ignored by the root repo; a path
in this checkout or in a worktree is not). A path outside every work tree (a temp folder, the worker container) is
fine. Backed up in CI by the golden-set text guard markers (services/golden_set/text_guard.py).
"""
from __future__ import annotations

import os
import subprocess


class OutRefused(ValueError):
    """The output path would land in a git work tree where git does not ignore it."""


def _git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, check=False)


def check_out(path: str) -> str:
    """The absolute path when it is safe to write, else OutRefused."""
    target = os.path.abspath(path)
    folder = os.path.dirname(target)
    while folder and not os.path.isdir(folder):
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        folder = parent
    try:
        top = _git(folder, "rev-parse", "--show-toplevel")
    except OSError:                                      # no git at all (a container): not in a work tree
        return target
    if top.returncode != 0 or not top.stdout.strip():
        return target
    root = top.stdout.strip()
    rel = os.path.relpath(os.path.join(os.path.realpath(folder), os.path.relpath(target, folder)),
                          os.path.realpath(root))
    if _git(root, "check-ignore", "-q", "--no-index", rel).returncode == 0:
        return target
    raise OutRefused(f"{target} is inside the git work tree {root} and not ignored there: real claim text never goes "
                     "into a repo; write to the workspace .tmp-session/ folder or a temp folder")


__all__ = ["OutRefused", "check_out"]
