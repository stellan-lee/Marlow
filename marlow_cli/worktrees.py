"""Git worktree isolation for single-query runs."""

import logging
import os
import shutil
import sys
import uuid
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)


def _report(message: str) -> None:
    print(message, file=sys.stderr)


def _git_repo_root() -> Optional[str]:
    """Return the git repo root for CWD, or None if not in a repo."""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _path_is_within_root(path: Path, root: Path) -> bool:
    """Return True when a resolved path stays within the expected root."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _setup_worktree(repo_root: str = None) -> Optional[Dict[str, str]]:
    """Create an isolated git worktree for this CLI session.

    Returns a dict with worktree metadata on success, None on failure.
    The dict contains: path, branch, repo_root.
    """
    import subprocess

    repo_root = repo_root or _git_repo_root()
    if not repo_root:
        _report("\033[31m✗ --worktree requires being inside a git repository.\033[0m")
        _report("  cd into your project repo first, then run marlow -w")
        return None

    short_id = uuid.uuid4().hex[:8]
    wt_name = f"marlow-{short_id}"
    branch_name = f"marlow/{wt_name}"

    worktrees_dir = Path(repo_root) / ".worktrees"
    worktrees_dir.mkdir(parents=True, exist_ok=True)

    wt_path = worktrees_dir / wt_name

    # Ensure .worktrees/ is in .gitignore
    gitignore = Path(repo_root) / ".gitignore"
    _ignore_entry = ".worktrees/"
    try:
        existing = gitignore.read_text() if gitignore.exists() else ""
        if _ignore_entry not in existing.splitlines():
            with open(gitignore, "a", encoding="utf-8") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                f.write(f"{_ignore_entry}\n")
    except Exception as e:
        logger.debug("Could not update .gitignore: %s", e)

    # Create the worktree
    try:
        result = subprocess.run(
            ["git", "worktree", "add", str(wt_path), "-b", branch_name, "HEAD"],
            capture_output=True, text=True, timeout=30, cwd=repo_root,
        )
        if result.returncode != 0:
            _report(f"\033[31m✗ Failed to create worktree: {result.stderr.strip()}\033[0m")
            return None
    except Exception as e:
        _report(f"\033[31m✗ Failed to create worktree: {e}\033[0m")
        return None

    # Copy files listed in .worktreeinclude (gitignored files the agent needs)
    include_file = Path(repo_root) / ".worktreeinclude"
    if include_file.exists():
        try:
            repo_root_resolved = Path(repo_root).resolve()
            wt_path_resolved = wt_path.resolve()
            for line in include_file.read_text().splitlines():
                entry = line.strip()
                if not entry or entry.startswith("#"):
                    continue
                src = Path(repo_root) / entry
                dst = wt_path / entry
                # Prevent path traversal and symlink escapes: both the resolved
                # source and the resolved destination must stay inside their
                # expected roots before any file or symlink operation happens.
                try:
                    src_resolved = src.resolve(strict=False)
                    dst_resolved = dst.resolve(strict=False)
                except (OSError, ValueError):
                    logger.debug("Skipping invalid .worktreeinclude entry: %s", entry)
                    continue
                if not _path_is_within_root(src_resolved, repo_root_resolved):
                    logger.warning("Skipping .worktreeinclude entry outside repo root: %s", entry)
                    continue
                if not _path_is_within_root(dst_resolved, wt_path_resolved):
                    logger.warning("Skipping .worktreeinclude entry that escapes worktree: %s", entry)
                    continue
                if src.is_file():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(src), str(dst))
                elif src.is_dir():
                    # Symlink directories (faster, saves disk). Fall back to a
                    # recursive copy if the filesystem rejects symlinks.
                    if not dst.exists():
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            os.symlink(str(src_resolved), str(dst))
                        except (OSError, NotImplementedError) as _sym_err:
                            logger.info(
                                ".worktreeinclude: symlink failed (%s) — "
                                "falling back to copytree.",
                                _sym_err,
                            )
                            try:
                                shutil.copytree(
                                    str(src_resolved),
                                    str(dst),
                                    symlinks=True,
                                    dirs_exist_ok=False,
                                )
                            except Exception as _copy_err:
                                logger.warning(
                                    ".worktreeinclude: copy fallback "
                                        "also failed for %s -> %s: %s",
                                        src, dst, _copy_err,
                                    )
                            else:
                                raise
        except Exception as e:
            logger.debug("Error copying .worktreeinclude entries: %s", e)

    info = {
        "path": str(wt_path),
        "branch": branch_name,
        "repo_root": repo_root,
    }

    _report(f"\033[32m✓ Worktree created:\033[0m {wt_path}")
    _report(f"  Branch: {branch_name}")

    return info


def _worktree_has_unpushed_commits(worktree_path: str, timeout: int = 10) -> bool:
    """Return whether a worktree has commits not reachable from any remote branch.

    ``git log HEAD --not --remotes`` compares against remote-tracking refs under
    ``refs/remotes/*``. If a repo has no remote-tracking refs yet, there is no
    usable remote baseline to compare against, so treat it as having no
    "unpushed" commits.
    """
    import subprocess

    try:
        remote_refs = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname)", "refs/remotes"],
            capture_output=True, text=True, timeout=timeout, cwd=worktree_path,
        )
        if remote_refs.returncode != 0:
            return True
        if not remote_refs.stdout.strip():
            return False

        result = subprocess.run(
            ["git", "log", "--oneline", "HEAD", "--not", "--remotes"],
            capture_output=True, text=True, timeout=timeout, cwd=worktree_path,
        )
        if result.returncode != 0:
            return True
        return bool(result.stdout.strip())
    except Exception:
        return True


def _cleanup_worktree(info: Dict[str, str]) -> None:
    """Remove a worktree and its branch on exit.

    Preserves the worktree only if it has unpushed commits (real work
    that hasn't been pushed to any remote).  Uncommitted changes alone
    (untracked files, test artifacts) are not enough to keep it — agent
    work lives in commits/PRs, not the working tree.
    """
    import subprocess

    wt_path = info["path"]
    branch = info["branch"]
    repo_root = info["repo_root"]

    if not Path(wt_path).exists():
        return

    has_unpushed = _worktree_has_unpushed_commits(wt_path, timeout=10)

    if has_unpushed:
        _report(f"\n\033[33m⚠ Worktree has unpushed commits, keeping: {wt_path}\033[0m")
        _report(f"  To clean up manually: git worktree remove --force {wt_path}")
        return

    # Remove worktree (even if working tree is dirty — uncommitted
    # changes without unpushed commits are just artifacts)
    try:
        subprocess.run(
            ["git", "worktree", "remove", wt_path, "--force"],
            capture_output=True, text=True, timeout=15, cwd=repo_root,
        )
    except Exception as e:
        logger.debug("Failed to remove worktree: %s", e)

    # Delete the branch
    try:
        subprocess.run(
            ["git", "branch", "-D", branch],
            capture_output=True, text=True, timeout=10, cwd=repo_root,
        )
    except Exception as e:
        logger.debug("Failed to delete branch %s: %s", branch, e)

    _report(f"\033[32m✓ Worktree cleaned up: {wt_path}\033[0m")
