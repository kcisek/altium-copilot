"""Altium 365 clone status: read offline from temporary Git repositories whose origin is a
made-up Altium 365 URL. Nothing here fetches or contacts a server."""
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import server.main as main
import services.clone_status as clone_module
from altium import AltiumClient
from services.clone_status import clone_status, parse_a365_remote

GUID = "0F1E2D3C-4B5A-6978-8796-A5B4C3D2E1F0"
URL = f"https://example-ws.365.altium.com/git/{GUID}.git"
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


@pytest.fixture(autouse=True)
def isolated_git(tmp_path, monkeypatch):
    """No user or system git config, fixed identities."""
    empty = tmp_path / "empty.gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "test@example.com")


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout


def commit(root: Path, name: str, content: str, message: str) -> None:
    (root / name).write_text(content, encoding="utf-8")
    git(root, "add", name)
    git(root, "commit", "-q", "-m", message)


def make_clone(root: Path, url: str | None = URL) -> Path:
    """A clone of a project: two commits, origin/master at HEAD, upstream set."""
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "master")
    commit(root, "Board.PrjPcb", "[Design]\n[Document1]\nDocumentPath=Board.SchDoc\n",
           "Initial import")
    commit(root, "Board.SchDoc", "v1", "Add power sheet")
    if url is not None:
        git(root, "remote", "add", "origin", url)
        git(root, "update-ref", "refs/remotes/origin/master", "HEAD")
        git(root, "branch", "-q", "--set-upstream-to=origin/master")
    return root


def fetched(root: Path, days_ago: float, now: datetime = NOW) -> None:
    fetch_head = root / ".git" / "FETCH_HEAD"
    fetch_head.write_text(f"{git(root, 'rev-parse', 'HEAD').strip()}\t\tbranch 'master'\n",
                          encoding="utf-8")
    when = (now - timedelta(days=days_ago)).timestamp()
    os.utime(fetch_head, (when, when))


@pytest.fixture
def clone(tmp_path):
    root = make_clone(tmp_path / "Board")
    fetched(root, 2)
    return root


def warnings_of(status: dict) -> list[str]:
    return [w["warning"] for w in status["warnings"]]


# ---------- remote URL ----------

@pytest.mark.parametrize("url", [
    URL,
    URL.removesuffix(".git"),
    URL + "/",
    URL.lower(),
    URL.replace("example-ws.365.altium.com", "Example-WS.365.Altium.com"),
    f"https://someone:secret@example-ws.365.altium.com/git/{GUID}.git",
])
def test_altium365_remote_urls(url):
    assert parse_a365_remote(url) == ("example-ws", GUID)


@pytest.mark.parametrize("url", [
    "https://github.com/example/board.git",
    f"http://example-ws.365.altium.com/git/{GUID}.git",
    f"https://example-ws.365.altium.com.example.org/git/{GUID}.git",
    "https://example-ws.365.altium.com/git/not-a-guid.git",
    f"https://example-ws.365.altium.com/other/{GUID}.git",
    "",
])
def test_other_urls_are_not_altium365(url):
    assert parse_a365_remote(url) is None


# ---------- clone_status ----------

def test_up_to_date_clone(clone):
    status = clone_status(clone, now=NOW)
    assert status["altium365"] is True
    assert status["workspace"] == "example-ws"
    assert status["project_guid"] == GUID
    assert status["branch"] == "master"
    assert status["remote_branch"] == "origin/master"
    assert status["days_since_fetch"] == 2
    assert status["last_fetch_utc"] == (NOW - timedelta(days=2)).isoformat(timespec="seconds")
    assert "last_fetch_source" not in status
    assert (status["ahead"], status["behind"]) == (0, 0)
    assert status["uncommitted_count"] == 0
    assert "uncommitted" not in status
    assert status["last_commit"]["message"] == "Add power sheet"
    assert status["last_commit"]["date_utc"].endswith("+00:00")
    assert "project_subfolder" not in status
    assert status["warnings"] == []
    assert "nothing was fetched" in status["note"]


def test_old_fetch_warns(clone):
    fetched(clone, 45.5)
    status = clone_status(clone, now=NOW)
    assert status["days_since_fetch"] == 45
    assert warnings_of(status) == ["fetch_stale"]
    assert "45 days ago" in status["warnings"][0]["message"]
    assert warnings_of(clone_status(clone, now=NOW, stale_after_days=60)) == []


