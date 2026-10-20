"""Where a project's local Git clone stands against Altium 365, offline.

Altium 365 serves each project as a Git repository at
https://<workspace>.365.altium.com/git/<project GUID>.git, and Altium Designer pulls it
into a local clone. This reads that clone with local git commands only: no fetch, no
network, no credentials. What the clone knows about Altium 365 (its remote-tracking
branch) is as of the last fetch, so ahead and behind are too.

Every git call runs with --no-optional-locks (git status does not rewrite the index),
protocol.allow=never (no command can open a connection, a lazy fetch included), no
prompts and core.fsmonitor off (a repository's config cannot start a program).
"""
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

STALE_FETCH_DAYS = 30
UNCOMMITTED_SAMPLE = 10
# Files the tools read. Altium also leaves files that Altium 365 does not keep in the
# clone (.PrjPcbStructure, release zips, PDFs); changes to those are counted, not warned.
DESIGN_EXTENSIONS = {".prjpcb", ".schdoc", ".pcbdoc", ".pcbdwf", ".annotation"}
GIT_TIMEOUT_S = 10

# Optional user info is accepted and never reported.
A365_REMOTE = re.compile(
    r"^https://(?:[^@/]+@)?(?P<workspace>[A-Za-z0-9-]+)\.365\.altium\.com/git/"
    r"(?P<guid>[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})"
    r"(?:\.git)?/?$"
)

_GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GCM_INTERACTIVE": "never",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_NO_LAZY_FETCH": "1",
}
_GIT_CONFIG = ("-c", "protocol.allow=never", "-c", "core.fsmonitor=false")

# git status --porcelain XY codes, by what changed.
_KINDS = (("conflicted", "U"), ("added", "A"), ("deleted", "D"), ("renamed", "RC"),
          ("modified", "MT"))
_CONFLICTS = {"DD", "AA"}


class GitError(Exception):
    pass


def parse_a365_remote(url: str) -> tuple[str, str] | None:
    """(workspace, project GUID) of an Altium 365 project URL, else None."""
    match = A365_REMOTE.match(url.strip())
    if match is None:
        return None
    return match["workspace"], match["guid"].upper()


def _has_git_dir(path: Path) -> bool:
    return any((folder / ".git").exists() for folder in (path, *path.parents))


def _git(git: str, cwd: Path, *args: str, check: bool = True) -> str | None:
    """stdout of a local git command; None (check False) or GitError when it fails."""
    try:
        result = subprocess.run(  # noqa: S603 - fixed git subcommands, no shell
            [git, "--no-optional-locks", *_GIT_CONFIG, "-C", str(cwd), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=GIT_TIMEOUT_S, stdin=subprocess.DEVNULL, env={**os.environ, **_GIT_ENV},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as error:
        raise GitError(f"git {args[0]} did not answer within {GIT_TIMEOUT_S} s") from error
    except OSError as error:
        raise GitError(f"git could not be started: {error}") from error
    if result.returncode != 0:
        if check:
            first_line = (result.stderr.strip().splitlines() or [""])[0]
            raise GitError(first_line or f"git {args[0]} failed")
        return None
    return result.stdout


def _utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="seconds")


def _refs(git: str, cwd: Path) -> tuple[str | None, str | None, str | None, set[str]]:
    """Current branch, its upstream, origin's default branch and every branch, in one
    call: for-each-ref also answers for a detached or unborn HEAD."""
    out = _git(git, cwd, "for-each-ref",
               "--format=%(HEAD)%09%(refname)%09%(upstream)%09%(symref)",
               "refs/heads", "refs/remotes")
    branch = upstream = default = None
    existing = set()
    for line in out.splitlines():
        current, refname, tracked, symref = (line.split("\t") + ["", "", ""])[:4]
        existing.add(refname)
        if current == "*" and refname.startswith("refs/heads/"):
            branch, upstream = refname.removeprefix("refs/heads/"), tracked or None
        if refname == "refs/remotes/origin/HEAD":
            default = symref or None
    return branch, upstream, default, existing


def _remote_ref(branch: str | None, upstream: str | None, default: str | None,
                existing: set[str]) -> str | None:
    """Full name of the remote-tracking branch HEAD is compared with: the branch's
    upstream, else origin/<branch>, else origin's default branch."""
    candidates = [upstream if upstream and upstream.startswith("refs/remotes/") else None,
                  f"refs/remotes/origin/{branch}" if branch else None, default]
    return next((ref for ref in candidates if ref and ref in existing), None)


def _git_path(git: str, cwd: Path, name: str) -> Path:
    return _absolute(cwd, _git(git, cwd, "rev-parse", "--git-path", name).strip())


def _absolute(cwd: Path, path: str) -> Path:
    return Path(path) if Path(path).is_absolute() else cwd / path


def _last_fetch(git: str, cwd: Path, fetch_head: Path,
                remote_ref: str | None) -> tuple[float | None, str | None]:
    """Time of the last fetch: FETCH_HEAD's mtime, else the newest entry of the
    remote-tracking branch's reflog (the clone itself writes one)."""
    if fetch_head.is_file():
        return fetch_head.stat().st_mtime, "FETCH_HEAD"
    if remote_ref:
        log = _git_path(git, cwd, f"logs/{remote_ref}")
        try:
            last = log.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-1]
            # "<old> <new> <name> <email> <unix time> <tz>\t<message>"
            return float(last.split("\t", 1)[0].split()[-2]), "remote_branch_reflog"
        except (OSError, IndexError, ValueError):
            pass
    return None, None


