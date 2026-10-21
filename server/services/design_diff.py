"""What changed between two states of a project, offline: an ECO-style changelog for
reviews and release notes.

The two states are two revisions of the Git clone holding the loaded project (commits,
branches, tags, or 'working' for the files on disk) or two project folders. A revision's
.PrjPcb and the .SchDoc, .PcbDoc and .Annotation files it lists are read with
`git cat-file` into a temporary folder outside the clone, parsed with the parsers every
other tool uses, and deleted. Git runs with the clone_status hardening (no optional
locks, so nothing rewrites the index; no network and no lazy fetch; no fsmonitor; no
prompts) and only commands that read: rev-parse, ls-tree, log and cat-file. Nothing is
written to the clone.

Matching follows Altium's comparator: components by UniqueId (a channel copy's full
path, then the UniqueId on its sheet, then the UniqueId alone) and by designator only
when no UniqueId links them; nets by shared pins first (the project's Min Match % and
Min Matched Members) and by name second.

Nets come from the PcbDoc (each pad's net): an old revision cannot be compiled through
Altium and there is no offline schematic netlister, so a wiring change made on the
sheets and not yet pushed to the board with Update PCB is not seen (check_sch_pcb_sync
compares the sheets with the board). Components, parameters, variants and BOM lines come
from the sheets and the .PrjPcb. Only electrical, BOM and physical data is compared, so
purely graphical edits (moved symbols, wires, text, colours) are not reported.
"""
import hashlib
import math
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

try:
    from altium import enrich_netlist_sheets
    from parsers.annotation import parse_annotation
    from parsers.pcb_doc import MM_PER_MIL, parse_pcb_doc
    from parsers.prj_pcb import Variation, parse_prj_pcb
    from parsers.sch_doc import parse_sch_doc
    from services.bom import (
        BASE_VARIANT, build_parts, group_key, natural_key, plan_variant, resolve,
        variant_names,
    )
    from services.clone_status import (
        _GIT_CONFIG, _GIT_ENV, _POSIX, GIT_TIMEOUT_S, GitError, _kill_tree,
    )
    from services.impedance import profile_refs
    from services.pcb_rules import BINARY_KINDS, rule_values
    from services.sch_pcb_sync import DEFAULT_NET_MATCHING, net_matching_options
except ImportError:
    from server.altium import enrich_netlist_sheets
    from server.parsers.annotation import parse_annotation
    from server.parsers.pcb_doc import MM_PER_MIL, parse_pcb_doc
    from server.parsers.prj_pcb import Variation, parse_prj_pcb
    from server.parsers.sch_doc import parse_sch_doc
    from server.services.bom import (
        BASE_VARIANT, build_parts, group_key, natural_key, plan_variant, resolve,
        variant_names,
    )
    from server.services.clone_status import (
        _GIT_CONFIG, _GIT_ENV, _POSIX, GIT_TIMEOUT_S, GitError, _kill_tree,
    )
    from server.services.impedance import profile_refs
    from server.services.pcb_rules import BINARY_KINDS, rule_values
    from server.services.sch_pcb_sync import DEFAULT_NET_MATCHING, net_matching_options

WORKING = "working"
DESIGN_SUFFIXES = (".prjpcb", ".schdoc", ".pcbdoc", ".annotation")
RECENT_COMMITS = 15
BLOB_TIMEOUT_S = 120
POSITION_TOLERANCE_MIL = 0.1    # a part moved less than this has not moved
ROTATION_TOLERANCE_DEG = 0.01
LENGTH_TOLERANCE_MIL = 1.0      # routed-length changes below this are not reported
THICKNESS_TOLERANCE_MIL = 0.01
SAMPLE = 20                     # pins or members listed per entry
GROUP_PINS = 2                  # pads a net part of a split or merge holds at least

# Sections in the order they are listed; ict_hazards first.
SECTIONS = (
    "ict_hazards", "project", "components", "variants", "bom", "nets", "placement",
    "rules", "classes", "diff_pairs", "stackup", "routing",
)
BOARD_SECTIONS = ("nets", "placement", "rules", "classes", "diff_pairs", "stackup", "routing")
SCHEMATIC_SECTIONS = ("project", "components", "variants", "bom")

# The only git commands run: each reads the object store or the refs.
_READ_COMMANDS = frozenset({"rev-parse", "ls-tree", "log", "cat-file"})
_NO_AUTO = ("-c", "gc.auto=0", "-c", "maintenance.auto=false")
_REVISION = re.compile(r"[A-Za-z0-9_@][A-Za-z0-9_./~^@{}+-]{0,199}")
_LFS_POINTER = b"version https://git-lfs"
_LOG_FORMAT = "--format=%H%x1f%h%x1f%ct%x1f%an%x1f%s"

NOTES = [
    "Nets come from the PcbDoc (each pad's net): old revisions cannot be compiled "
    "through Altium and there is no offline schematic netlister, so a schematic wiring "
    "change not yet pushed to the board with Update PCB is not seen. check_sch_pcb_sync "
    "compares the sheets with the board.",
    "Purely graphical edits (moved symbols, wires, labels, text, colours) are not "
    "reported: only electrical, BOM and physical data is compared.",
    "Components match by UniqueId (a channel copy's full path through the .Annotation "
    "file, then the UniqueId on its sheet, then the UniqueId alone) and by designator "
    "only when no UniqueId links them; nets by shared pins first (the project's Min "
    "Match % and Min Matched Members, Altium's comparator defaults otherwise), then by "
    "name. Compile-masked parts are left out, as Altium's compiler leaves them out.",
    "Routed length is a net's track and arc copper (pour copper excluded), as "
    "get_net_pcb reports it, not the pad-to-pad signal length (get_signal_length); "
    "changes under 1 mil are not listed.",
    "Not compared: polygon pours and their copper, board shape and cutouts, silkscreen "
    "and mechanical layers, room outlines, xSignals, Draftsman drawings and OutJobs; "
    "free pads (pads not in a component, so their nets are not in the pin lists); pad, "
    "hole and footprint geometry under an unchanged footprint name; track widths and "
    "via sizes (only routed length and via count); sheet (title block) parameters. "
    "Parameter names are shown lower-case, as Altium matches them.",
]


class DiffError(Exception):
    """A structured tool error: payload is returned to the client as is."""

    def __init__(self, error: str, message: str, **extra):
        super().__init__(message)
        self.payload = {"error": error, "message": message, **extra}


# ---------- git, read only ----------

