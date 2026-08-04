"""Build a .docx from one of the HTML documents in this folder.

Standard library only: the HTML is parsed into a small tree and emitted as
WordprocessingML inside an OOXML zip package. Mermaid diagrams are replaced by
the PNGs produced by `node docs/export-diagrams.mjs`, which must be run first
for any document that contains diagrams.

Usage:
    node docs/export-diagrams.mjs                        # only if it has diagrams
    python docs/generate-architecture-docx.py [basename]  # default: architecture
"""

from __future__ import annotations

import html
import struct
import sys
import zipfile
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

DOCS = Path(__file__).resolve().parent

# Which document is being built; set from the optional CLI argument in main().
STEM = "architecture"

EMU_PER_INCH = 914400
DIAGRAM_SCALE = 3          # deviceScaleFactor used by export-diagrams.mjs
MAX_IMG_W_IN = 6.3
MAX_IMG_H_IN = 8.4
CONTENT_WIDTH_DXA = 9638   # A4 minus 2cm margins, in twips

INK = "1A2332"
MUTED = "5A6570"
ACCENT = "2C5F7C"
ACCENT_SOFT = "E8F1F8"
LINE = "D5DDE5"
WARN = "8A5A00"
OK = "1F6B4A"


# ── HTML → tree ────────────────────────────────────────────────────────────────

VOID = {"br", "img", "meta", "link", "hr"}


class Node:
    __slots__ = ("tag", "attrs", "children", "text")

    def __init__(self, tag: str, attrs: dict | None = None, text: str = ""):
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list[Node] = []
        self.text = text

    def cls(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())


class TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self.stack = [self.root]
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if self.skip_depth or tag in ("script", "style", "head"):
            if tag not in VOID:
                self.skip_depth += 1
            return
        node = Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        if self.skip_depth:
            self.skip_depth -= 1
            return
        if tag in VOID:
            return
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if self.skip_depth or not data.strip():
            # Keep a single space so inline tags don't run together.
            if not self.skip_depth and data and self.stack[-1].tag not in ("#root", "html", "body"):
                self.stack[-1].children.append(Node("#text", text=" "))
            return
        self.stack[-1].children.append(Node("#text", text=data))


def parse_html(path: Path) -> Node:
    builder = TreeBuilder()
    builder.feed(path.read_text(encoding="utf-8"))
    return builder.root


def find(node: Node, tag: str) -> Node | None:
    if node.tag == tag:
        return node
    for c in node.children:
        hit = find(c, tag)
        if hit:
            return hit
    return None


# ── XML helpers ────────────────────────────────────────────────────────────────

def esc(text: str) -> str:
    return html.escape(text, quote=False)


def collapse(text: str) -> str:
    return " ".join(text.split())