def _uncommitted(git: str, cwd: Path) -> dict:
    """Uncommitted changes: counts per kind, how many are design files the tools read,
    and a sample of paths, design files first."""
    out = _git(git, cwd, "status", "--porcelain", "-z", "--untracked-files=normal")
    entries = out.split("\0")
    counts: dict[str, int] = {}
    paths: list[str] = []
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        if code == "??":
            kind = "untracked"
        elif code in _CONFLICTS:
            kind = "conflicted"
        else:
            kind = next((name for name, letters in _KINDS
                         if code[0] in letters or code[1] in letters), "modified")
        if "R" in code or "C" in code:
            i += 1  # the rename's source path follows
        counts[kind] = counts.get(kind, 0) + 1
        paths.append(path)
    design = [path for path in paths if Path(path).suffix.lower() in DESIGN_EXTENSIONS]
    others = [path for path in paths if Path(path).suffix.lower() not in DESIGN_EXTENSIONS]
    return {"count": len(paths), "by_kind": counts, "design_files": len(design),
            "sample": (design + others)[:UNCOMMITTED_SAMPLE]}


def _commit(git: str, cwd: Path, rev: str) -> dict | None:
    out = _git(git, cwd, "log", "-1", "--format=%h%x1f%ct%x1f%s", rev, "--", check=False)
    if not out or not out.strip():
        return None
    short, timestamp, subject = out.rstrip("\n").split("\x1f", 2)
    return {"commit": short, "date_utc": _utc(float(timestamp)), "message": subject}


def clone_status(project_dir: str | Path, now: datetime | None = None,
                 stale_after_days: int = STALE_FETCH_DAYS, git: str | None = None) -> dict:
    """Where the Git clone holding project_dir stands against its Altium 365 project.

    altium365 is False with a reason when the folder is not in a Git work tree whose
    origin is an Altium 365 project; error and message when git is missing or fails.
    """
    cwd = Path(project_dir)
    git = git or shutil.which("git")
    if git is None:
        if _has_git_dir(cwd):
            return {"error": "git_not_found",
                    "message": "The project is in a Git clone, but git is not installed or "
                               "not on PATH, so its Altium 365 status cannot be read."}
        return {"altium365": False, "reason": "not_a_git_repository"}
    try:
        top, fetch_head = _git(git, cwd, "rev-parse", "--show-toplevel",
                               "--git-path", "FETCH_HEAD").splitlines()[:2]
    except GitError as error:
        if not _has_git_dir(cwd):
            return {"altium365": False, "reason": "not_a_git_repository"}
        return {"error": "git_failed", "message": str(error)}
    url = _git(git, cwd, "config", "--get", "remote.origin.url", check=False) or ""
    remote = parse_a365_remote(url)
    if remote is None:
        return {"altium365": False, "reason": "origin_is_not_altium365"}
    workspace, guid = remote
    try:
        return _a365_status(git, cwd, Path(top), _absolute(cwd, fetch_head), workspace,
                            guid, now or datetime.now(timezone.utc), stale_after_days)
    except GitError as error:
        return {"altium365": True, "workspace": workspace, "project_guid": guid,
                "error": "git_failed", "message": str(error)}