def _run_git(git: str, cwd: Path, *args: str, stdin: bytes | None = None,
             timeout: float = GIT_TIMEOUT_S) -> bytes:
    """stdout of a read-only git command, as bytes; GitError when it fails."""
    if args[0] not in _READ_COMMANDS:
        raise GitError(f"git {args[0]} is not a read-only command")
    env = {**os.environ, **_GIT_ENV}
    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed read-only git subcommands, no shell
            [git, "--no-pager", "--no-optional-locks", *_GIT_CONFIG, *_NO_AUTO,
             "-C", str(cwd), *args],
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            start_new_session=_POSIX,
        )
    except OSError as error:
        raise GitError(f"git could not be started: {error}") from error
    try:
        stdout, stderr = proc.communicate(input=stdin, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        _kill_tree(proc)
        raise GitError(f"git {args[0]} did not answer within {timeout:g} s") from error
    if proc.returncode != 0:
        lines = stderr.decode("utf-8", "replace").strip().splitlines()
        raise GitError(lines[0] if lines else f"git {args[0]} failed")
    return stdout


def _text(git: str, cwd: Path, *args: str) -> str:
    return _run_git(git, cwd, *args).decode("utf-8", "replace")


def _glob_escape(text: str) -> str:
    return re.sub(r"([\[\]*?\\])", r"\\\1", text)


class _Repo:
    """The Git work tree holding a project folder, read with read-only commands."""

    def __init__(self, project_dir: Path, git: str | None = None):
        self.git = git or shutil.which("git")
        if self.git is None:
            raise DiffError("git_not_found", "git is not installed or not on PATH, so "
                            "revisions cannot be read. Pass project_dir_a and "
                            "project_dir_b to compare two folders.")
        self.cwd = project_dir
        try:
            lines = _text(self.git, project_dir, "rev-parse", "--show-toplevel",
                          "--show-prefix").splitlines()
        except GitError as error:
            if "not a git repository" in str(error).lower():
                raise DiffError(
                    "not_a_git_repository",
                    "The project folder is not in a Git clone, so it has no revisions "
                    "to compare. Pass project_dir_a and project_dir_b to compare two "
                    "folders (e.g. a copy of an earlier release).") from error
            raise DiffError("git_failed", str(error)) from error
        self.top = Path(lines[0])
        self.prefix = lines[1] if len(lines) > 1 else ""

    def resolve(self, revision: str) -> str:
        if not _REVISION.fullmatch(revision):
            raise DiffError("invalid_revision",
                            f"'{revision}' is not a revision: pass a commit hash, branch, "
                            "tag, HEAD~n, or 'working' for the files on disk.")
        try:
            return _text(self.git, self.cwd, "rev-parse", "--verify", "--quiet",
                         f"{revision}^{{commit}}").strip()
        except GitError as error:
            raise DiffError("revision_not_found",
                            f"No commit '{revision}' in the clone. Call diff_design "
                            "without revisions to list the recent commits.") from error

    def parent(self, sha: str) -> str | None:
        try:
            return _text(self.git, self.cwd, "rev-parse", "--verify", "--quiet",
                         f"{sha}^1").strip() or None
        except GitError:
            return None

    def log(self, revision: str, pathspecs: list[str], limit: int) -> list[dict]:
        out = _text(self.git, self.cwd, "log", f"-n{limit}", "--no-show-signature",
                    _LOG_FORMAT, revision, "--", *pathspecs)
        commits = []
        for line in out.splitlines():
            parts = line.split("\x1f", 4)
            if len(parts) == 5:
                commits.append(_commit_row(parts))
        return commits

    def commit(self, sha: str) -> dict:
        out = _text(self.git, self.cwd, "log", "-1", "--no-show-signature", _LOG_FORMAT,
                    sha, "--").strip()
        return _commit_row(out.split("\x1f", 4))

    def tree(self, sha: str) -> dict[str, tuple[str, str]]:
        """Design files of a commit: lower-case path -> (path, blob id)."""
        files: dict[str, tuple[str, str]] = {}
        for entry in _run_git(self.git, self.cwd, "ls-tree", "-r", "-z", "--full-tree",
                              sha).split(b"\0"):
            meta, _, path = entry.partition(b"\t")
            fields = meta.split()
            if len(fields) != 3 or fields[1] != b"blob":
                continue
            name = path.decode("utf-8", "replace")
            if PurePosixPath(name).suffix.lower() in DESIGN_SUFFIXES:
                files[name.lower()] = (name, fields[2].decode("ascii"))
        return files

    def blobs(self, oids: list[str]) -> dict[str, bytes]:
        """Contents of blobs by id, in one `cat-file --batch` (no filters run)."""
        wanted = list(dict.fromkeys(oids))
        if not wanted:
            return {}
        out = _run_git(self.git, self.cwd, "cat-file", "--batch",
                       stdin=("\n".join(wanted) + "\n").encode("ascii"),
                       timeout=BLOB_TIMEOUT_S)
        found: dict[str, bytes] = {}
        at = 0
        for oid in wanted:
            end = out.index(b"\n", at)
            header = out[at:end].split()
            at = end + 1
            if len(header) == 3 and header[1] == b"blob":
                size = int(header[2])
                found[oid] = out[at:at + size]
                at += size + 1
        return found

    def pathspecs(self, outside: list[str]) -> list[str]:
        """The project folder's design files, plus documents it lists elsewhere in the
        repository (paths from the top)."""
        specs = [f":(top,glob,icase){_glob_escape(self.prefix)}**/*{suffix}"
                 for suffix in DESIGN_SUFFIXES]
        return specs + [f":(top,literal,icase){path}" for path in outside]


def _commit_row(parts: list[str]) -> dict:
    full, short, timestamp, author, subject = (parts + [""] * 5)[:5]
    try:
        date = datetime.fromtimestamp(int(timestamp), tz=timezone.utc).isoformat(
            timespec="seconds")
    except ValueError:
        date = None
    return {"commit": short, "hash": full, "date_utc": date, "author": author,
            "subject": subject}


# ---------- sources ----------

def _blob_id(data: bytes) -> str:
    """Git's blob id of the content, so files on disk compare with committed ones."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _key(path: str, base: Path) -> str:
    return os.path.relpath(path, base).replace("\\", "/").lower()


@dataclass
class DesignSource:
    """One state of the project: its .PrjPcb parsed and its documents (paths, blob ids).
    A git revision's documents are written to its temporary folder on first use."""

    label: dict
    prj_path: Path
    prj: object  # PrjPcbData
    sheets: dict[str, str] = field(default_factory=dict)       # key -> path
    annotations: dict[str, str] = field(default_factory=dict)  # key -> path
    pcb: tuple[str, str] | None = None                         # (key, path)
    digests: dict[str, str] = field(default_factory=dict)      # key -> blob id
    missing: dict[str, str] = field(default_factory=dict)      # key -> reason
    pending: dict[str, str] = field(default_factory=dict)      # path -> blob id (git)
    repo: _Repo | None = None
    _schematic: tuple | None = None
    _board: tuple | None = None

    def materialize(self) -> None:
        if not self.pending or self.repo is None:
            return
        contents = self.repo.blobs(list(self.pending.values()))
        for path, oid in self.pending.items():
            data = contents.get(oid)
            key = _key(path, self.prj_path.parent)
            if data is None:
                self.missing[key] = "not readable from the clone (missing object)"
                continue
            if data.startswith(_LFS_POINTER):
                self.missing[key] = "stored in Git LFS: not read (no filter is run)"
                continue
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(data)
        self.pending = {}

    def schematic(self) -> tuple[dict, list[dict]]:
        """({designator: component}, sheet errors), enriched the way a netlist-free
        load files sheet parts: through the .Annotation file to board designators."""
        if self._schematic is None:
            self.materialize()
            sheets, errors = [], []
            for key, path in self.sheets.items():
                name = Path(path).stem
                if key in self.missing or not Path(path).is_file():
                    errors.append({"sheet": name,
                                   "error": self.missing.get(key, "file not found")})
                    continue
                try:
                    sheets.append((name, parse_sch_doc(path)))
                except Exception as error:  # one bad sheet never hides the others
                    errors.append({"sheet": name, "error": f"{type(error).__name__}: {error}"})
            annotation = []
            for key, path in self.annotations.items():
                if key in self.missing or not Path(path).is_file():
                    continue
                try:
                    annotation.extend(parse_annotation(path))
                except Exception as error:
                    errors.append({"annotation": Path(path).name,
                                   "error": f"{type(error).__name__}: {error}"})
            netlist = enrich_netlist_sheets({"pin_to_net": {}}, sheets, annotation)
            self._schematic = (netlist["components"], errors)
        return self._schematic

    def board(self) -> tuple[object | None, str | None]:
        """(PcbData, None) or (None, why it was not read)."""
        if self._board is None:
            self.materialize()
            if self.pcb is None:
                self._board = (None, "the project lists no PcbDoc")
            else:
                key, path = self.pcb
                if key in self.missing:
                    self._board = (None, self.missing[key])
                elif not Path(path).is_file():
                    self._board = (None, "file not found")
                else:
                    try:
                        self._board = (parse_pcb_doc(path), None)
                    except Exception as error:
                        self._board = (None, f"parse failed: {type(error).__name__}: {error}")
        return self._board

    @property
    def schematic_digest(self) -> tuple:
        keys = ["<project>", *self.sheets, *self.annotations]
        return tuple((key, self.digests.get(key)) for key in sorted(keys))

    @property
    def board_digest(self) -> tuple | None:
        return None if self.pcb is None else (self.digests.get(self.pcb[0]),)


def _find_prjpcb(folder: Path, name: str | None) -> Path:
    if not folder.is_dir():
        raise DiffError("folder_not_found", f"Folder not found: {folder}")
    found = sorted(p for p in folder.iterdir()
                   if p.suffix.lower() == ".prjpcb" and p.is_file())
    if not found:
        raise DiffError("no_prjpcb", f"No .PrjPcb file found in {folder}.")
    if name:
        named = [p for p in found if p.name.lower() == name.lower()]
        if named:
            return named[0]
    return found[0]


def _documents(source: DesignSource) -> list[str]:
    prj = source.prj
    base = source.prj_path.parent
    for path in prj.sheet_paths:
        source.sheets[_key(path, base)] = path
    for path in prj.annotation_paths:
        source.annotations[_key(path, base)] = path
    if prj.pcb_doc_paths:
        source.pcb = (_key(prj.pcb_doc_paths[0], base), prj.pcb_doc_paths[0])
    return [*source.sheets.values(), *source.annotations.values(),
            *([source.pcb[1]] if source.pcb else [])]


def folder_source(folder: str | Path, prj_name: str | None = None,
                  label: dict | None = None) -> DesignSource:
    """The project in a folder, as saved on disk."""
    # Resolved like the paths parse_prj_pcb gives, so documents key the same way in
    # every source (a short 8.3 folder name would not).
    prj_path = _find_prjpcb(Path(folder), prj_name).resolve()
    source = DesignSource(label=label or {"source": "folder", "folder": str(folder)},
                          prj_path=prj_path, prj=parse_prj_pcb(str(prj_path)))
    source.digests["<project>"] = _blob_id(prj_path.read_bytes())
    for path in _documents(source):
        key = _key(path, prj_path.parent)
        try:
            source.digests[key] = _blob_id(Path(path).read_bytes())
        except OSError:
            source.missing[key] = "file not found"
    return source


def git_source(repo: _Repo, sha: str, prj_name: str, temp: Path) -> DesignSource:
    """The project as committed in sha: its .PrjPcb is read now, the documents it lists
    when first parsed, into temp (which mirrors the repository)."""
    tree = repo.tree(sha)
    wanted = (repo.prefix + prj_name).lower()
    if wanted not in tree:
        here = [key for key in tree
                if key.startswith(repo.prefix.lower()) and key.endswith(".prjpcb")
                and "/" not in key[len(repo.prefix):]]
        if len(here) != 1:
            raise DiffError("project_not_in_revision",
                            f"Commit {sha[:12]} has no {prj_name} in the project folder.",
                            commit=sha[:12])
        wanted = here[0]
    tree_path, prj_oid = tree[wanted]
    prj_path = temp / tree_path
    # A tree entry is any byte string: '..', a drive or (on Windows) a backslash in it
    # would place the file outside the temporary folder. The documents need no check:
    # parse_prj_pcb resolves their paths and only those inside temp are read.
    if not prj_path.resolve().is_relative_to(temp.resolve()):
        raise DiffError("unsafe_path", f"Commit {sha[:12]} stores the project under a "
                        "path that leaves the repository; it is not read.",
                        commit=sha[:12])
    prj_data = repo.blobs([prj_oid]).get(prj_oid)
    if prj_data is None:
        raise DiffError("missing_object", f"Commit {sha[:12]}'s .PrjPcb is not in the "
                        "clone's object store (a partial or shallow clone?): open the "
                        "project in Altium or fetch it again, then retry.",
                        commit=sha[:12])
    prj_path.parent.mkdir(parents=True, exist_ok=True)
    prj_path.write_bytes(prj_data)
    source = DesignSource(label={"source": "git", **repo.commit(sha)}, prj_path=prj_path,
                          prj=parse_prj_pcb(str(prj_path)), repo=repo)
    source.digests["<project>"] = prj_oid
    for path in _documents(source):
        key = _key(path, prj_path.parent)
        try:
            in_repo = Path(path).relative_to(temp).as_posix().lower()
        except ValueError:
            source.missing[key] = "outside the repository"
            continue
        entry = tree.get(in_repo)
        if entry is None:
            source.missing[key] = "not in this commit"
            continue
        source.digests[key] = entry[1]
        source.pending[path] = entry[1]
    return source


# ---------- matching ----------

def _match(left: list, right: list, keys) -> tuple[dict[int, int], dict[int, str]]:
    """Pair items level by level: a key links two items only when it is unique on both
    sides among the items still free. Returns (left index -> right index, how)."""
    pairs: dict[int, int] = {}
    how: dict[int, str] = {}
    free_left, free_right = set(range(len(left))), set(range(len(right)))
    for name, key in keys:
        by_left, by_right = defaultdict(list), defaultdict(list)
        for i in free_left:
            value = key(left[i])
            if value:
                by_left[value].append(i)
        for j in free_right:
            value = key(right[j])
            if value:
                by_right[value].append(j)
        for value, items in by_left.items():
            other = by_right.get(value)
            if len(items) == 1 and other and len(other) == 1:
                pairs[items[0]], how[items[0]] = other[0], name
                free_left.discard(items[0])
                free_right.discard(other[0])
    return pairs, how


def _fold(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _sample(items) -> list:
    return sorted(items, key=natural_key)[:SAMPLE]


# ---------- formatting ----------

class _Units:
    def __init__(self, units: str):
        self.name = units

    def length(self, mil: float | None, signed: bool = False) -> str:
        if mil is None:
            return "?"
        value = mil * MM_PER_MIL if self.name == "mm" else mil
        text = f"{value:+.4f}" if signed else f"{value:.4f}"
        text = text.rstrip("0").rstrip(".") if "." in text else text
        return f"{text} {self.name}"


def _r(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def _quote(value) -> str:
    return "(none)" if value in (None, "") else str(value)


def _entry(section: str, change: str, eco: str, text: str, **fields) -> dict:
    return {"section": section, "change": change, "eco": eco, "text": text,
            **{key: value for key, value in fields.items() if value not in (None, [], {})}}


# ---------- schematic: components, variants, BOM, project ----------

@dataclass
class _Comp:
    designator: str
    sheet: str
    uid: str
    path: str
    comment: str | None
    value: str | None
    footprint: str | None
    params: dict[str, str]
    properties: dict[str, str]


def _components(components: dict, project_params: dict) -> list[_Comp]:
    fallback = {name.lower(): value for name, value in (project_params or {}).items()}
    rows = []
    for designator, meta in components.items():
        if meta.get("compile_masked"):
            continue
        params = meta.get("params") or {}

        def text(raw, params=params):
            return None if raw is None else resolve(raw, params, fallback)[0]

        paths = meta.get("unique_paths") or []
        rows.append(_Comp(
            designator=designator, sheet=meta.get("sheet") or "",
            uid=meta.get("unique_id") or "", path=paths[0] if paths else "",
            comment=text(params.get("comment")), value=text(params.get("value")),
            footprint=meta.get("footprint"),
            params={k: v for k, v in params.items() if k not in ("comment", "value")},
            properties={
                # The designator drawn on the sheet, when the .Annotation file gives
                # the part another one on the board.
                "sheet designator": meta.get("logical_refdes") or designator,
                "description": meta.get("description") or "",
                "library reference": meta.get("lib_reference") or "",
                "design item id": meta.get("design_item_id") or "",
                "component kind": str(meta.get("kind") or 0),
            },
        ))
    return rows


_COMPONENT_KEYS = (
    ("unique_id_path", lambda c: c.path or None),
    ("unique_id", lambda c: (c.sheet.lower(), c.uid) if c.uid else None),
    ("unique_id", lambda c: c.uid or None),
    ("designator", lambda c: c.designator if "?" not in c.designator else None),
)


def _part_label(comment, footprint) -> str:
    return ", ".join(str(x) for x in (comment, footprint) if x) or "no comment or footprint"


def _component_changes(rows_a: list[_Comp], rows_b: list[_Comp]):
    pairs, how = _match(rows_a, rows_b, _COMPONENT_KEYS)
    out = []
    for i, a in enumerate(rows_a):
        if i in pairs:
            continue
        out.append(_entry("components", "removed", "Remove Component",
                          f"Remove Component {a.designator} ({_part_label(a.comment, a.footprint)})",
                          designator=a.designator, sheet=a.sheet, comment=a.comment,
                          footprint=a.footprint))
    matched_b = set(pairs.values())
    for j, b in enumerate(rows_b):
        if j in matched_b:
            continue
        out.append(_entry("components", "added", "Add Component",
                          f"Add Component {b.designator} ({_part_label(b.comment, b.footprint)})"
                          f" on sheet {b.sheet}",
                          designator=b.designator, sheet=b.sheet, comment=b.comment,
                          footprint=b.footprint))
    for i, j in pairs.items():
        a, b = rows_a[i], rows_b[j]
        d = b.designator
        if a.designator != b.designator:
            out.append(_entry("components", "redesignated", "Change Designator",
                              f"Change Designator {a.designator} -> {b.designator}",
                              designator=d, previous_designator=a.designator,
                              matched_by=how[i]))
        revalued = {}
        for name in ("comment", "value"):
            if _fold(getattr(a, name)) != _fold(getattr(b, name)):
                revalued[name] = {"from": getattr(a, name), "to": getattr(b, name)}
        if revalued:
            text = "; ".join(f"Change {name.title()} {d}: {_quote(v['from'])} -> "
                             f"{_quote(v['to'])}" for name, v in revalued.items())
            out.append(_entry("components", "revalued", "Change Comment", text,
                              designator=d, **revalued))
        if _fold(a.footprint) != _fold(b.footprint):
            out.append(_entry("components", "footprint", "Change Footprint",
                              f"Change Footprint {d}: {_quote(a.footprint)} -> "
                              f"{_quote(b.footprint)}",
                              designator=d, footprint={"from": a.footprint, "to": b.footprint}))
        changed = _dict_changes(a.params, b.params)
        if changed:
            out.append(_entry("components", "parameters", "Change Parameter",
                              f"Change Parameters {d}: " + _describe(changed),
                              designator=d, parameters=changed))
        changed = _dict_changes(a.properties, b.properties)
        if changed:
            out.append(_entry("components", "properties", "Change Component Properties",
                              f"Change {d}: " + _describe(changed),
                              designator=d, properties=changed))
    return out, pairs, how


def _dict_changes(a: dict, b: dict) -> list[dict]:
    changes = []
    for name in sorted(set(a) | set(b)):
        before, after = a.get(name), b.get(name)
        if _fold(before) != _fold(after):
            changes.append({"name": name, "from": before, "to": after})
    return changes


def _describe(changes: list[dict], limit: int = 6) -> str:
    parts = []
    for change in changes[:limit]:
        if change["from"] in (None, ""):
            parts.append(f"add {change['name']} = {change['to']}")
        elif change["to"] in (None, ""):
            parts.append(f"remove {change['name']} (was {change['from']})")
        else:
            parts.append(f"{change['name']} {change['from']} -> {change['to']}")
    if len(changes) > limit:
        parts.append(f"... {len(changes) - limit} more")
    return "; ".join(parts)


def _designator_hazards(rows_a, rows_b, pairs, how, where: str) -> dict[str, dict]:
    """Designators held by a different part in b than in a: another part (UniqueId)
    took the designator, or the two only match by designator and their UniqueIds
    differ (deleted and placed again)."""
    holder_b = defaultdict(list)
    for j, b in enumerate(rows_b):
        holder_b[b.designator].append(j)
    found = {}
    for i, a in enumerate(rows_a):
        if "?" in a.designator or not a.designator:
            continue
        holders = holder_b.get(a.designator) or []
        if len(holders) != 1:
            continue
        j = holders[0]
        b = rows_b[j]
        if pairs.get(i) == j and (how.get(i) != "designator" or not a.uid or not b.uid
                                  or a.uid == b.uid):
            continue
        found[a.designator] = {
            "where": [where],
            "before": {"comment": a.comment, "footprint": a.footprint},
            "after": {"comment": b.comment, "footprint": b.footprint},
            "previous_part_now": (rows_b[pairs[i]].designator
                                  if i in pairs and pairs[i] != j else None),
        }
    return found


def _variant_entries(prj) -> dict[str, object]:
    variants = {v.name.lower(): v for v in prj.variants}
    placeholder = variants.get("default")
    if (len(variants) == 1 and placeholder is not None and not placeholder.variations
            and not placeholder.dnp_refdes and not placeholder.param_variations):
        return {}
    return variants


def _variations(variant) -> dict[str, Variation]:
    entries = list(variant.variations) or [
        Variation(designator=d, unique_id="", kind=1) for d in variant.dnp_refdes
    ]
    out = {}
    for entry in entries:
        out[("uid:" + entry.uid_tail) if entry.unique_id else ("des:" + entry.designator)] = entry
    return out


def _variation_text(entry: Variation | None) -> str:
    if entry is None or entry.kind == 0:
        return "fitted"
    if entry.kind == 1:
        return "Not Fitted"
    return f"alternate part {entry.alternate_part or entry.alt.get('DesignItemId', '?')}"


def _variant_changes(prj_a, prj_b, rows_b: list[_Comp]) -> list[dict]:
    now_by_uid = {c.uid: c.designator for c in rows_b if c.uid}
    va, vb = _variant_entries(prj_a), _variant_entries(prj_b)
    out = []
    for name in sorted(set(va) - set(vb)):
        out.append(_entry("variants", "removed", "Remove Variant",
                          f"Remove Variant {va[name].name}", variant=va[name].name))
    for name in sorted(set(vb) - set(va)):
        entries = _variations(vb[name]).values()
        not_fitted = sum(1 for e in entries if e.kind == 1)
        alternates = sum(1 for e in entries if e.kind == 2)
        out.append(_entry("variants", "added", "Add Variant",
                          f"Add Variant {vb[name].name} ({not_fitted} Not Fitted, "
                          f"{alternates} alternate parts)", variant=vb[name].name))
    for name in sorted(set(va) & set(vb)):
        variant = vb[name].name
        before, after = _variations(va[name]), _variations(vb[name])
        for key in sorted(set(before) | set(after)):
            a, b = before.get(key), after.get(key)
            if _variation_text(a) == _variation_text(b) and (
                    (a.alt if a else {}) == (b.alt if b else {})):
                continue
            stored = (b or a).designator
            designator = now_by_uid.get((b or a).uid_tail, stored) if (b or a).unique_id else stored
            out.append(_entry(
                "variants", "fitting", "Change Variant",
                f"Variant {variant}: {designator} {_variation_text(b)} "
                f"(was {_variation_text(a)})",
                variant=variant, designator=designator,
                **{"from": _variation_text(a), "to": _variation_text(b)}))
        params_a, params_b = va[name].param_variations, vb[name].param_variations
        for designator in sorted(set(params_a) | set(params_b), key=natural_key):
            pa, pb = params_a.get(designator, {}), params_b.get(designator, {})
            changed = _dict_changes({k: v[1] for k, v in pa.items()},
                                    {k: v[1] for k, v in pb.items()})
            if changed:
                out.append(_entry("variants", "parameters", "Change Variant Parameter",
                                  f"Variant {variant}: {designator} varied parameters: "
                                  + _describe(changed),
                                  variant=variant, designator=designator, parameters=changed))
    return out


def _bom_lines(components: dict, variant, project_params: dict) -> dict[tuple, dict]:
    netlist = {"components": components, "pin_to_net": {}}
    plan = plan_variant(variant, netlist, variant_names(netlist)) if variant else None
    parts, _ = build_parts(netlist, plan, project_params)
    lines: dict[tuple, dict] = {}
    for part in parts:
        if not part.fitted:
            continue
        line = lines.setdefault(group_key(part, "mpn"), {
            "label": (f"{part.mpn} ({part.manufacturer})" if part.mpn and part.manufacturer
                      else part.mpn or _part_label(part.comment or part.value, part.footprint)),
            "designators": set(),
        })
        line["designators"].add(part.designator)
    return lines


def _bom_variant_changes(name: str, lines_a: dict, lines_b: dict) -> list[dict]:
    out = []
    for key in sorted(set(lines_a) | set(lines_b), key=lambda k: tuple(map(str, k))):
        a, b = lines_a.get(key), lines_b.get(key)
        da = a["designators"] if a else set()
        db = b["designators"] if b else set()
        if da == db:
            continue
        label = (b or a)["label"]
        if a is None:
            change, eco = "line_added", "Add BOM Line"
            text = f"BOM {name}: add line {label} x{len(db)} ({', '.join(_sample(db))})"
        elif b is None:
            change, eco = "line_removed", "Remove BOM Line"
            text = f"BOM {name}: remove line {label} x{len(da)} ({', '.join(_sample(da))})"
        else:
            change, eco = "quantity", "Change BOM Line"
            moves = [f"+{d}" for d in _sample(db - da)] + [f"-{d}" for d in _sample(da - db)]
            text = f"BOM {name}: line {label} quantity {len(da)} -> {len(db)} ({', '.join(moves)})"
        out.append(_entry("bom", change, eco, text, variant=name, line=label,
                          quantity={"from": len(da), "to": len(db)},
                          designators_added=_sample(db - da),
                          designators_removed=_sample(da - db)))
    return out


def _bom_changes(comps_a: dict, comps_b: dict, prj_a, prj_b) -> tuple[list[dict], list[str]]:
    """Line changes of the BOM without variations, then of each variant both states
    define; a variant whose line changes equal the base BOM's is only named."""
    params_a, params_b = prj_a.parameters, prj_b.parameters
    base = _bom_variant_changes(BASE_VARIANT, _bom_lines(comps_a, None, params_a),
                                _bom_lines(comps_b, None, params_b))
    out, same = list(base), []
    signature = {(e["line"], tuple(e.get("designators_added") or ()),
                  tuple(e.get("designators_removed") or ())) for e in base}
    va, vb = _variant_entries(prj_a), _variant_entries(prj_b)
    for key in sorted(set(va) & set(vb)):
        name = vb[key].name
        changes = _bom_variant_changes(name, _bom_lines(comps_a, va[key], params_a),
                                       _bom_lines(comps_b, vb[key], params_b))
        if {(e["line"], tuple(e.get("designators_added") or ()),
             tuple(e.get("designators_removed") or ())) for e in changes} == signature:
            same.append(name)
        else:
            out.extend(changes)
    return out, same


def _project_changes(a: DesignSource, b: DesignSource) -> list[dict]:
    out = []
    names_a = {k: Path(p).name for k, p in a.sheets.items()}
    names_b = {k: Path(p).name for k, p in b.sheets.items()}
    for key in sorted(set(names_b) - set(names_a)):
        out.append(_entry("project", "sheet_added", "Add Sheet",
                          f"Add sheet {names_b[key]} to the project", sheet=names_b[key]))
    for key in sorted(set(names_a) - set(names_b)):
        out.append(_entry("project", "sheet_removed", "Remove Sheet",
                          f"Remove sheet {names_a[key]} from the project", sheet=names_a[key]))
    pcb_a = Path(a.pcb[1]).name if a.pcb else None
    pcb_b = Path(b.pcb[1]).name if b.pcb else None
    if (pcb_a or "").lower() != (pcb_b or "").lower():
        out.append(_entry("project", "pcb_document", "Change PCB Document",
                          f"PCB document {_quote(pcb_a)} -> {_quote(pcb_b)}",
                          pcb_document={"from": pcb_a, "to": pcb_b}))
    changed = _dict_changes(a.prj.parameters, b.prj.parameters)
    if changed:
        out.append(_entry("project", "parameters", "Change Project Parameter",
                          "Project parameters: " + _describe(changed), parameters=changed))
    return out


# ---------- board: parts, nets, rules, classes, pairs, stackup, routing ----------

@dataclass
class _Part:
    index: int
    designator: str
    path: str
    tail: str
    footprint: str | None
    x: float | None
    y: float | None
    rotation: float | None
    side: str
    comment: str | None
    uid: str = ""


def _parts(pcb) -> list[_Part]:
    return [
        _Part(index=c.index, designator=c.designator or c.refdes or "",
              path=c.source_unique_id or "", tail=c.source_uid_tail or "",
              footprint=c.footprint, x=c.x, y=c.y, rotation=c.rotation, side=c.side,
              comment=c.comment, uid=c.source_uid_tail or "")
        for c in pcb.components
    ]


_PART_KEYS = (
    ("unique_id_path", lambda p: p.path or None),
    ("unique_id", lambda p: p.tail or None),
    ("designator", lambda p: p.designator or None),
    # Parts with neither (logos, fiducials placed on the board) that did not change.
    ("position", lambda p: (p.footprint, p.side, _r(p.x, 1), _r(p.y, 1))
     if p.x is not None and p.y is not None else None),
)


def _angle_delta(a: float, b: float) -> float:
    return abs((b - a + 180.0) % 360.0 - 180.0)


def _placement_changes(parts_a, parts_b, pairs, how, units: _Units) -> list[dict]:
    out = []
    matched_b = set(pairs.values())
    for i, a in enumerate(parts_a):
        if i not in pairs:
            out.append(_entry("placement", "removed", "Remove Component",
                              f"Remove {a.designator} from the board ({_quote(a.footprint)})",
                              designator=a.designator, footprint=a.footprint))
    for j, b in enumerate(parts_b):
        if j not in matched_b:
            out.append(_entry("placement", "added", "Add Component",
                              f"Add {b.designator} to the board ({_quote(b.footprint)}) at "
                              f"({units.length(b.x)}, {units.length(b.y)}) on {b.side}",
                              designator=b.designator, footprint=b.footprint,
                              x_mil=_r(b.x), y_mil=_r(b.y), side=b.side))
    for i, j in pairs.items():
        a, b = parts_a[i], parts_b[j]
        d = b.designator
        if a.designator != b.designator:
            out.append(_entry("placement", "redesignated", "Change Designator",
                              f"Change Designator on the board {a.designator} -> {d}",
                              designator=d, previous_designator=a.designator,
                              matched_by=how[i]))
        if None not in (a.x, a.y, b.x, b.y):
            dx, dy = b.x - a.x, b.y - a.y
            if math.hypot(dx, dy) > POSITION_TOLERANCE_MIL:
                out.append(_entry("placement", "moved", "Move Component",
                                  f"Move {d} by ({units.length(dx, True)}, "
                                  f"{units.length(dy, True)}) to ({units.length(b.x)}, "
                                  f"{units.length(b.y)})",
                                  designator=d, dx_mil=_r(dx), dy_mil=_r(dy),
                                  x_mil=_r(b.x), y_mil=_r(b.y),
                                  distance_mil=_r(math.hypot(dx, dy))))
        if None not in (a.rotation, b.rotation) and \
                _angle_delta(a.rotation, b.rotation) > ROTATION_TOLERANCE_DEG:
            out.append(_entry("placement", "rotated", "Rotate Component",
                              f"Rotate {d} {a.rotation:g} -> {b.rotation:g} deg",
                              designator=d, rotation={"from": a.rotation, "to": b.rotation}))
        if a.side != b.side:
            out.append(_entry("placement", "flipped", "Flip Component",
                              f"Flip {d} to the {b.side} side",
                              designator=d, side={"from": a.side, "to": b.side}))
        if _fold(a.footprint) != _fold(b.footprint):
            out.append(_entry("placement", "footprint", "Change Footprint",
                              f"Change Footprint {d} on the board: {_quote(a.footprint)} -> "
                              f"{_quote(b.footprint)}",
                              designator=d, footprint={"from": a.footprint, "to": b.footprint}))
    return out


def _members(pcb, part_key: dict[int, object]) -> dict[str, set]:
    """Net name -> its pads, each (part key, pad name)."""
    members: dict[str, set] = defaultdict(set)
    for p in pcb.primitives:
        if p.kind != "pad" or p.component is None or p.net is None or not p.pad_name:
            continue
        if p.net >= len(pcb.nets) or p.component not in part_key:
            continue
        members[pcb.nets[p.net]].add((part_key[p.component], p.pad_name))
    return members


def _match_nets(members_a: dict, members_b: dict, names_a: set, names_b: set,
                options: dict) -> dict[str, str]:
    """A net -> B net, by shared pads first (the most shared first), then by name."""
    owner: dict = defaultdict(set)
    for net, pads in members_b.items():
        for pad in pads:
            owner[pad].add(net)
    candidates = []
    for net, pads in members_a.items():
        shared = Counter(other for pad in pads for other in owner.get(pad, ()))
        for other, count in shared.items():
            size = max(len(pads), len(members_b[other]))
            if pads == members_b[other] or (
                    count >= options["min_matched_members"]
                    and 100.0 * count / size >= options["min_match_percent"]):
                candidates.append((-count, other != net, net, other))
    candidates.sort()
    to_b: dict[str, str] = {}
    taken: set[str] = set()
    for _, _, net, other in candidates:
        if net not in to_b and other not in taken:
            to_b[net] = other
            taken.add(other)
    free_b = names_b - taken
    if not options.get("name_matching", True):
        # A net without pads on either side (tracks, vias or free pads only) has no
        # members to match by: its name is all it has, even with name matching off.
        for net in sorted(names_a - set(to_b)):
            if net in free_b and not members_a.get(net) and not members_b.get(net):
                to_b[net] = net
                taken.add(net)
        return to_b
    folded: dict[str, list[str]] = defaultdict(list)
    for net in free_b:
        folded[net.lower()].append(net)
    for net in sorted(names_a - set(to_b)):
        other = net if net in free_b else None
        if other is None and len(folded.get(net.lower(), [])) == 1:
            other = folded[net.lower()][0]
        if other and other not in taken:
            to_b[net] = other
            taken.add(other)
    return to_b


def _net_changes(members_a, members_b, names_a, names_b, to_b, label) -> tuple[list, dict]:
    """Added, removed, renamed, merged and split nets and pins moved between nets.
    Returns (entries, B net -> A net for the routing comparison)."""
    net_a = {pad: net for net, pads in members_a.items() for pad in pads}
    net_b = {pad: net for net, pads in members_b.items() for pad in pads}
    common = set(net_a) & set(net_b)
    went: dict[str, Counter] = defaultdict(Counter)  # A net -> B nets of its pads
    came: dict[str, Counter] = defaultdict(Counter)  # B net -> A nets of its pads
    for pad in common:
        went[net_a[pad]][net_b[pad]] += 1
        came[net_b[pad]][net_a[pad]] += 1

    def majority(counter: Counter) -> str | None:
        """The net holding most of these pads, when it holds more than half and at
        least GROUP_PINS of them; fewer moved pads are pin moves."""
        top = counter.most_common(1)
        if top and 2 * top[0][1] > sum(counter.values()) and top[0][1] >= GROUP_PINS:
            return top[0][0]
        return None

    splits = defaultdict(list)   # A net -> B nets that came from it
    for net, counter in came.items():
        source = majority(counter)
        if source is not None:
            splits[source].append(net)
    splits = {net: sorted(parts) for net, parts in splits.items() if len(parts) > 1}
    merges = defaultdict(list)   # B net -> A nets that went into it
    for net, counter in went.items():
        target = majority(counter)
        if target is not None:
            merges[target].append(net)
    merges = {net: sorted(parts) for net, parts in merges.items() if len(parts) > 1}
    grouped = set()  # (A net, B net) pairs explained by a split or merge
    in_group_a, in_group_b = set(), set()
    out = []
    for net, parts in sorted(splits.items()):
        grouped.update((net, part) for part in parts)
        in_group_a.add(net)
        in_group_b.update(parts)
        out.append(_entry("nets", "split", "Split Net",
                          f"Split Net {net} into " + ", ".join(
                              f"{part} ({went[net][part]} pins)" for part in parts),
                          net=net, into=parts))
    for net, parts in sorted(merges.items()):
        grouped.update((part, net) for part in parts)
        in_group_b.add(net)
        in_group_a.update(parts)
        out.append(_entry("nets", "merged", "Merge Nets",
                          "Merge Nets " + ", ".join(
                              f"{part} ({came[net][part]} pins)" for part in parts)
                          + f" into {net}", net=net, merged=parts))
    from_b = {b: a for a, b in to_b.items()}
    for net, other in sorted(to_b.items()):
        if other != net and net not in in_group_a and other not in in_group_b:
            out.append(_entry("nets", "renamed", "Change Net Name",
                              f"Rename Net {net} -> {other} ({len(members_b.get(other, ()))} pins)",
                              net=other, previous_name=net,
                              pins=len(members_b.get(other, ()))))
    for net in sorted(names_a - set(to_b) - in_group_a):
        out.append(_entry("nets", "removed", "Remove Net",
                          f"Remove Net {net} ({len(members_a.get(net, ()))} pins)",
                          net=net, pins=len(members_a.get(net, ()))))
    for net in sorted(names_b - set(from_b) - in_group_b):
        pads = members_b.get(net, set())
        out.append(_entry("nets", "added", "Add Net",
                          f"Add Net {net} ({len(pads)} pins)", net=net, pins=len(pads),
                          pins_sample=_sample(label(p) for p in pads)))
    moves = []
    for pad in common:
        source, target = net_a[pad], net_b[pad]
        if to_b.get(source) == target or (source, target) in grouped:
            continue
        moves.append(_entry("nets", "pin_moved", "Move Pin",
                            f"Move Pin {label(pad)} from {source} to {target}",
                            pin=label(pad), **{"from": source, "to": target}))
    moves.sort(key=lambda e: natural_key(e["pin"]))
    return out + moves, from_b


def _net_hazards(members_a, members_b, label, unmatched_keys) -> list[dict]:
    out = []
    for net in sorted(set(members_a) & set(members_b), key=natural_key):
        pads_a, pads_b = members_a[net], members_b[net]
        if pads_a == pads_b:
            continue
        added, removed = pads_b - pads_a, pads_a - pads_b
        parts_only = all(pad[0] in unmatched_keys for pad in added | removed)
        out.append(_entry(
            "ict_hazards", "net_connections_changed", "Warning",
            f"Net {net} kept its name but its connections changed ({len(added)} pins "
            f"added, {len(removed)} removed): an ICT fixture or test program made for the "
            "old revision probes the wrong pins.",
            net=net, pins_added=_sample(label(p) for p in added),
            pins_removed=_sample(label(p) for p in removed),
            pins_added_count=len(added), pins_removed_count=len(removed),
            only_parts_added_or_removed=parts_only or None))
    return out


def _signal_layers(board) -> dict[int, str]:
    order = sorted(range(len(board.layers)), key=lambda i: (
        board.layers[i].stack_order if board.layers[i].stack_order is not None else -1, i))
    layers = {}
    for i in order:
        layer = board.layers[i]
        if layer.kind == "copper" and layer.v6_id and 1 <= layer.v6_id <= 32:
            layers.setdefault(layer.v6_id, layer.name)
    return layers


def _flatten(value, prefix: str = "") -> dict[str, object]:
    if isinstance(value, dict):
        flat = {}
        for key, item in value.items():
            flat.update(_flatten(item, f"{prefix}.{key}" if prefix else str(key)))
        return flat
    if isinstance(value, float):
        return {prefix: round(value, 4)}
    if isinstance(value, list):
        return {prefix: tuple(sorted(map(str, value)))}
    return {prefix: value}


def _rule_fields(rule, layers, board) -> dict:
    fields = {"enabled": rule.enabled, "scope": rule.scope1, "comment": rule.comment}
    if rule.kind in BINARY_KINDS:
        fields["scope2"] = rule.scope2
        fields["connective_checking"] = rule.net_scope
    values = rule_values(rule, layers, board.substacks, profile_refs(board))
    for key, value in _flatten(values).items():
        fields[f"values.{key}"] = value
    return fields


def _value_change(name: str, before, after, units: _Units) -> tuple[dict, str]:
    if "_mil" in name and all(isinstance(v, (int, float, type(None))) and not isinstance(v, bool)
                              for v in (before, after)):
        plain = name.replace("_mil", "")
        return ({"field": plain, "from_mil": before, "to_mil": after},
                f"{plain} {units.length(before)} -> {units.length(after)}")
    return ({"field": name, "from": before, "to": after},
            f"{name} {_quote(before)} -> {_quote(after)}")


def _rule_changes(pcb_a, pcb_b, units: _Units) -> list[dict]:
    rules_a, rules_b = pcb_a.rules, pcb_b.rules
    keys = (
        ("unique_id", lambda r: r.unique_id or None),
        ("name", lambda r: (r.kind, r.name.lower())),
    )
    pairs, _ = _match(rules_a, rules_b, keys)
    out = []
    for i, rule in enumerate(rules_a):
        if i not in pairs:
            out.append(_entry("rules", "removed", "Remove Rule",
                              f"Remove Rule {rule.name} ({rule.kind})",
                              rule=rule.name, kind=rule.kind))
    matched_b = set(pairs.values())
    for j, rule in enumerate(rules_b):
        if j not in matched_b:
            out.append(_entry("rules", "added", "Add Rule",
                              f"Add Rule {rule.name} ({rule.kind}), scope {rule.scope1}",
                              rule=rule.name, kind=rule.kind, scope=rule.scope1))
    # Priorities are ranks within a kind: a new rule shifts every one below it, so only
    # a change in the order of the rules both states have is reported.
    rank_a, rank_b = {}, {}
    by_kind = defaultdict(list)
    for i in pairs:
        by_kind[rules_a[i].kind].append(i)
    for items in by_kind.values():
        for rank, i in enumerate(sorted(items, key=lambda i: rules_a[i].priority)):
            rank_a[i] = rank
        for rank, i in enumerate(sorted(items, key=lambda i: rules_b[pairs[i]].priority)):
            rank_b[i] = rank
    layers_a, layers_b = _signal_layers(pcb_a.board), _signal_layers(pcb_b.board)
    for i, j in sorted(pairs.items(), key=lambda p: (rules_b[p[1]].kind, rules_b[p[1]].name)):
        a, b = rules_a[i], rules_b[j]
        fields_a, fields_b = _rule_fields(a, layers_a, pcb_a.board), _rule_fields(
            b, layers_b, pcb_b.board)
        changes, texts = [], []
        if a.name != b.name:
            changes.append({"field": "name", "from": a.name, "to": b.name})
            texts.append(f"renamed from {a.name}")
        if rank_a[i] != rank_b[i]:
            changes.append({"field": "priority", "from": a.priority, "to": b.priority})
            texts.append(f"priority {a.priority} -> {b.priority}")
        for name in sorted(set(fields_a) | set(fields_b)):
            before, after = fields_a.get(name), fields_b.get(name)
            if before != after:
                change, text = _value_change(name, before, after, units)
                changes.append(change)
                texts.append(text)
        if changes:
            out.append(_entry("rules", "changed", "Change Rule",
                              f"Change Rule {b.name} ({b.kind}): " + "; ".join(texts[:6])
                              + (f"; ... {len(texts) - 6} more" if len(texts) > 6 else ""),
                              rule=b.name, kind=b.kind, changes=changes))
    return out


def _class_changes(pcb_a, pcb_b) -> list[dict]:
    def classes(pcb):
        return {(c.kind, c.name.lower()): c for c in pcb.classes}

    ca, cb = classes(pcb_a), classes(pcb_b)
    out = []
    for key in sorted(set(ca) - set(cb)):
        c = ca[key]
        out.append(_entry("classes", "removed", "Remove Class",
                          f"Remove {c.kind} class {c.name}", kind=c.kind, name=c.name))
    for key in sorted(set(cb) - set(ca)):
        c = cb[key]
        out.append(_entry("classes", "added", "Add Class",
                          f"Add {c.kind} class {c.name} ({len(c.members)} members)",
                          kind=c.kind, name=c.name, members=len(c.members)))
    for key in sorted(set(ca) & set(cb)):
        a, b = ca[key], cb[key]
        if a.superclass or b.superclass:
            continue
        added, removed = set(b.members) - set(a.members), set(a.members) - set(b.members)
        if added or removed:
            text = ", ".join([f"+{m}" for m in _sample(added)][:8]
                             + [f"-{m}" for m in _sample(removed)][:8])
            out.append(_entry("classes", "members", "Change Class Members",
                              f"Change {b.kind} class {b.name}: +{len(added)} -{len(removed)} "
                              f"members ({text})",
                              kind=b.kind, name=b.name, members_added=_sample(added),
                              members_removed=_sample(removed),
                              members_added_count=len(added),
                              members_removed_count=len(removed)))
    return out


def _pair_changes(pcb_a, pcb_b) -> list[dict]:
    pa = {p.name.lower(): p for p in pcb_a.diff_pairs}
    pb = {p.name.lower(): p for p in pcb_b.diff_pairs}
    out = []
    for key in sorted(set(pa) - set(pb)):
        p = pa[key]
        out.append(_entry("diff_pairs", "removed", "Remove Differential Pair",
                          f"Remove Differential Pair {p.name} ({p.positive_net} / "
                          f"{p.negative_net})", pair=p.name))
    for key in sorted(set(pb) - set(pa)):
        p = pb[key]
        out.append(_entry("diff_pairs", "added", "Add Differential Pair",
                          f"Add Differential Pair {p.name} ({p.positive_net} / "
                          f"{p.negative_net})", pair=p.name, positive_net=p.positive_net,
                          negative_net=p.negative_net))
    for key in sorted(set(pa) & set(pb)):
        a, b = pa[key], pb[key]
        if (a.positive_net, a.negative_net) != (b.positive_net, b.negative_net):
            out.append(_entry("diff_pairs", "nets", "Change Differential Pair",
                              f"Change Differential Pair {b.name}: {a.positive_net} / "
                              f"{a.negative_net} -> {b.positive_net} / {b.negative_net}",
                              pair=b.name,
                              positive_net={"from": a.positive_net, "to": b.positive_net},
                              negative_net={"from": a.negative_net, "to": b.negative_net}))
    return out


_LAYER_FIELDS = (("kind", "kind", None), ("copper_thick_mil", "copper thickness", True),
                 ("diel_height_mil", "dielectric height", True),
                 ("diel_const", "dielectric constant", False), ("material", "material", None))


def _stack(board) -> list:
    return sorted((layer for layer in board.layers
                   if layer.stack_order is not None and layer.kind in ("copper", "dielectric")),
                  key=lambda layer: layer.stack_order)


def _stack_changes(board_a, board_b, units: _Units) -> list[dict]:
    out = []
    stack_a, stack_b = _stack(board_a), _stack(board_b)
    names_a, names_b = [x.name for x in stack_a], [x.name for x in stack_b]
    for name, count in sorted((Counter(names_b) - Counter(names_a)).items()):
        out.append(_entry("stackup", "layer_added", "Add Layer", f"Add Layer {name}",
                          layer=name, count=count if count > 1 else None))
    for name, count in sorted((Counter(names_a) - Counter(names_b)).items()):
        out.append(_entry("stackup", "layer_removed", "Remove Layer", f"Remove Layer {name}",
                          layer=name, count=count if count > 1 else None))
    if Counter(names_a) == Counter(names_b) and names_a != names_b:
        out.append(_entry("stackup", "reordered", "Reorder Layers",
                          "Layer order changed: " + " / ".join(names_b), order=names_b))

    def occurrences(stack):
        seen, keyed = Counter(), {}
        for layer in stack:
            keyed[(layer.name, seen[layer.name])] = layer
            seen[layer.name] += 1
        return keyed

    la, lb = occurrences(stack_a), occurrences(stack_b)
    for key in [k for k in lb if k in la]:
        a, b = la[key], lb[key]
        changes, texts = [], []
        for attr, label, length in _LAYER_FIELDS:
            before, after = getattr(a, attr), getattr(b, attr)
            if isinstance(before, float) or isinstance(after, float):
                if before is not None and after is not None and abs(before - after) <= 1e-6:
                    continue
            if before == after:
                continue
            if length:
                changes.append({"field": attr.replace("_mil", ""), "from_mil": before,
                                "to_mil": after})
                texts.append(f"{label} {units.length(before)} -> {units.length(after)}")
            else:
                changes.append({"field": attr, "from": before, "to": after})
                texts.append(f"{label} {_quote(before)} -> {_quote(after)}")
        if changes:
            out.append(_entry("stackup", "layer_changed", "Change Layer",
                              f"Change Layer {key[0]}: " + "; ".join(texts),
                              layer=key[0], changes=changes))

    def thickness(stack):
        return sum((x.copper_thick_mil or 0.0) + (x.diel_height_mil or 0.0) for x in stack)

    if stack_a and stack_b and abs(thickness(stack_a) - thickness(stack_b)) > \
            THICKNESS_TOLERANCE_MIL:
        out.append(_entry("stackup", "thickness", "Change Stack Thickness",
                          f"Stack thickness {units.length(thickness(stack_a))} -> "
                          f"{units.length(thickness(stack_b))}",
                          thickness_from_mil=_r(thickness(stack_a), 3),
                          thickness_to_mil=_r(thickness(stack_b), 3)))
    copper_a = sum(1 for x in board_a.layers if x.kind == "copper")
    copper_b = sum(1 for x in board_b.layers if x.kind == "copper")
    if copper_a != copper_b:
        out.append(_entry("stackup", "copper_layers", "Change Layer Count",
                          f"Copper layers {copper_a} -> {copper_b}",
                          copper_layers={"from": copper_a, "to": copper_b}))
    return out + _impedance_changes(board_a, board_b, units)


def _profile_layers(board, profile) -> dict[str, dict]:
    names = {layer.v7_id: layer.name for layer in board.layers if layer.v7_id is not None}
    rows = {}
    for entry in profile.layers:
        if not entry.enabled:
            continue
        name = names.get(entry.layer_v7, f"layer {entry.layer_v7}")
        row = {"width_mil": _r(entry.width_mil, 4),
               "calculated_ohm": _r(entry.calculated_ohm, 2)}
        if profile.differential:
            row["gap_mil"] = _r(entry.gap_mil, 4)
        rows[name] = row
    return rows


def _impedance_changes(board_a, board_b, units: _Units) -> list[dict]:
    pa = {p.name.lower(): p for p in board_a.impedance_profiles}
    pb = {p.name.lower(): p for p in board_b.impedance_profiles}
    out = []
    for key in sorted(set(pa) - set(pb)):
        out.append(_entry("stackup", "profile_removed", "Remove Impedance Profile",
                          f"Remove Impedance Profile {pa[key].name}", profile=pa[key].name))
    for key in sorted(set(pb) - set(pa)):
        p = pb[key]
        out.append(_entry("stackup", "profile_added", "Add Impedance Profile",
                          f"Add Impedance Profile {p.name} ({_quote(p.target_ohm)} ohm)",
                          profile=p.name, target_ohm=p.target_ohm))
    for key in sorted(set(pa) & set(pb)):
        a, b = pa[key], pb[key]
        changes, texts = [], []
        for attr, label in (("target_ohm", "target ohm"), ("tolerance_pct", "tolerance %"),
                            ("differential", "differential")):
            if getattr(a, attr) != getattr(b, attr):
                changes.append({"field": attr, "from": getattr(a, attr), "to": getattr(b, attr)})
                texts.append(f"{label} {getattr(a, attr)} -> {getattr(b, attr)}")
        rows_a, rows_b = _profile_layers(board_a, a), _profile_layers(board_b, b)
        for layer in sorted(set(rows_a) | set(rows_b)):
            ra, rb = rows_a.get(layer, {}), rows_b.get(layer, {})
            for name in sorted(set(ra) | set(rb)):
                if ra.get(name) != rb.get(name):
                    change, text = _value_change(f"{layer}.{name}", ra.get(name),
                                                 rb.get(name), units)
                    changes.append(change)
                    texts.append(text)
        if changes:
            out.append(_entry("stackup", "profile_changed", "Change Impedance Profile",
                              f"Change Impedance Profile {b.name}: " + "; ".join(texts[:6])
                              + (f"; ... {len(texts) - 6} more" if len(texts) > 6 else ""),
                              profile=b.name, changes=changes))
    return out


def _routing(pcb) -> tuple[dict[str, float], Counter]:
    """Per net: routed track and arc length (pour copper excluded) and via count."""
    length: dict[str, float] = defaultdict(float)
    vias: Counter = Counter()
    for p in pcb.primitives:
        if p.net is None or p.net >= len(pcb.nets):
            continue
        if p.kind in ("track", "arc") and p.pour_id is None and p.poured_by is None:
            length[pcb.nets[p.net]] += (p.length if p.length is not None
                                        else math.hypot(p.x2 - p.x1, p.y2 - p.y1))
        elif p.kind == "via":
            vias[pcb.nets[p.net]] += 1
    return length, vias


def _routing_changes(pcb_a, pcb_b, from_b: dict[str, str], units: _Units) -> list[dict]:
    length_a, vias_a = _routing(pcb_a)
    length_b, vias_b = _routing(pcb_b)
    out = []
    for net_b, net_a in from_b.items():
        la, lb = length_a.get(net_a, 0.0), length_b.get(net_b, 0.0)
        va, vb = vias_a.get(net_a, 0), vias_b.get(net_b, 0)
        if abs(lb - la) <= LENGTH_TOLERANCE_MIL and va == vb:
            continue
        text = (f"Net {net_b}: routed length {units.length(la)} -> {units.length(lb)} "
                f"({units.length(lb - la, True)})")
        if va != vb:
            text += f"; vias {va} -> {vb}"
        out.append(_entry("routing", "changed", "Routing", text, net=net_b,
                          previous_name=net_a if net_a != net_b else None,
                          length_from_mil=_r(la, 1), length_to_mil=_r(lb, 1),
                          length_delta_mil=_r(lb - la, 1),
                          vias={"from": va, "to": vb} if va != vb else None))
    out.sort(key=lambda e: (-abs(e.get("length_delta_mil") or 0.0), natural_key(e["net"])))
    return out


# ---------- the diff ----------

def _summary(entries: list[dict]) -> dict:
    counts: dict[str, Counter] = {section: Counter() for section in SECTIONS}
    for entry in entries:
        counts[entry["section"]][entry["change"]] += 1
    return {section: dict(counter) for section, counter in counts.items() if counter}


def _board_diff(a: DesignSource, b: DesignSource, units: _Units,
                options: dict) -> tuple[list[dict], dict, dict]:
    """Board entries, designator hazards on the board and routing totals."""
    pcb_a, _ = a.board()
    pcb_b, _ = b.board()
    parts_a, parts_b = _parts(pcb_a), _parts(pcb_b)
    pairs, how = _match(parts_a, parts_b, _PART_KEYS)
    out = _placement_changes(parts_a, parts_b, pairs, how, units)
    # Pads are keyed by their part: a linked pair shares a key, an unlinked part keeps
    # its own, so its pads never match.
    key_a, key_b, names = {}, {}, {}
    for i, j in pairs.items():
        key = ("linked", i)
        key_a[parts_a[i].index], key_b[parts_b[j].index] = key, key
        names[key] = parts_b[j].designator
    unmatched = set()
    for i, part in enumerate(parts_a):
        if i not in pairs:
            key = ("a", i)
            key_a[part.index], names[key] = key, part.designator
            unmatched.add(key)
    matched_b = set(pairs.values())
    for j, part in enumerate(parts_b):
        if j not in matched_b:
            key = ("b", j)
            key_b[part.index], names[key] = key, part.designator
            unmatched.add(key)

    def label(pad) -> str:
        return f"{names.get(pad[0], '?')}-{pad[1]}"

    members_a, members_b = _members(pcb_a, key_a), _members(pcb_b, key_b)
    names_a = set(pcb_a.nets) | set(members_a)
    names_b = set(pcb_b.nets) | set(members_b)
    to_b = _match_nets(members_a, members_b, names_a, names_b, options)
    nets, from_b = _net_changes(members_a, members_b, names_a, names_b, to_b, label)
    hazards = _net_hazards(members_a, members_b, label, unmatched)
    out += nets + hazards
    out += _rule_changes(pcb_a, pcb_b, units)
    out += _class_changes(pcb_a, pcb_b)
    out += _pair_changes(pcb_a, pcb_b)
    out += _stack_changes(pcb_a.board, pcb_b.board, units)
    routing = _routing_changes(pcb_a, pcb_b, from_b, units)
    out += routing
    totals = {
        "length_delta_mil": _r(sum(e["length_delta_mil"] for e in routing), 1),
        "via_delta": sum(e["vias"]["to"] - e["vias"]["from"] for e in routing if e.get("vias")),
    }
    designators = _designator_hazards(parts_a, parts_b, pairs, how, "pcb")
    return out, designators, totals


def build_design_diff(a: DesignSource, b: DesignSource, units: str | None = None,
                      net_options: dict | None = None) -> dict:
    """The changelog from a to b: summary counts per section, the entries (ICT hazards
    first, then by section) and what was not compared."""
    model: dict = {"a": a.label, "b": b.label}
    entries: list[dict] = []
    notes = list(NOTES)
    skipped: dict[str, str] = {}
    designators: dict[str, dict] = {}

    # Schematic side: components, variants, BOM lines, project.
    if a.schematic_digest == b.schematic_digest and None not in dict(a.schematic_digest).values():
        skipped["schematic"] = "the .PrjPcb, sheets and .Annotation files are identical"
    else:
        comps_a, errors_a = a.schematic()
        comps_b, errors_b = b.schematic()
        if errors_a or errors_b:
            model["sheet_errors"] = {"a": errors_a, "b": errors_b}
        rows_a = _components(comps_a, a.prj.parameters)
        rows_b = _components(comps_b, b.prj.parameters)
        entries += _project_changes(a, b)
        changes, pairs, how = _component_changes(rows_a, rows_b)
        entries += changes
        designators = _designator_hazards(rows_a, rows_b, pairs, how, "schematic")
        entries += _variant_changes(a.prj, b.prj, rows_b)
        bom, same = _bom_changes(comps_a, comps_b, a.prj, b.prj)
        entries += bom
        if same:
            model["bom_variants_same_as_base"] = same

    # Board side.
    resolved_units = units
    if a.board_digest is not None and a.board_digest == b.board_digest and \
            None not in a.board_digest:
        skipped["board"] = "the PcbDoc is identical"
    else:
        pcb_a, why_a = a.board()
        pcb_b, why_b = b.board()
        if pcb_a is None or pcb_b is None:
            skipped["board"] = "; ".join(
                f"{side}: {why}" for side, why in (("a", why_a), ("b", why_b)) if why)
        else:
            resolved_units = units or (pcb_b.board.display_unit or "mil").lower()
            if resolved_units not in ("mil", "mm"):
                resolved_units = "mil"
            board, board_designators, totals = _board_diff(
                a, b, _Units(resolved_units), {**DEFAULT_NET_MATCHING, **(net_options or {})})
            entries += board
            for designator, hazard in board_designators.items():
                if designator in designators:
                    designators[designator]["where"].append("pcb")
                else:
                    designators[designator] = hazard
            model["routing_totals"] = totals
    for designator in sorted(designators, key=natural_key):
        hazard = designators[designator]
        before = _part_label(hazard["before"]["comment"], hazard["before"]["footprint"])
        after = _part_label(hazard["after"]["comment"], hazard["after"]["footprint"])
        moved = (f"; the previous part is now {hazard['previous_part_now']}"
                 if hazard.get("previous_part_now") else "")
        same = hazard["before"] == hazard["after"]
        what = (f"same comment and footprint ({after}): likely deleted and placed again"
                if same else f"was {before}, now {after}")
        entries.append(_entry(
            "ict_hazards", "designator_reused", "Warning",
            f"Designator {designator} is now a different part (UniqueId changed): "
            f"{what}{moved}. A fixture, test program or rework instruction made for the "
            f"old {designator} may no longer match.",
            designator=designator, where=hazard["where"], before=hazard["before"],
            after=hazard["after"], same_comment_and_footprint=same or None,
            previous_part_now=hazard.get("previous_part_now")))

    order = {section: rank for rank, section in enumerate(SECTIONS)}
    entries.sort(key=lambda e: order[e["section"]])  # stable: keeps each section's order
    missing = {side: source.missing for side, source in (("a", a), ("b", b)) if source.missing}
    model["units"] = resolved_units or "mil"
    model["summary"] = _summary(entries)
    model["total_changes"] = len(entries)
    if not entries:
        model["no_changes"] = True
    if skipped:
        model["unchanged_or_not_compared"] = skipped
    if missing:
        model["documents_not_read"] = missing
    model["net_matching"] = {k: v for k, v in (net_options or {}).items()}
    model["notes"] = notes
    model["changes"] = entries
    return model


def page_design_diff(model: dict, offset: int, limit: int, section: str | None = None) -> dict:
    """One page of changes (one section's when section is given); the sources, summary
    and notes go with the first page."""
    offset = max(0, int(offset or 0))
    changes = model["changes"]
    if section:
        changes = [entry for entry in changes if entry["section"] == section]
    if offset:
        result = {key: model[key] for key in ("a", "b", "units") if key in model}
    else:
        result = {key: value for key, value in model.items() if key != "changes"}
    if section:
        result["section"] = section
    result["total"] = len(changes)
    result["offset"] = offset
    result["has_more"] = offset + limit < len(changes)
    if result["has_more"]:
        result["next_offset"] = offset + limit
    result["changes"] = [{k: v for k, v in entry.items() if k != "section" or not section}
                         for entry in changes[offset:offset + limit]]
    return result


# ---------- entry points ----------

_CACHE: dict = {}


def _cached(key: tuple, build) -> dict:
    if _CACHE.get("key") != key:
        _CACHE.clear()
        _CACHE["model"] = build()
        _CACHE["key"] = key
    return _CACHE["model"]


def _source_key(source: DesignSource) -> tuple:
    return (repr(sorted(source.label.items())), source.schematic_digest,
            source.board_digest, tuple(sorted(source.missing.items())))


def _outside_pathspecs(repo: _Repo, project: DesignSource) -> list[str]:
    """Repository paths of the documents the project lists outside its folder."""
    top = repo.top.resolve()
    folder = project.prj_path.parent.resolve()
    paths = []
    for path in [*project.sheets.values(), *project.annotations.values(),
                 *([project.pcb[1]] if project.pcb else [])]:
        resolved = Path(path).resolve()
        try:
            resolved.relative_to(folder)
            continue
        except ValueError:
            pass
        try:
            paths.append(resolved.relative_to(top).as_posix())
        except ValueError:
            continue
    return paths


def diff_revisions(prj_pcb_path: str, rev_a: str | None = None, rev_b: str | None = None,
                   units: str | None = None, git: str | None = None) -> dict:
    """Diff two revisions of the clone holding the project: commits, branches, tags or
    'working' (the files on disk). rev_b defaults to working; rev_a to the last commit
    that changed a design file when rev_b is working, else rev_b's parent. With neither
    given, the recent commits that changed design files are listed too."""
    prj_path = Path(prj_pcb_path)
    repo = _Repo(prj_path.parent, git)
    working = folder_source(prj_path.parent, prj_path.name,
                            {"source": WORKING, "folder": str(prj_path.parent)})
    try:
        pathspecs = repo.pathspecs(_outside_pathspecs(repo, working))
        listing = None
        if rev_a is None and rev_b is None:
            listing = repo.log("HEAD", pathspecs, RECENT_COMMITS)
        rev_b = rev_b or WORKING
        sha_b = None if rev_b.lower() == WORKING else repo.resolve(rev_b)
        if rev_a is None:
            if sha_b is None:
                last = listing if listing is not None else repo.log("HEAD", pathspecs, 1)
                if not last:
                    raise DiffError("no_design_commits",
                                    "No commit in the clone changed the project's design "
                                    "files, so there is nothing to compare with.")
                sha_a = last[0]["hash"]
            else:
                sha_a = repo.parent(sha_b)
                if sha_a is None:
                    raise DiffError("no_parent_commit",
                                    f"{rev_b} is the first commit: pass rev_a to compare "
                                    "it with another revision.")
        else:
            sha_a = None if rev_a.lower() == WORKING else repo.resolve(rev_a)
    except GitError as error:
        raise DiffError("git_failed", str(error)) from error
    # Checked before the folder is made: making it inside the clone would write there.
    temp_root = Path(tempfile.gettempdir()).resolve()
    if temp_root.is_relative_to(repo.top.resolve()):
        raise DiffError("temp_inside_clone", "The temporary folder is inside the clone; "
                        "set TEMP to a folder outside it.")
    temp = Path(tempfile.mkdtemp(prefix="altium-copilot-diff-", dir=str(temp_root))).resolve()
    try:
        try:
            sources = [working if sha is None else git_source(repo, sha, prj_path.name,
                                                               temp / sha[:12])
                       for sha in (sha_a, sha_b)]
        except GitError as error:
            raise DiffError("git_failed", str(error)) from error
        a, b = sources
        options = net_matching_options(str(b.prj_path))
        key = ("git", str(prj_path), _source_key(a), _source_key(b), units,
               tuple(sorted(options.items())))
        try:
            model = dict(_cached(key, lambda: build_design_diff(a, b, units, options)))
        except GitError as error:
            raise DiffError("git_failed", str(error)) from error
    finally:
        shutil.rmtree(temp, ignore_errors=True)
    if listing is not None:
        model["recent_commits"] = listing
        model["recent_commits_note"] = (
            "Commits that changed the project's design files, newest first. Pass a hash "
            "as rev_a (and rev_b, default working) to compare other revisions.")
    if model.get("no_changes") and sha_b is None:
        model["no_changes_hint"] = (
            "The design files on disk match the compared commit: pass an older commit as "
            "rev_a, or rev_b=<commit> to see what that commit changed.")
    return model


def diff_folders(folder_a: str, folder_b: str, prj_name_a: str | None = None,
                 prj_name_b: str | None = None, units: str | None = None) -> dict:
    """Diff the projects in two folders (e.g. an archived release and the working copy)."""
    a = folder_source(folder_a, prj_name_a)
    b = folder_source(folder_b, prj_name_b or a.prj_path.name)
    options = net_matching_options(str(b.prj_path))
    key = ("folders", str(a.prj_path), str(b.prj_path), _source_key(a), _source_key(b),
           units, tuple(sorted(options.items())))
    return dict(_cached(key, lambda: build_design_diff(a, b, units, options)))
