"""Designer-added information for the loaded project, rendered as compact text.

Sources: project parameters (.PrjPcb), per-sheet annotations (.SchDoc notes, text
frames, labels, pictures, directives, title block) and Draftsman documents (.PCBDwf).
Parsed documents are cached by file mtime and size, so a save in Altium is picked up
on the next call without refresh_netlist.
"""
from collections import Counter
from pathlib import Path, PureWindowsPath

try:
    from parsers.pcb_dwf import DraftsmanDoc, parse_pcb_dwf
    from parsers.sch_doc import SheetAnnotations, parse_sch_doc_annotations
except ImportError:
    from server.parsers.pcb_dwf import DraftsmanDoc, parse_pcb_dwf
    from server.parsers.sch_doc import SheetAnnotations, parse_sch_doc_annotations

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


def render_sheet_notes(name: str, notes: SheetAnnotations) -> str:
    texts = [i for i in notes.items if i.kind != "image"]
    images = [i for i in notes.items if i.kind == "image"]
    lines = [
        f"sheet:{name} notes:{len(texts)} images:{len(images)} directives:{len(notes.directives)}"
    ]
    if notes.title_block:
        lines.append("  title_block: " + "; ".join(f"{k}={v}" for k, v in notes.title_block.items()))
    if notes.template:
        lines.append(f"  template: {PureWindowsPath(notes.template).name}")
    for item in notes.items:
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
    return a.replace("\\", "/").lower().rstrip("/") == b.replace("\\", "/").lower().rstrip("/")


def render_draftsman(name: str, doc: DraftsmanDoc, project_pcb_paths: list[str]) -> str:
    header = f"draftsman:{name} pages:{len(doc.pages)}"
    if doc.source_pcb:
        header += f" source_pcb:{doc.source_pcb}"
        if not any(_same_path(doc.source_pcb, p) for p in project_pcb_paths):
            header += " (not this project's PcbDoc — the drawing may be out of date)"
    if doc.last_update:
        header += f" last_update:{doc.last_update[:19]}"
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
                title = f": {item.title}" if item.title else ""
                lines.append(f"    [view] {item.view}{side}{title}")
    if doc.drill_table:
        lines.append("  drill_table (cached from the board at last_update):")
        for row in doc.drill_table:
            line = (
                f"    {row['size_mm']:.3f} mm {'plated' if row['plated'] else 'non-plated'} "
                f"{row['type']} {row['span']} x{row['count']}"
            )
            if row.get("slot_length_mm"):
                line += f" slot {row['slot_length_mm']:.3f} mm"
            if row.get("backdrill"):
                line += " backdrill"
            lines.append(line)
    if doc.layer_notes:
        lines.append("  layer_stack_notes:")
        for layer in doc.layer_notes:
            details = "; ".join(f"{k}={v}" for k, v in layer.items() if k != "layer")
            lines.append(f"    {layer['layer']}: {details}")
    return "\n".join(lines)


def render_project_parameters(project: dict) -> str | None:
    params = project.get("parameters") or {}
    if not params:
        return None
    return f"project:{project['name']} parameters: " + "; ".join(f"{k}={v}" for k, v in params.items())


def _section(cache: DesignNotesCache, project: dict, kind: str, doc: dict) -> str:
    try:
        if kind == "sheet":
            return render_sheet_notes(doc["name"], cache.sheet(doc["path"]))
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
            return (
                f"error: document_not_found — '{document}' is not a sheet or Draftsman "
                f"document of this project. Available: {available}"
            )
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