def _a365_status(git: str, cwd: Path, top: Path, fetch_head: Path, workspace: str,
                 guid: str, now: datetime, stale_after_days: int) -> dict:
    branch, upstream, default, existing = _refs(git, cwd)
    remote_ref = _remote_ref(branch, upstream, default, existing)
    fetched, fetch_source = _last_fetch(git, cwd, fetch_head, remote_ref)
    changes = _uncommitted(git, cwd)
    status: dict = {
        "altium365": True,
        "workspace": workspace,
        "project_guid": guid,
        "branch": branch,
        "remote_branch": remote_ref.removeprefix("refs/remotes/") if remote_ref else None,
        "last_fetch_utc": _utc(fetched) if fetched is not None else None,
        "days_since_fetch": (
            max(0, int((now.timestamp() - fetched) // 86400)) if fetched is not None else None
        ),
        "ahead": None,
        "behind": None,
        "uncommitted_count": changes["count"],
    }
    if fetch_source != "FETCH_HEAD":
        status["last_fetch_source"] = fetch_source
    if changes["count"]:
        status["uncommitted"] = changes["by_kind"]
        status["uncommitted_design_files"] = changes["design_files"]
        status["uncommitted_sample"] = changes["sample"]
    try:
        subfolder = cwd.resolve().relative_to(top.resolve())
    except ValueError:
        subfolder = None
    if subfolder is not None and subfolder.parts:
        status["project_subfolder"] = subfolder.as_posix()
    status["last_commit"] = _commit(git, cwd, "HEAD")
    if remote_ref:
        counts = _git(git, cwd, "rev-list", "--left-right", "--count", f"HEAD...{remote_ref}",
                      check=False)
        if counts:
            ahead, behind = (int(n) for n in counts.split())
            status["ahead"], status["behind"] = ahead, behind
            if behind:
                status["remote_last_commit"] = _commit(git, cwd, remote_ref)
    status["warnings"] = _warnings(status, stale_after_days)
    status["note"] = (
        "Read offline from the local clone; nothing was fetched. ahead and behind compare "
        "the local commit with what Altium 365 had at the last fetch."
    )
    return status


def _warnings(status: dict, stale_after_days: int) -> list[dict]:
    warnings = []
    days = status["days_since_fetch"]
    if days is None:
        warnings.append({"warning": "fetch_time_unknown",
                         "message": "When this copy was last updated from Altium 365 is "
                                    "unknown; it may be behind the workspace."})
    elif days > stale_after_days:
        warnings.append({"warning": "fetch_stale",
                         "message": f"Last updated from Altium 365 {days} days ago: the "
                                    "workspace may have newer revisions. Update the project "
                                    "from the server in Altium Designer to check."})
    if status["remote_branch"] is None:
        warnings.append({"warning": "no_remote_branch",
                         "message": "The clone has no branch from Altium 365 to compare with, "
                                    "so ahead and behind are unknown."})
    if status["behind"]:
        warnings.append({"warning": "behind",
                         "message": f"At the last fetch Altium 365 had {status['behind']} "
                                    "commit(s) not in this copy: the files read here are an "
                                    "older revision."})
    if status["ahead"]:
        warnings.append({"warning": "ahead",
                         "message": f"{status['ahead']} local commit(s) not saved to "
                                    "Altium 365 (as of the last fetch)."})
    if status.get("uncommitted_design_files"):
        warnings.append({"warning": "uncommitted_changes",
                         "message": f"{status['uncommitted_design_files']} design file(s) "
                                    "(.PrjPcb, .SchDoc, .PcbDoc, .PCBDwf, .Annotation) differ "
                                    "from the last commit: the tools read these local files, "
                                    "not the revision in Altium 365."})
    return warnings