def run(text: str, *, bold=False, italic=False, code=False, color="", size=0) -> str:
    props = []
    if code:
        props.append('<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/>')
    if bold:
        props.append("<w:b/>")
    if italic:
        props.append("<w:i/>")
    if color:
        props.append(f'<w:color w:val="{color}"/>')
    if size:
        props.append(f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>')
    if code:
        props.append(f'<w:shd w:val="clear" w:color="auto" w:fill="EEF2F6"/>')
    rpr = f"<w:rPr>{''.join(props)}</w:rPr>" if props else ""
    return f'<w:r>{rpr}<w:t xml:space="preserve">{esc(text)}</w:t></w:r>'


def para(runs: str, ppr: str = "") -> str:
    body = f"<w:pPr>{ppr}</w:pPr>" if ppr else ""
    return f"<w:p>{body}{runs}</w:p>"


# ── Inline rendering ───────────────────────────────────────────────────────────

class Fmt:
    __slots__ = ("bold", "italic", "code")

    def __init__(self, bold=False, italic=False, code=False):
        self.bold, self.italic, self.code = bold, italic, code

    def with_(self, **kw) -> "Fmt":
        return Fmt(
            kw.get("bold", self.bold),
            kw.get("italic", self.italic),
            kw.get("code", self.code),
        )


def inline(node: Node, fmt: Fmt, color: str = "", size: int = 0) -> list[tuple[str, Fmt]]:
    """Flatten a node's inline content into (text, format) pairs."""
    out: list[tuple[str, Fmt]] = []
    for child in node.children:
        if child.tag == "#text":
            out.append((collapse(child.text) if child.text.strip() else " ", fmt))
        elif child.tag in ("strong", "b"):
            out.extend(inline(child, fmt.with_(bold=True)))
        elif child.tag in ("em", "i"):
            out.extend(inline(child, fmt.with_(italic=True)))
        elif child.tag == "code":
            out.extend(inline(child, fmt.with_(code=True)))
        elif child.tag == "br":
            out.append(("\n", fmt))
        else:
            out.extend(inline(child, fmt))
    return out


def inline_runs(node: Node, color: str = "", size: int = 0) -> str:
    pairs = inline(node, Fmt())
    # Merge whitespace-only fragments into their neighbours, then trim the ends.
    merged: list[list] = []
    for text, fmt in pairs:
        if not text:
            continue
        if merged and merged[-1][0].endswith(" ") and text.startswith(" "):
            text = text.lstrip(" ")
            if not text:
                continue
        merged.append([text, fmt])
    if not merged:
        return ""
    merged[0][0] = merged[0][0].lstrip()
    merged[-1][0] = merged[-1][0].rstrip()

    parts = []
    for text, fmt in merged:
        if not text:
            continue
        if "\n" in text:
            chunks = text.split("\n")
            for idx, chunk in enumerate(chunks):
                if idx:
                    parts.append("<w:r><w:br/></w:r>")
                if chunk:
                    parts.append(run(chunk, bold=fmt.bold, italic=fmt.italic,
                                     code=fmt.code, color=color, size=size))
        else:
            parts.append(run(text, bold=fmt.bold, italic=fmt.italic,
                             code=fmt.code, color=color, size=size))
    return "".join(parts)


def plain_text(node: Node) -> str:
    return collapse("".join(t for t, _ in inline(node, Fmt())))


# ── Images ─────────────────────────────────────────────────────────────────────

def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()[:33]
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path} is not a PNG")
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def image_paragraph(path: Path, rel_id: str, doc_pr_id: int) -> str:
    px_w, px_h = png_size(path)
    in_w = px_w / (96 * DIAGRAM_SCALE)
    in_h = px_h / (96 * DIAGRAM_SCALE)
    scale = min(MAX_IMG_W_IN / in_w, MAX_IMG_H_IN / in_h, 1.0)
    cx = int(in_w * scale * EMU_PER_INCH)
    cy = int(in_h * scale * EMU_PER_INCH)
    drawing = (
        "<w:r><w:drawing>"
        '<wp:inline distT="0" distB="0" distL="0" distR="0">'
        f'<wp:extent cx="{cx}" cy="{cy}"/>'
        '<wp:effectExtent l="0" t="0" r="0" b="0"/>'
        f'<wp:docPr id="{doc_pr_id}" name="Diagram {doc_pr_id}"/>'
        '<wp:cNvGraphicFramePr><a:graphicFrameLocks noChangeAspect="1"/></wp:cNvGraphicFramePr>'
        "<a:graphic><a:graphicData "
        'uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        "<pic:pic><pic:nvPicPr>"
        f'<pic:cNvPr id="{doc_pr_id}" name="{path.name}"/><pic:cNvPicPr/>'
        "</pic:nvPicPr>"
        f'<pic:blipFill><a:blip r:embed="{rel_id}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
        f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>'
        "</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>"
    )
    # CT_PPr requires spacing before jc.
    ppr = '<w:spacing w:before="180" w:after="60"/><w:jc w:val="center"/>'
    return para(drawing, ppr)