def test_behind_altium365_as_of_the_last_fetch(clone):
    commit(clone, "Board.SchDoc", "v2", "Rev B values")
    git(clone, "update-ref", "refs/remotes/origin/master", "HEAD")
    git(clone, "reset", "-q", "--hard", "HEAD~1")
    status = clone_status(clone, now=NOW)
    assert (status["ahead"], status["behind"]) == (0, 1)
    assert status["remote_last_commit"]["message"] == "Rev B values"
    assert warnings_of(status) == ["behind"]


def test_ahead_of_altium365(clone):
    commit(clone, "Board.SchDoc", "v2", "Local change")
    status = clone_status(clone, now=NOW)
    assert (status["ahead"], status["behind"]) == (1, 0)
    assert "remote_last_commit" not in status
    assert warnings_of(status) == ["ahead"]


def test_uncommitted_changes_without_rewriting_the_index(clone):
    commit(clone, "Logo.txt", "logo", "Add logo")
    git(clone, "update-ref", "refs/remotes/origin/master", "HEAD")
    (clone / "Board.SchDoc").write_text("v1 edited", encoding="utf-8")
    (clone / "Notes.txt").write_text("new", encoding="utf-8")
    git(clone, "mv", "Board.PrjPcb", "Renamed.PrjPcb")
    # A file whose content is unchanged but whose mtime moved: a plain git status
    # refreshes the index and writes it back.
    os.utime(clone / "Logo.txt", (NOW.timestamp(), NOW.timestamp()))
    index = clone / ".git" / "index"
    before = (index.read_bytes(), index.stat().st_mtime_ns)

    status = clone_status(clone, now=NOW)

    assert (index.read_bytes(), index.stat().st_mtime_ns) == before
    assert status["uncommitted_count"] == 3
    assert status["uncommitted"] == {"modified": 1, "untracked": 1, "renamed": 1}
    assert status["uncommitted_design_files"] == 2
    # Design files first.
    assert status["uncommitted_sample"] == ["Board.SchDoc", "Renamed.PrjPcb", "Notes.txt"]
    assert warnings_of(status) == ["uncommitted_changes"]
    assert status["warnings"][0]["message"].startswith("2 design file(s)")
    # Control: without --no-optional-locks the same status does rewrite the index.
    git(clone, "status", "--porcelain")
    assert index.stat().st_mtime_ns != before[1]


def test_files_the_tools_do_not_read_are_counted_without_a_warning(clone):
    # Altium leaves files Altium 365 does not keep, e.g. the compiled project structure.
    (clone / "Board.PrjPcbStructure").write_text("x", encoding="utf-8")
    commit(clone, "Gerber.GTL", "g", "Fab outputs")
    git(clone, "update-ref", "refs/remotes/origin/master", "HEAD")
    (clone / "Gerber.GTL").unlink()
    status = clone_status(clone, now=NOW)
    assert status["uncommitted"] == {"untracked": 1, "deleted": 1}
    assert status["uncommitted_design_files"] == 0
    assert warnings_of(status) == []


def test_new_folder_of_sheets_counts_as_design_files(clone):
    new = clone / "NewSheets"
    new.mkdir()
    (new / "Power.SchDoc").write_text("p", encoding="utf-8")
    (new / "IO.SchDoc").write_text("i", encoding="utf-8")
    status = clone_status(clone, now=NOW)
    assert status["uncommitted"] == {"untracked": 2}
    assert status["uncommitted_design_files"] == 2
    assert sorted(status["uncommitted_sample"]) == ["NewSheets/IO.SchDoc",
                                                   "NewSheets/Power.SchDoc"]
    assert warnings_of(status) == ["uncommitted_changes"]


def test_no_fetch_head_falls_back_to_the_remote_branch_reflog(tmp_path):
    root = make_clone(tmp_path / "Board")
    status = clone_status(root, now=datetime.now(timezone.utc))
    assert status["last_fetch_source"] == "remote_branch_reflog"
    assert status["days_since_fetch"] == 0
    assert warnings_of(status) == []


def test_unknown_fetch_time(tmp_path):
    root = make_clone(tmp_path / "Board")
    shutil.rmtree(root / ".git" / "logs" / "refs" / "remotes")
    status = clone_status(root, now=NOW)
    assert status["last_fetch_utc"] is None
    assert status["days_since_fetch"] is None
    assert warnings_of(status) == ["fetch_time_unknown"]


def test_no_remote_branch(tmp_path):
    root = make_clone(tmp_path / "Board")
    fetched(root, 1)
    git(root, "update-ref", "-d", "refs/remotes/origin/master")
    status = clone_status(root, now=NOW)
    assert status["remote_branch"] is None
    assert (status["ahead"], status["behind"]) == (None, None)
    assert warnings_of(status) == ["no_remote_branch"]


