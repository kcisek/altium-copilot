"""Designer-added information for the loaded project, rendered as compact text and as
rows for the package_for_xfn snapshot.

Sources: project parameters (.PrjPcb), per-sheet annotations (.SchDoc notes, text
frames, labels, pictures, directives, title block) and Draftsman documents (.PCBDwf).
Parsed documents are cached by file mtime and size, so a save in Altium is picked up
on the next call without refresh_netlist.
"""
import json
import os
from collections import Counter
from dataclasses import replace
from pathlib import Path, PureWindowsPath

try:
    from parsers.pcb_dwf import DraftsmanDoc, parse_pcb_dwf
    from parsers.sch_doc import (
        SheetAnnotations, SheetHierarchy, SheetIntegrity, parse_sch_doc_annotations,
        parse_sch_hierarchy, parse_sch_integrity,
    )
except ImportError:
    from server.parsers.pcb_dwf import DraftsmanDoc, parse_pcb_dwf
    from server.parsers.sch_doc import (
        SheetAnnotations, SheetHierarchy, SheetIntegrity, parse_sch_doc_annotations,
        parse_sch_hierarchy, parse_sch_integrity,
    )

NOTES_PAGE_CHAR_BUDGET = 60_000
NOTES_MAX_RESULT_SIZE_CHARS = 80_000
SHEET_CONTEXT_NOTES_CHARS = 12_000


class DesignNotesCache:
    """Parse results per (parser, path), reused while the file's mtime and size match."""

    def __init__(self):
        self._entries: dict[tuple[str, str], tuple[tuple[int, int], object]] = {}  # (kind, path)

    def _get(self, kind: str, path: str, parser):
        stat = Path(path).stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        key = (kind, str(path))
        hit = self._entries.get(key)
        if hit is not None and hit[0] == signature:
            return hit[1]
        value = parser(str(path))
        self._entries[key] = (signature, value)
        return value

    def sheet(self, path: str) -> SheetAnnotations:
        return self._get("sheet", path, parse_sch_doc_annotations)

    def draftsman(self, path: str) -> DraftsmanDoc:
        return self._get("draftsman", path, parse_pcb_dwf)

    def hierarchy(self, path: str) -> SheetHierarchy:
        return self._get("hierarchy", path, parse_sch_hierarchy)

    def integrity(self, path: str) -> SheetIntegrity:
        return self._get("integrity", path, parse_sch_integrity)


def draftsman_documents(project: dict) -> list[dict]:
    """[{name, path}] for the Draftsman documents listed in the project."""
    return [{"name": Path(p).name, "path": p} for p in project.get("pcb_dwf_paths") or []]


def resolve_document(project: dict, document: str) -> tuple[str, dict] | None:
    """("sheet" | "draftsman", {name, path}) for a sheet name or Draftsman file name/stem."""
    wanted = document.strip().lower()
    for sheet in project.get("sheets") or []:
        if sheet["name"].lower() == wanted or Path(sheet["path"]).name.lower() == wanted:
            return "sheet", sheet
    for doc in draftsman_documents(project):
        if doc["name"].lower() == wanted or Path(doc["path"]).stem.lower() == wanted:
            return "draftsman", doc
    return None


def _indent_text(text: str, prefix: str) -> str:
    """First line as-is, continuation lines indented under it."""
    return text.replace("\n", "\n" + " " * len(prefix))


def _fmt_mil(value: float) -> str:
    return f"{value:.0f}"


def _split_plus(expression: str) -> list[str]:
    """Split a special-string expression on "+" outside quoted literals."""
    parts, current, quote = [], "", None
    for ch in expression:
        if quote:
            current += ch
            quote = None if ch == quote else quote
        elif ch in "'\"":
            quote = ch
            current += ch
        elif ch == "+":
            parts.append(current)
            current = ""
        else:
            current += ch
    return parts + [current]


def _unset(value: str) -> bool:
    return value in ("", "*")  # "*" is Altium's placeholder for a parameter not filled in