# ── Tables ─────────────────────────────────────────────────────────────────────

BORDER = (
    "<w:tblBorders>"
    + "".join(
        f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="{LINE}"/>'
        for side in ("top", "left", "bottom", "right", "insideH", "insideV")
    )
    + "</w:tblBorders>"
)


def column_widths(rows: list[list[Node]]) -> list[int]:
    """Weight columns by mean content length, clamped so no column collapses."""
    cols = max(len(r) for r in rows)
    totals = [0] * cols
    counts = [0] * cols
    for row in rows:
        for i, cell in enumerate(row[:cols]):
            totals[i] += len(plain_text(cell))
            counts[i] += 1
    means = [max(totals[i] / max(counts[i], 1), 1.0) for i in range(cols)]
    lo, hi = 0.09, 0.52
    share = [m / sum(means) for m in means]
    share = [min(max(s, lo), hi) for s in share]
    share = [s / sum(share) for s in share]
    widths = [int(CONTENT_WIDTH_DXA * s) for s in share]
    widths[-1] += CONTENT_WIDTH_DXA - sum(widths)
    return widths


def render_table(table: Node) -> str:
    rows: list[tuple[bool, list[Node]]] = []
    for section in table.children:
        if section.tag in ("thead", "tbody", "tfoot"):
            for tr in section.children:
                if tr.tag == "tr":
                    cells = [c for c in tr.children if c.tag in ("td", "th")]
                    rows.append((section.tag == "thead", cells))
        elif section.tag == "tr":
            cells = [c for c in section.children if c.tag in ("td", "th")]
            rows.append((any(c.tag == "th" for c in cells), cells))
    if not rows:
        return ""

    widths = column_widths([cells for _, cells in rows])
    grid = "".join(f'<w:gridCol w:w="{w}"/>' for w in widths)

    xml = [
        "<w:tbl><w:tblPr>",
        f'<w:tblW w:w="{CONTENT_WIDTH_DXA}" w:type="dxa"/>',
        BORDER,
        '<w:tblLayout w:type="fixed"/>',
        '<w:tblCellMar><w:top w:w="70" w:type="dxa"/><w:left w:w="100" w:type="dxa"/>'
        '<w:bottom w:w="70" w:type="dxa"/><w:right w:w="100" w:type="dxa"/></w:tblCellMar>',
        f"</w:tblPr><w:tblGrid>{grid}</w:tblGrid>",
    ]

    for is_header, cells in rows:
        trpr = '<w:trPr><w:cantSplit/><w:tblHeader/></w:trPr>' if is_header else ""
        xml.append(f"<w:tr>{trpr}")
        for i, cell in enumerate(cells):
            width = widths[i] if i < len(widths) else widths[-1]
            # A th inside a body row is a row header, as in key-value tables.
            head_cell = is_header or cell.tag == "th"
            shd = f'<w:shd w:val="clear" w:color="auto" w:fill="{ACCENT_SOFT}"/>' if head_cell else ""
            runs = (
                inline_runs_bold(cell) if head_cell
                else inline_runs(cell, size=19)
            )
            ppr = '<w:spacing w:before="20" w:after="20" w:line="240" w:lineRule="auto"/>'
            xml.append(
                f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/>{shd}'
                '<w:vAlign w:val="top"/></w:tcPr>'
                + para(runs or run(""), ppr)
                + "</w:tc>"
            )
        xml.append("</w:tr>")
    xml.append("</w:tbl>")
    # Word needs a paragraph after a table so adjacent tables don't merge.
    xml.append(para("", '<w:spacing w:after="120"/>'))
    return "".join(xml)


def inline_runs_bold(cell: Node) -> str:
    pairs = inline(cell, Fmt(bold=True))
    parts = [
        run(collapse(t), bold=True, italic=f.italic, code=f.code, color=ACCENT, size=19)
        for t, f in pairs
        if collapse(t)
    ]
    return "".join(parts)