def test_detached_head_compares_with_origins_default_branch(clone):
    git(clone, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/master")
    git(clone, "checkout", "-q", "--detach", "HEAD~1")
    status = clone_status(clone, now=NOW)
    assert status["branch"] is None
    assert status["remote_branch"] == "origin/master"
    assert (status["ahead"], status["behind"]) == (0, 1)


def test_project_in_a_subfolder_of_the_clone(clone):
    sub = clone / "Hardware" / "Board"
    sub.mkdir(parents=True)
    status = clone_status(sub, now=NOW)
    assert status["project_guid"] == GUID
    assert status["project_subfolder"] == "Hardware/Board"


def test_not_a_git_repository(tmp_path):
    folder = tmp_path / "Plain"
    folder.mkdir()
    assert clone_status(folder) == {"altium365": False, "reason": "not_a_git_repository"}


@pytest.mark.parametrize("url", ["https://github.com/example/board.git", None])
def test_origin_is_not_altium365(tmp_path, url):
    root = make_clone(tmp_path / "Board", url=url)
    assert clone_status(root) == {"altium365": False, "reason": "origin_is_not_altium365"}


def test_credentials_in_the_url_are_never_reported(tmp_path):
    root = make_clone(tmp_path / "Board",
                      url=f"https://someone:s3cr3t@example-ws.365.altium.com/git/{GUID}.git")
    status = clone_status(root, now=NOW)
    assert status["project_guid"] == GUID
    assert "s3cr3t" not in json.dumps(status)
    assert "someone" not in json.dumps(status)


def test_git_not_installed(clone, tmp_path):
    other = make_clone(tmp_path / "Other", url="https://github.com/example/board.git")
    plain = tmp_path / "Plain"
    plain.mkdir()
    with patch.object(clone_module.shutil, "which", return_value=None):
        status = clone_status(clone)
        # Whether it is an Altium 365 clone comes from .git/config without git.
        assert status["error"] == "git_not_found"
        assert (status["altium365"], status["project_guid"]) == (True, GUID)
        assert clone_status(other) == {"altium365": False, "reason": "origin_is_not_altium365"}
        assert clone_status(plain) == {"altium365": False, "reason": "not_a_git_repository"}


def test_git_failure_is_reported(clone, tmp_path):
    other = make_clone(tmp_path / "Other", url="https://github.com/example/board.git")

    def broken(*args, **kwargs):
        raise OSError("access denied")
    with patch.object(clone_module.subprocess, "Popen", side_effect=broken):
        status = clone_status(clone)
        assert clone_status(other) == {"altium365": False, "reason": "origin_is_not_altium365"}
    assert status == {"altium365": True, "workspace": "example-ws", "project_guid": GUID,
                      "error": "git_failed",
                      "message": "git could not be started: access denied"}


def test_origin_url_from_a_worktree_config(clone, tmp_path):
    worktree = tmp_path / "Second"
    git(clone, "worktree", "add", "-q", "--detach", str(worktree))
    assert (worktree / ".git").is_file()
    assert clone_module._origin_url_from_files(worktree / "sub") == URL
    with patch.object(clone_module.shutil, "which", return_value=None):
        assert clone_status(worktree)["project_guid"] == GUID


def test_only_local_read_only_git_commands(clone):
    calls = []
    real_popen = subprocess.Popen

    def recording_popen(args, **kwargs):
        calls.append(args)
        return real_popen(args, **kwargs)

    with patch.object(clone_module.subprocess, "Popen", side_effect=recording_popen):
        clone_status(clone, now=NOW)
    assert calls
    for args in calls:
        assert args[1:3] == ["--no-pager", "--no-optional-locks"]
        assert "protocol.allow=never" in args and "core.fsmonitor=false" in args
        subcommand = args[args.index("-C") + 2]
        assert subcommand in {"rev-parse", "config", "for-each-ref", "status", "log",
                              "rev-list"}
        if subcommand == "log":
            assert "--no-show-signature" in args
        if subcommand == "status":
            assert "--ignore-submodules=all" in args


def test_repository_filters_do_not_run(clone, tmp_path):
    # A clean filter (git-lfs is one) runs on git status for a file whose stat data
    # changed; it could write to .git. Required drivers must not make status fail either.
    marker = tmp_path / "filter-ran"
    (clone / ".gitattributes").write_text("*.txt filter=probe\n", encoding="utf-8")
    git(clone, "config", "filter.probe.clean", f"touch '{marker.as_posix()}'; cat")
    git(clone, "config", "filter.probe.required", "true")
    commit(clone, "Notes.txt", "abc\n", "Add notes")
    git(clone, "add", ".gitattributes")
    git(clone, "commit", "-q", "-m", "Attributes")
    git(clone, "update-ref", "refs/remotes/origin/master", "HEAD")
    marker.unlink(missing_ok=True)
    os.utime(clone / "Notes.txt", (NOW.timestamp(), NOW.timestamp()))
    status = clone_status(clone, now=NOW)
    assert "error" not in status
    assert status["uncommitted_count"] == 0
    assert not marker.exists()
    git(clone, "--no-optional-locks", "status", "--porcelain")  # control: the filter runs
    assert marker.exists()


def test_a_hanging_git_is_stopped(clone, monkeypatch):
    git(clone, "config", "alias.hang", "!sleep 30")
    monkeypatch.setattr(clone_module, "GIT_TIMEOUT_S", 1)
    start = time.monotonic()
    with pytest.raises(clone_module.GitError, match="did not answer within 1 s"):
        clone_module._git(shutil.which("git"), clone, "hang")
    assert time.monotonic() - start < 8


def test_git_calls_cannot_reach_a_remote(clone, tmp_path):
    # protocol.allow=never blocks every transport, the local one included.
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    git_exe = shutil.which("git")
    assert clone_module._git(git_exe, clone, "ls-remote", bare.as_uri(), check=False) is None
    git(clone, "ls-remote", bare.as_uri())  # control: allowed without the guard


def test_repository_config_cannot_start_an_fsmonitor_hook(clone, tmp_path):
    marker = tmp_path / "hook-ran"
    git(clone, "config", "core.fsmonitor", f"touch '{marker.as_posix()}'")
    clone_status(clone, now=NOW)
    assert not marker.exists()
    git(clone, "status", "--porcelain")  # control: a plain status runs the hook
    assert marker.exists()


# ---------- set_project_dir and get_clone_status ----------

@pytest.fixture
def main_env():
    with patch.object(main, "_project", None), \
         patch.object(main, "_variant_state", None), \
         patch.object(main, "_netlist_last_updated", None), \
         patch.object(main, "_pcb_session", main.PcbSession()), \
         patch.object(main, "_altium", AltiumClient()), \
         patch.object(main, "read_registry", return_value={"projects": []}), \
         patch.object(main, "upsert_registry_entry"), \
         patch.object(main._altium, "get_status", return_value={"running": False}):
        yield


def test_load_reports_the_altium365_status(main_env, clone):
    fetched(clone, 40, now=datetime.now(timezone.utc))
    result = json.loads(main.set_project_dir(str(clone)))
    assert result["partial"] is True
    status = result["altium365"]
    assert status["project_guid"] == GUID
    assert status["days_since_fetch"] == 40
    assert "fetch_stale" in warnings_of(status)
    assert json.loads(main.get_clone_status())["project_guid"] == GUID


def test_load_outside_a_clone_has_no_altium365_block(main_env, tmp_path):
    root = make_clone(tmp_path / "Board", url="https://github.com/example/board.git")
    result = json.loads(main.set_project_dir(str(root)))
    assert result["loaded"] is True
    assert "altium365" not in result
    error = json.loads(main.get_clone_status())
    assert error["error"] == "not_an_altium365_clone"
    assert error["reason"] == "origin_is_not_altium365"


def test_clone_status_never_fails_a_load(main_env, clone):
    with patch.object(main, "clone_status", side_effect=RuntimeError("boom")):
        result = json.loads(main.set_project_dir(str(clone)))
    assert result["loaded"] is True
    assert "altium365" not in result


def test_load_outside_altium365_without_git_has_no_altium365_block(main_env, tmp_path):
    root = make_clone(tmp_path / "Board", url="https://github.com/example/board.git")
    with patch.object(clone_module.shutil, "which", return_value=None):
        result = json.loads(main.set_project_dir(str(root)))
    assert result["loaded"] is True
    assert "altium365" not in result


def test_get_clone_status_never_raises(main_env, clone):
    with patch.object(main, "clone_status", side_effect=ValueError("odd output")):
        result = json.loads(main.get_clone_status(str(clone)))
    assert result == {"error": "clone_status_failed", "message": "ValueError: odd output"}


def test_get_clone_status_arguments(main_env, clone, tmp_path):
    assert json.loads(main.get_clone_status())["error"] == "no_project"
    assert json.loads(main.get_clone_status(str(tmp_path / "missing")))["error"] == "folder_not_found"
    assert json.loads(main.get_clone_status(str(clone)))["project_guid"] == GUID