def resolve_special_string(text: str, *parameter_sets: dict[str, str], depth: int = 0) -> str | None:
    """The value Altium shows for a special string, or None if it cannot be worked out.

    A special string is "=Name" or a "+" expression of names and quoted literals
    ("=PrjName + '-v' + Revision"). Names are matched case-insensitively against the
    parameter sets in order (the sheet's own before the project's); a set whose value is
    "*" or empty does not count, so the next one is used. A value that is itself a
    special string ("Revision" = "=SCH_Rev") is followed. A name that only has unset
    values shows as nothing. None when the text is not a special string or uses a name
    no set has.
    """
    if not text.startswith("=") or depth > 5:
        return None
    lookup: dict[str, str] = {}
    for params in parameter_sets:
        for key, value in params.items():
            if not _unset(value):
                lookup.setdefault(key.lower(), value)
    known = {key.lower() for params in parameter_sets for key in params}
    out = []
    for part in (p.strip() for p in _split_plus(text[1:])):
        if len(part) >= 2 and part[0] == part[-1] and part[0] in "'\"":
            out.append(part[1:-1])
            continue
        value = lookup.get(part.lower())
        if value is None and part.lower() in known:
            value = ""  # defined, but nowhere filled in
        elif value is not None and value.startswith("="):
            value = resolve_special_string(value, *parameter_sets, depth=depth + 1)
        if value is None:
            return None
        out.append(value)
    return "".join(out)


# Sheet parameters Altium computes when it draws them; the stored record is not the value.
_COMPUTED_PARAMETERS = {
    "currenttime", "currentdate", "time", "date", "documentfullpathandname", "documentname",
    "modifieddate", "application_buildnumber", "projectname",
}


def resolved_sheet(notes: SheetAnnotations, project: dict, sheet_path: str) -> SheetAnnotations:
    """The sheet's notes with special strings ("=Revision", "=A + '-' + B") replaced by
    what Altium displays: label, text-frame and note text, and title-block values.

    Names resolve against the sheet's parameters, then the project's, then the values
    Altium computes itself that are known here (ProjectName, DocumentName). A text that
    resolves to nothing is dropped, as nothing is drawn on the sheet; one that cannot be
    resolved is kept as typed.
    """
    sheet = {k: v for k, v in notes.sheet_parameters.items() if k.lower() not in _COMPUTED_PARAMETERS}
    built_in = {"DocumentName": Path(sheet_path).name}
    if project.get("prj_pcb_path"):
        built_in["ProjectName"] = Path(project["prj_pcb_path"]).name
    sets = (sheet, project.get("parameters") or {}, built_in)

    def shown(text: str) -> str:
        resolved = resolve_special_string(text, *sets)
        return text if resolved is None else resolved

    items = []
    for item in notes.items:
        if item.kind != "image" and item.text.startswith("="):
            text = shown(item.text)
            if not text.strip():
                continue
            item = replace(item, text=text)
        items.append(item)
    return replace(notes, items=items,
                   title_block={k: shown(v) for k, v in notes.title_block.items()})


def render_sheet_notes(name: str, notes: SheetAnnotations) -> str:
    """One sheet's notes as text; pass them through resolved_sheet first."""
    items = notes.items
    texts = [i for i in items if i.kind != "image"]
    images = [i for i in items if i.kind == "image"]
    lines = [
        f"sheet:{name} notes:{len(texts)} images:{len(images)} directives:{len(notes.directives)}"
    ]
    if notes.title_block:
        lines.append("  title_block: " + "; ".join(f"{k}={v}" for k, v in notes.title_block.items()))
    if notes.template:
        lines.append(f"  template: {PureWindowsPath(notes.template).name}")
    for item in items:
        if item.kind == "image":
            source = "embedded" if item.embedded else "linked"
            line = (
                f"  [image {item.image_index}] {source} \"{item.file_name or ''}\" "
                f"{_fmt_mil(item.width_mil)}x{_fmt_mil(item.height_mil)} mil "
                f"at ({_fmt_mil(item.x_mil)},{_fmt_mil(item.y_mil)})"
            )
            if item.captions:
                line += " captions: " + " | ".join(c.replace("\n", " ") for c in item.captions)
            line += f" -> get_design_image(document='{name}', index={item.image_index})"
            lines.append(line)
            continue
        if item.kind == "note":
            tag = "[note"
            if item.author:
                tag += f" by {item.author}"
            if item.collapsed:
                tag += ", collapsed"
            tag += "] "
        else:
            tag = f"[{item.kind}] "
        prefix = "  " + tag
        line = prefix + _indent_text(item.text, prefix)
        if item.near:
            line += f" (near {', '.join(item.near)})"
        lines.append(line)
    if notes.directives:
        groups: Counter = Counter()
        for directive in notes.directives:
            params = ", ".join(f"{k}={v}" for k, v in directive.parameters.items())
            groups[directive.name + (f" [{params}]" if params else "")] += 1
        lines.append("  directives: " + "; ".join(
            f"{label} x{count}" if count > 1 else label for label, count in groups.items()
        ))
    return "\n".join(lines)