# ── Block rendering ────────────────────────────────────────────────────────────

class Renderer:
    def __init__(self):
        self.body: list[str] = []
        self.images: list[Path] = []
        self.ordered_lists = 0

    def add(self, xml: str):
        if xml:
            self.body.append(xml)

    def walk(self, node: Node):
        for child in node.children:
            self.block(child)

    def block(self, node: Node):
        tag = node.tag
        classes = node.cls()

        if tag == "#text":
            return
        if tag == "h1":
            self.add(para(inline_runs(node), '<w:pStyle w:val="Title"/>'))
        elif tag == "h2":
            self.add(para(inline_runs(node), '<w:pStyle w:val="Heading1"/>'))
        elif tag == "h3":
            self.add(para(inline_runs(node), '<w:pStyle w:val="Heading2"/>'))
        elif tag == "p":
            if "caption" in classes:
                self.add(para(inline_runs(node), '<w:pStyle w:val="Caption"/>'))
            elif "muted" in classes:
                self.add(para(inline_runs(node, color=MUTED)))
            else:
                self.add(para(inline_runs(node)))
        elif tag == "div" and "diagram" in classes:
            index = len(self.images) + 1
            path = DOCS / f"{STEM}-diagram-{index}.png"
            if not path.exists():
                raise FileNotFoundError(
                    f"{path.name} missing — run `node docs/export-diagrams.mjs` first"
                )
            self.images.append(path)
            self.add(image_paragraph(path, f"rIdImg{index}", 100 + index))
        elif tag == "div" and "callout" in classes:
            self.add(self.callout(node, classes))
        elif tag in ("div", "section", "header", "main"):
            self.walk(node)
        elif tag == "ul":
            self.list(node, ordered=False)
        elif tag == "ol":
            self.list(node, ordered=True)
        elif tag == "table":
            self.add(render_table(node))
        elif tag == "footer":
            self.add(para("", '<w:pBdr><w:top w:val="single" w:sz="6" w:space="8" '
                              f'w:color="{LINE}"/></w:pBdr><w:spacing w:before="360"/>'))
            self.add(para(inline_runs(node, color=MUTED, size=18)))
        else:
            self.walk(node)

    def callout(self, node: Node, classes: set[str]) -> str:
        fill, border = ACCENT_SOFT, ACCENT
        if "warn" in classes:
            fill, border = "FDF6E7", WARN
        elif "ok" in classes:
            fill, border = "EAF6F0", OK
        ppr = (
            f'<w:pBdr><w:left w:val="single" w:sz="18" w:space="8" w:color="{border}"/></w:pBdr>'
            f'<w:shd w:val="clear" w:color="auto" w:fill="{fill}"/>'
            '<w:spacing w:before="200" w:after="200"/>'
            '<w:ind w:left="170" w:right="140"/>'
        )
        return para(inline_runs(node), ppr)

    def list(self, node: Node, *, ordered: bool):
        if ordered:
            self.ordered_lists += 1
            num_id = 1 + self.ordered_lists
        else:
            num_id = 1
        for item in node.children:
            if item.tag != "li":
                continue
            ppr = (
                '<w:pStyle w:val="ListParagraph"/>'
                f'<w:numPr><w:ilvl w:val="0"/><w:numId w:val="{num_id}"/></w:numPr>'
                '<w:spacing w:before="40" w:after="100"/>'
            )
            self.add(para(inline_runs(item), ppr))


# ── Package parts ──────────────────────────────────────────────────────────────

DOC_NS = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"'
)

XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'


