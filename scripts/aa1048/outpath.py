"""AA-1048 — where the claim-label tools may write. Export, panel, finals, detector rows and R1 report files hold real
tour and agency text (or claim hashes joined to it), and the app repos are public. So a path inside a git work tree is
allowed only when ALL of these hold (review S272):
  - its first folder is a scratch folder (SCRATCH_DIRS: the workspace `.tmp-session/`, ignored by the root repo);
  - git ignores it there (`git check-ignore` WITH the index: a tracked file is never "ignored");
  - git does not track it (`git ls-files --error-unmatch`), so a tracked file is never overwritten.
Any other path in a work tree is refused, an ignored one included: this CIS checkout ignores `scripts/`, `data/`,
`*.jsonl` and most of `docs/`, yet files under them are tracked, and one `git add -f` would publish an ignored file.
A CIS worktree under the root `.tmp-session/wt/` is its own work tree, so a path in it is refused too. A path outside
every work tree (a temp folder, the worker container without git) is fine. Backed up in CI by the golden-set text
guard markers (services/golden_set/text_guard.py).
"""
from __future__ import annotations

import os
import subprocess

SCRATCH_DIRS = (".tmp-session",)


class OutRefused(ValueError):
    """The output path would land in a git work tree outside its ignored scratch folder, or on a tracked file."""


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
    if _git(root, "ls-files", "--error-unmatch", "--", rel).returncode == 0:
        raise OutRefused(f"{target} is tracked by the git work tree {root}: never overwritten with real claim text")
    first = rel.split(os.sep, 1)[0]
    if first in SCRATCH_DIRS and _git(root, "check-ignore", "-q", "--", rel).returncode == 0:
        return target
    raise OutRefused(f"{target} is inside the git work tree {root} and not in its ignored scratch folder "
                     f"({', '.join(SCRATCH_DIRS)}): real claim text never goes into a repo; write to the workspace "
                     ".tmp-session/ folder or a temp folder")


__all__ = ["OutRefused", "SCRATCH_DIRS", "check_out"]