def _same_path(a: str, b: str) -> bool:
    if a.replace("\\", "/").lower().rstrip("/") == b.replace("\\", "/").lower().rstrip("/"):
        return True
    try:  # the same file reached through a junction, subst or mapped drive
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        return False


def _source_warning(source_pcb: str, project_pcb_paths: list[str]) -> str | None:
    """Why the board a drawing was last updated from may not be this project's, or None."""
    if any(_same_path(source_pcb, p) for p in project_pcb_paths):
        return None
    names = {PureWindowsPath(p).name.lower() for p in project_pcb_paths}
    if PureWindowsPath(source_pcb).name.lower() in names:
        return ("(a board of the same name in another folder — a copied project or an earlier "
                "revision; the drawing may be out of date)")
    return "(not this project's PcbDoc — the drawing may be out of date)"


def _utc(timestamp: str) -> str:
    """Draftsman's LastUpdate ("...T16:04:28.6774825Z") to the second, keeping the UTC "Z"."""
    return timestamp[:19] + ("Z" if timestamp.endswith("Z") else "")


def render_draftsman(name: str, doc: DraftsmanDoc, project_pcb_paths: list[str]) -> str:
    header = f"draftsman:{name} pages:{len(doc.pages)}"
    if doc.source_pcb:
        header += f" source_pcb:{doc.source_pcb}"
        warning = _source_warning(doc.source_pcb, project_pcb_paths)
        if warning:
            header += " " + warning
    if doc.last_update:
        header += f" last_update:{_utc(doc.last_update)}"
    lines = [header]
    if doc.parameters:
        lines.append("  parameters: " + "; ".join(f"{k}={v}" for k, v in doc.parameters.items()))
    for page_no, size in enumerate(doc.pages, start=1):
        items = [i for i in doc.items if i.page == page_no]
        if not items:
            continue
        lines.append(f"  page {page_no} ({size}):")
        for item in items:
            template = " (template)" if item.from_template else ""
            if item.kind == "note":
                lines.append(f"    [note{template}] {item.title or ''}".rstrip())
                lines.extend(
                    "      " + _indent_text(f"{n}. {text}", "      ")
                    for n, text in enumerate(item.lines, start=1)
                )
            elif item.kind == "text":
                prefix = f"    [text{template}] "
                lines.append(prefix + _indent_text(item.text or "", prefix))
            elif item.kind == "table":
                lines.append(f"    [table] {item.title or ''}".rstrip())
                lines.extend("      " + " | ".join(row) for row in item.rows)
            elif item.kind == "picture":
                size_text = (
                    f" {item.width_mm:.0f}x{item.height_mm:.0f} mm"
                    if item.width_mm and item.height_mm else ""
                )
                lines.append(
                    f"    [picture {item.picture_index}]{size_text}"
                    f" -> get_design_image(document='{name}', index={item.picture_index})"
                )
            elif item.kind == "view":
                side = f", {item.side} side" if item.side else ""
                layer = f", layer {item.layer}" if item.layer and item.layer not in (item.title or "") else ""
                title = f": {item.title}" if item.title else ""
                lines.append(f"    [view] {item.view}{side}{layer}{title}")
    if doc.drill_table:
        lines.append("  drill_table (cached from the board at last_update):")
        lines.extend("    " + _drill_text(row) for row in doc.drill_table)
    if doc.layer_notes:
        lines.append("  layer_stack_notes:")
        for layer in doc.layer_notes:
            lines.append(f"    {layer['layer']}: {_layer_note_text(layer)}")
    return "\n".join(lines)


def _drill_text(row: dict) -> str:
    size = f"{row['size_mm']:.3f}" if row["size_mm"] is not None else "?"
    text = (
        f"{size} mm {'plated' if row['plated'] else 'non-plated'} "
        f"{row['type']} {row['span']} x{row['count']}"
    )
    if row.get("slot_length_mm"):
        text += f" slot {row['slot_length_mm']:.3f} mm"
    if row.get("backdrill"):
        text += " backdrill"
    return text


def _layer_note_text(layer: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in layer.items() if k != "layer")


