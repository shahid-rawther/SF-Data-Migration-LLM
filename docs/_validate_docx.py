import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

path = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/ARCHITECTURE.docx")
z = zipfile.ZipFile(path)
print("zip integrity:", "OK" if z.testzip() is None else z.testzip())

for name in z.namelist():
    data = z.read(name)
    if name.endswith((".xml", ".rels")):
        ET.fromstring(data)
        print(f"well-formed  {name:40s} {len(data):>8,} bytes")
    else:
        print(f"binary part  {name:40s} {len(data):>8,} bytes")

doc = z.read("word/document.xml").decode("utf-8")
rels = z.read("word/_rels/document.xml.rels").decode("utf-8")
used = set(re.findall(r'r:embed="([^"]+)"', doc))
declared = set(re.findall(r'Id="([^"]+)"', rels))
print("image rels used:", sorted(used))
print("missing rels:", used - declared or "none")

num_ids_used = set(re.findall(r'<w:numId w:val="(\d+)"/>', doc))
num_ids_declared = set(re.findall(r'<w:num w:numId="(\d+)">', z.read("word/numbering.xml").decode()))
print("numIds used:", sorted(num_ids_used, key=int))
print("missing numIds:", num_ids_used - num_ids_declared or "none")

styles_used = set(re.findall(r'<w:pStyle w:val="([^"]+)"/>', doc))
styles_declared = set(re.findall(r'w:styleId="([^"]+)"', z.read("word/styles.xml").decode()))
print("styles used:", sorted(styles_used))
print("missing styles:", styles_used - styles_declared or "none")

# Word rejects properties that appear out of the schema's fixed sequence.
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
ORDERS = {
    "pPr": ["pStyle", "keepNext", "keepLines", "pageBreakBefore", "framePr", "widowControl",
            "numPr", "suppressLineNumbers", "pBdr", "shd", "tabs", "spacing", "ind",
            "contextualSpacing", "jc", "outlineLvl", "rPr", "sectPr"],
    "rPr": ["rFonts", "b", "bCs", "i", "iCs", "caps", "strike", "color", "spacing", "position",
            "sz", "szCs", "highlight", "u", "shd", "vertAlign", "lang"],
    "tblPr": ["tblStyle", "tblW", "jc", "tblCellSpacing", "tblInd", "tblBorders", "shd",
              "tblLayout", "tblCellMar", "tblLook"],
    "tcPr": ["cnfStyle", "tcW", "gridSpan", "vMerge", "tcBorders", "shd", "noWrap", "tcMar",
             "textDirection", "vAlign"],
    "trPr": ["cnfStyle", "gridBefore", "gridAfter", "cantSplit", "trHeight", "tblHeader", "jc"],
}

problems = []
for part in ("word/document.xml", "word/styles.xml"):
    for elem in ET.fromstring(z.read(part)).iter():
        local = elem.tag.replace(W, "")
        order = ORDERS.get(local)
        if not order:
            continue
        seen = [c.tag.replace(W, "") for c in elem]
        ranks = [order.index(s) for s in seen if s in order]
        if ranks != sorted(ranks):
            problems.append(f"{part}: <w:{local}> out of order -> {seen}")

print("schema order:", "OK" if not problems else "")
for p in dict.fromkeys(problems):
    print("  ", p)

print(
    "counts -> tables:", doc.count("<w:tbl>"),
    "rows:", doc.count("<w:tr>"),
    "paragraphs:", doc.count("<w:p>") - doc.count("<w:pPr>") if False else doc.count("<w:p>"),
    "images:", doc.count("<w:drawing>"),
)