def styles_xml() -> str:
    def heading(style_id, name, outline, size, color, before, after, border=False):
        bdr = (
            f'<w:pBdr><w:bottom w:val="single" w:sz="6" w:space="6" w:color="{LINE}"/></w:pBdr>'
            if border else ""
        )
        return (
            f'<w:style w:type="paragraph" w:styleId="{style_id}">'
            f'<w:name w:val="{name}"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/>'
            f'<w:qFormat/><w:pPr><w:keepNext/><w:keepLines/>{bdr}'
            f'<w:spacing w:before="{before}" w:after="{after}"/>'
            f'<w:outlineLvl w:val="{outline}"/></w:pPr>'
            f'<w:rPr><w:b/><w:color w:val="{color}"/>'
            f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/></w:rPr></w:style>'
        )

    return (
        XML_DECL
        + f"<w:styles {DOC_NS}>"
        + "<w:docDefaults><w:rPrDefault><w:rPr>"
        '<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/>'
        f'<w:color w:val="{INK}"/><w:sz w:val="21"/><w:szCs w:val="21"/>'
        "</w:rPr></w:rPrDefault><w:pPrDefault><w:pPr>"
        '<w:spacing w:before="0" w:after="140" w:line="276" w:lineRule="auto"/>'
        "</w:pPr></w:pPrDefault></w:docDefaults>"
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
        '<w:name w:val="Normal"/><w:qFormat/></w:style>'
        '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/>'
        '<w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>'
        "<w:pPr><w:keepNext/>"
        f'<w:pBdr><w:bottom w:val="single" w:sz="12" w:space="10" w:color="{ACCENT}"/></w:pBdr>'
        '<w:spacing w:before="0" w:after="360"/>'
        "</w:pPr><w:rPr><w:b/>"
        f'<w:color w:val="{INK}"/><w:sz w:val="40"/><w:szCs w:val="40"/></w:rPr></w:style>'
        + heading("Heading1", "heading 1", 0, 30, ACCENT, 400, 160, border=True)
        + heading("Heading2", "heading 2", 1, 24, INK, 280, 120)
        + heading("Heading3", "heading 3", 2, 22, INK, 240, 100)
        + '<w:style w:type="paragraph" w:styleId="ListParagraph">'
        '<w:name w:val="List Paragraph"/><w:basedOn w:val="Normal"/><w:qFormat/>'
        '<w:pPr><w:ind w:left="426"/><w:contextualSpacing/></w:pPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="Caption"><w:name w:val="caption"/>'
        '<w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>'
        '<w:pPr><w:spacing w:before="0" w:after="300"/><w:jc w:val="center"/></w:pPr>'
        f'<w:rPr><w:i/><w:color w:val="{MUTED}"/><w:sz w:val="18"/><w:szCs w:val="18"/>'
        "</w:rPr></w:style>"
        "</w:styles>"
    )


def numbering_xml(ordered_lists: int) -> str:
    def lvl(ilvl, fmt, text, indent):
        return (
            f'<w:lvl w:ilvl="{ilvl}"><w:start w:val="1"/><w:numFmt w:val="{fmt}"/>'
            f'<w:lvlText w:val="{text}"/><w:lvlJc w:val="left"/>'
            f'<w:pPr><w:ind w:left="{indent}" w:hanging="340"/></w:pPr></w:lvl>'
        )

    abstract = (
        '<w:abstractNum w:abstractNumId="0"><w:multiLevelType w:val="hybridMultilevel"/>'
        + lvl(0, "bullet", "\u2022", 426)
        + lvl(1, "bullet", "\u25e6", 786)
        + "</w:abstractNum>"
        '<w:abstractNum w:abstractNumId="1"><w:multiLevelType w:val="hybridMultilevel"/>'
        + lvl(0, "decimal", "%1.", 426)
        + lvl(1, "lowerLetter", "%2.", 786)
        + "</w:abstractNum>"
    )
    nums = ['<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>']
    for i in range(ordered_lists):
        nums.append(
            f'<w:num w:numId="{2 + i}"><w:abstractNumId w:val="1"/>'
            '<w:lvlOverride w:ilvl="0"><w:startOverride w:val="1"/></w:lvlOverride></w:num>'
        )
    return XML_DECL + f"<w:numbering {DOC_NS}>{abstract}{''.join(nums)}</w:numbering>"