def _note_row(document_kind: str, document: str, kind: str, *, text: str | None = "",
              title: str | None = None, page: int | None = None, author: str | None = None,
              near: list[str] | None = None, data: dict | None = None) -> dict:
    return {
        "document_kind": document_kind, "document": document, "kind": kind, "page": page,
        "title": title, "text": text or "", "author": author,
        "near_refdes": list(near or []), "data": data or {},
    }


def _file_name(path: str | None) -> str | None:
    """The last part of a Windows path. The snapshot is shared, so it keeps no local
    folders (user profiles, drive layouts); pasted-picture names like
    "ClipboardPicture03/06/2025 14:30" have no backslash and stay whole."""
    return path.rsplit("\\", 1)[-1] if path else path


def _sheet_rows(name: str, notes: SheetAnnotations) -> list[dict]:
    rows = []
    for item in notes.items:
        data = {"x_mil": item.x_mil, "y_mil": item.y_mil,
                "width_mil": item.width_mil, "height_mil": item.height_mil}
        if item.kind == "image":
            file_name = _file_name(item.file_name)
            data.update(file_name=file_name, embedded=item.embedded,
                        image_index=item.image_index, captions=item.captions)
            rows.append(_note_row("sheet", name, "image", title=file_name,
                                  text="\n".join(item.captions), near=item.near, data=data))
            continue
        if item.kind == "note":
            data["collapsed"] = item.collapsed
        rows.append(_note_row("sheet", name, item.kind, text=item.text, author=item.author,
                              near=item.near, data=data))
    for directive in notes.directives:
        rows.append(_note_row(
            "sheet", name, "directive", title=directive.name,
            text=", ".join(f"{k}={v}" for k, v in directive.parameters.items()),
            data={"parameters": directive.parameters,
                  "x_mil": directive.x_mil, "y_mil": directive.y_mil},
        ))
    rows += [_note_row("sheet", name, "title_block", title=k, text=v)
             for k, v in notes.title_block.items()]
    if notes.template:
        rows.append(_note_row("sheet", name, "template", text=_file_name(notes.template)))
    return rows


def _draftsman_rows(name: str, doc: DraftsmanDoc, project_pcb_paths: list[str]) -> list[dict]:
    source = _file_name(doc.source_pcb)
    rows = [_note_row("draftsman", name, "drawing", text=source, data={
        "source_pcb": source,
        "matches_project_pcb": bool(doc.source_pcb) and any(
            _same_path(doc.source_pcb, p) for p in project_pcb_paths),
        "source_warning": _source_warning(doc.source_pcb, project_pcb_paths) if doc.source_pcb else None,
        "last_update": doc.last_update,
        "pages": doc.pages,
    })]
    rows += [_note_row("draftsman", name, "parameter", title=k, text=v)
             for k, v in doc.parameters.items()]
    for item in doc.items:
        data = {"from_template": item.from_template}
        if item.kind == "note":
            data["lines"] = item.lines
            text = "\n".join(f"{n}. {line}" for n, line in enumerate(item.lines, start=1))
        elif item.kind == "table":
            data["rows"] = item.rows
            text = "\n".join(" | ".join(row) for row in item.rows)
        elif item.kind == "picture":
            data.update(picture_index=item.picture_index,
                        width_mm=item.width_mm, height_mm=item.height_mm)
            text = ""
        elif item.kind == "view":
            data.update(view=item.view, side=item.side, layer=item.layer)
            text = item.view + (f", {item.side} side" if item.side else "")
        else:
            text = item.text
        rows.append(_note_row("draftsman", name, item.kind, title=item.title, text=text,
                              page=item.page, data=data))
    rows += [_note_row("draftsman", name, "drill", text=_drill_text(row), data=row)
             for row in doc.drill_table]
    rows += [_note_row("draftsman", name, "layer_note", title=layer["layer"],
                       text=_layer_note_text(layer), data=layer)
             for layer in doc.layer_notes]
    return rows


