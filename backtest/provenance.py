"""What code and what configuration produced a backtest.

**"FREEZE BEFORE LAUNCH" WAS A HABIT, AND HABITS FAIL.** Three runs in one
afternoon were invalidated by edits landing mid-walk — each time the numbers
described code that no longer existed, and the only thing that caught it was
remembering. A 75-minute run whose provenance is a memory is a number nobody
can check later, so this records it instead:

  * the git commit the walk ran from,
  * a hash of the EFFECTIVE configuration (the validated models, including
    every default and every sweep override — not the YAML text, which can
    differ while the loaded values do not),
  * and whether the tree was dirty.

A dirty tree REFUSES to run unless `--allow-dirty` is passed, and a run that
uses that flag is stamped `baseline=False` for ever: its output is a
diagnostic, never evidence. The stamp travels on every CSV row rather than in
a side file, because a CSV gets copied, concatenated and mailed around, and
provenance that lives somewhere else is provenance that gets separated from
the thing it describes.

No network, no database. Everything here is `git` and a hash.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime

from core import clock, config

__all__ = ["DirtyTree", "Provenance", "config_hash", "current"]

UNKNOWN = "unknown"


class DirtyTree(RuntimeError):
    """The working tree has uncommitted changes and the run refused to start."""


@dataclass(frozen=True)
class Provenance:
    """The identity of one run. Immutable, and carried on every output row."""

    commit: str
    dirty: bool
    config_hash: str
    allow_dirty: bool
    created_at: datetime
    dirty_files: tuple[str, ...] = ()

    @property
    def baseline(self) -> bool:
        """Whether this run's numbers may be cited as evidence.

        A clean tree at a known commit is reproducible: anyone can check out
        that commit, load that config, and get these numbers back. Anything
        else is a diagnostic — useful while working, never a baseline.
        """
        return not self.dirty and self.commit != UNKNOWN

    @property
    def label(self) -> str:
        state = "clean" if not self.dirty else f"DIRTY ({len(self.dirty_files)} file(s))"
        return f"{self.commit} {state}, config {self.config_hash}"

    def as_row(self) -> dict[str, str]:
        """The columns stamped onto every trade."""
        return {
            "commit": self.commit,
            "config_hash": self.config_hash,
            "dirty": "1" if self.dirty else "0",
        }

    def header(self) -> list[str]:
        lines = [
            f"code      {self.commit}{'  (DIRTY TREE)' if self.dirty else ''}",
            f"config    {self.config_hash}",
            f"run at    {self.created_at:%Y-%m-%d %H:%M:%S %Z}",
        ]
        if not self.baseline:
            lines.append(
                "NON-BASELINE: this run did not come from a clean, committed tree. "
                "Its numbers are a diagnostic and must not be cited as evidence "
                "(D39/D40 count only what is reproducible)."
            )
            if self.dirty_files:
                lines.append(f"            uncommitted: {', '.join(self.dirty_files[:8])}")
        return lines


def _git(*args: str) -> str | None:
    """Raw stdout, NOT stripped.

    `--porcelain` encodes status in the first TWO columns and the first of them
    is often a space (` M path`), so stripping the output removes a character
    from the first line only — which printed `acktest/engine.py` in a refusal
    message and would have sent somebody looking for a file that does not
    exist. Callers strip what they can afford to.
    """
    try:
        done = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def config_hash(*models) -> str:
    """A stable hash of the EFFECTIVE configuration.

    Hashes the validated models rather than the YAML, because that is what the
    code actually read: a comment change must not alter the hash, and a default
    that moved must. Sorted keys so the hash depends on values, not on dict
    ordering.
    """
    payload = models or (config.scoring(), config.risk(), config.universe())
    blob = json.dumps([m.model_dump(mode="json") for m in payload], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def current(*, allow_dirty: bool = False, models=()) -> Provenance:
    """This moment's provenance. Raises `DirtyTree` unless dirt is permitted.

    A repository we cannot read (no git, a tarball deploy) counts as DIRTY: the
    rule is "prove the tree is clean", and an unanswerable question is not a
    yes. That keeps `--allow-dirty` the single, visible way to run without a
    verifiable commit.
    """
    raw_commit = _git("rev-parse", "--short", "HEAD")
    commit = raw_commit.strip() if raw_commit else UNKNOWN
    status = _git("status", "--porcelain")
    if status is None:
        dirty, files = True, ()
    else:
        # Columns 0-1 are the status codes and column 2 is the separator.
        files = tuple(line[3:] for line in status.splitlines() if line.strip())
        dirty = bool(files)

    if dirty and not allow_dirty:
        detail = ", ".join(files[:8]) if files else "git state could not be read"
        raise DirtyTree(
            f"refusing to start: the working tree is not clean ({detail}). A 75-minute "
            f"walk whose code changes underneath it produces numbers that describe "
            f"nothing. Commit, stash, or pass --allow-dirty to run anyway — which "
            f"stamps the output NON-BASELINE."
        )

    return Provenance(
        commit=commit,
        dirty=dirty,
        config_hash=config_hash(*models),
        allow_dirty=allow_dirty,
        created_at=clock.et_now(),
        dirty_files=files,
    )