def content_types_xml() -> str:
    ns = "http://schemas.openxmlformats.org/package/2006/content-types"
    doc = "application/vnd.openxmlformats-officedocument.wordprocessingml"
    return (
        XML_DECL
        + f'<Types xmlns="{ns}">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="png" ContentType="image/png"/>'
        f'<Override PartName="/word/document.xml" ContentType="{doc}.document.main+xml"/>'
        f'<Override PartName="/word/styles.xml" ContentType="{doc}.styles+xml"/>'
        f'<Override PartName="/word/numbering.xml" ContentType="{doc}.numbering+xml"/>'
        '<Override PartName="/docProps/core.xml" '
        'ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        "</Types>"
    )


def root_rels_xml() -> str:
    ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    off = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    pkg = "http://schemas.openxmlformats.org/package/2006/relationships"
    return (
        XML_DECL
        + f'<Relationships xmlns="{ns}">'
        f'<Relationship Id="rId1" Type="{off}/officeDocument" Target="word/document.xml"/>'
        f'<Relationship Id="rId2" Type="{pkg}/metadata/core-properties" Target="docProps/core.xml"/>'
        "</Relationships>"
    )


def document_rels_xml(image_count: int) -> str:
    ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    off = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    rels = [
        f'<Relationship Id="rIdStyles" Type="{off}/styles" Target="styles.xml"/>',
        f'<Relationship Id="rIdNum" Type="{off}/numbering" Target="numbering.xml"/>',
    ]
    for i in range(1, image_count + 1):
        rels.append(
            f'<Relationship Id="rIdImg{i}" Type="{off}/image" Target="media/diagram-{i}.png"/>'
        )
    return XML_DECL + f'<Relationships xmlns="{ns}">{"".join(rels)}</Relationships>'


def core_xml(title: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        XML_DECL
        + '<cp:coreProperties '
        'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:title>{esc(title)}</dc:title>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{stamp}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{stamp}</dcterms:modified>'
        "</cp:coreProperties>"
    )


def document_xml(body: str) -> str:
    sect = (
        "<w:sectPr>"
        '<w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134" '
        'w:header="567" w:footer="567" w:gutter="0"/>'
        "</w:sectPr>"
    )
    return XML_DECL + f"<w:document {DOC_NS}><w:body>{body}{sect}</w:body></w:document>"


# ── Build ──────────────────────────────────────────────────────────────────────

def main() -> None:
    global STEM
    if len(sys.argv) > 1:
        STEM = Path(sys.argv[1]).stem

    src = DOCS / f"{STEM}.html"
    if not src.exists():
        raise SystemExit(f"{src.name} not found in {DOCS}")

    root = parse_html(src)
    body_node = find(root, "body")
    if body_node is None:
        raise SystemExit(f"No <body> found in {src.name}")

    renderer = Renderer()
    renderer.walk(body_node)

    title_node = find(root, "h1")
    title = plain_text(title_node) if title_node else STEM

    target = DOCS / f"{STEM.upper()}.docx"
    try:
        target.unlink(missing_ok=True)
    except PermissionError:
        # Open in Word; write beside it instead of failing the build.
        target = DOCS / f"{STEM.upper()}-latest.docx"
        target.unlink(missing_ok=True)

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types_xml())
        z.writestr("_rels/.rels", root_rels_xml())
        z.writestr("docProps/core.xml", core_xml(title))
        z.writestr("word/document.xml", document_xml("".join(renderer.body)))
        z.writestr("word/_rels/document.xml.rels", document_rels_xml(len(renderer.images)))
        z.writestr("word/styles.xml", styles_xml())
        z.writestr("word/numbering.xml", numbering_xml(renderer.ordered_lists))
        for i, img in enumerate(renderer.images, start=1):
            z.write(img, f"word/media/diagram-{i}.png")

    print(f"Wrote {target} ({target.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