def note_rows(cache: DesignNotesCache, project: dict) -> tuple[list[dict], list[dict]]:
    """The package_for_xfn notes table: what get_design_notes reports, one row per item.

    Pictures are described (file name, size, index) without their image data, and paths
    are reduced to file names. A document that could not be read gets one "unreadable"
    row, so a reader can tell it apart from a document without notes. Returns (rows,
    errors); errors list [{document_kind, document, error}] for those documents, so the
    export can go ahead without them.
    """
    rows = [_note_row("project", project["name"], "parameter", title=k, text=v)
            for k, v in (project.get("parameters") or {}).items()]
    errors = []
    documents = [("sheet", s) for s in project.get("sheets") or []]
    documents += [("draftsman", d) for d in draftsman_documents(project)]
    for kind, doc in documents:
        try:
            if kind == "sheet":
                rows += _sheet_rows(doc["name"],
                                    resolved_sheet(cache.sheet(doc["path"]), project, doc["path"]))
            else:
                rows += _draftsman_rows(doc["name"], cache.draftsman(doc["path"]),
                                        project.get("pcb_doc_paths") or [])
        except Exception as e:
            errors.append({"document_kind": kind, "document": doc["name"],
                           "error": f"{type(e).__name__}: {e}"})
            reason = e.strerror if isinstance(e, OSError) and e.strerror else str(e)
            reason = reason.replace(doc["path"], Path(doc["path"]).name)
            rows.append(_note_row(kind, doc["name"], "unreadable", text=f"{type(e).__name__}: {reason}"))
    return rows, errors


def render_project_parameters(project: dict) -> str | None:
    params = project.get("parameters") or {}
    if not params:
        return None
    return f"project:{project['name']} parameters: " + "; ".join(f"{k}={v}" for k, v in params.items())


def _section(cache: DesignNotesCache, project: dict, kind: str, doc: dict) -> str:
    try:
        if kind == "sheet":
            return render_sheet_notes(doc["name"],
                                      resolved_sheet(cache.sheet(doc["path"]), project, doc["path"]))
        return render_draftsman(doc["name"], cache.draftsman(doc["path"]),
                                project.get("pcb_doc_paths") or [])
    except Exception as e:
        return f"{kind}:{doc['name']} error: notes could not be read ({type(e).__name__}: {e})"


def sheet_notes_for_context(cache: DesignNotesCache, project: dict, sheet_name: str) -> str | None:
    """The notes block get_sheet_context puts above the components, or None if empty."""
    sheet = next((s for s in project.get("sheets") or [] if s["name"] == sheet_name), None)
    if sheet is None:
        return None
    text = _section(cache, project, "sheet", sheet)
    if "\n" not in text and " error: " not in text:
        return None  # header only: nothing designer-added on this sheet
    if len(text) > SHEET_CONTEXT_NOTES_CHARS:
        text = (
            text[:SHEET_CONTEXT_NOTES_CHARS].rsplit("\n", 1)[0]
            + f"\n  ... truncated — call get_design_notes(document='{sheet_name}') for all notes"
        )
    return "design_notes:\n" + text


def build_design_notes(cache: DesignNotesCache, project: dict, document: str | None = None,
                       offset: int = 0) -> str:
    """Project parameters, then every sheet, then every Draftsman document, paginated.

    One section per document; a page holds as many sections as fit in the character
    budget (at least one, so the offset always advances).
    """
    if document:
        resolved = resolve_document(project, document)
        if resolved is None:
            available = [s["name"] for s in project.get("sheets") or []] + [
                d["name"] for d in draftsman_documents(project)
            ]
            return json.dumps({
                "error": "document_not_found",
                "message": f"'{document}' is not a sheet or Draftsman document of this project. "
                           f"Available: {available}",
            })
        sections = [_section(cache, project, *resolved)]
    else:
        sections = []
        params = render_project_parameters(project)
        if params:
            sections.append(params)
        sections += [_section(cache, project, "sheet", s) for s in project.get("sheets") or []]
        sections += [_section(cache, project, "draftsman", d) for d in draftsman_documents(project)]

    offset = max(0, offset)
    total = len(sections)
    if offset >= total and total > 0:
        return f"design_notes total:{total} offset:{offset} has_more:false\nwarning:offset {offset} >= total {total}"
    page: list[str] = []
    chars = 0
    for text in sections[offset:]:
        if page and chars + len(text) > NOTES_PAGE_CHAR_BUDGET:
            break
        page.append(text)
        chars += len(text)
    next_offset = offset + len(page)
    has_more = next_offset < total
    header = f"design_notes total:{total} offset:{offset} has_more:{'true' if has_more else 'false'}"
    if has_more:
        doc_arg = f"document='{document}', " if document else ""
        header += f"\nnext:get_design_notes({doc_arg}offset={next_offset}) [{total - next_offset} remaining]"
    if not project.get("pcb_dwf_paths") and not document:
        header += "\nnote: the project lists no Draftsman (.PCBDwf) documents"
    return header + "\n\n" + "\n\n".join(page)
