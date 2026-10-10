"""
A small vector-PDF writer: lines, rectangles, circles and text on pages of
a chosen size, nothing else.

It exists because the fabrication drawing has to be a PDF a shop can
read, and the environment this runs in has no PDF library (and a layout
engine would be far more than a drawing of lines and labels needs).
Text uses the standard Helvetica face, which every PDF reader carries, so
nothing is embedded and the file stays a few kilobytes.

Units are millimetres, origin at the bottom-left of the page, y upwards
(the PDF's own convention, scaled from points).
"""
from __future__ import annotations

import math
import zlib
from typing import List, Tuple

PT_PER_MM = 72.0 / 25.4

A4 = (210.0, 297.0)
A3 = (297.0, 420.0)
A4_LANDSCAPE = (297.0, 210.0)
A3_LANDSCAPE = (420.0, 297.0)

# Helvetica advance widths (1/1000 em) for printable ASCII, from the
# standard AFM. Used to right-align and wrap text without a font engine.
_HELV = [
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
]


def text_width(text: str, size: float) -> float:
    """Width of `text` in mm at font size `size` (pt)."""
    total = 0
    for ch in text:
        code = ord(ch)
        total += _HELV[code - 32] if 32 <= code < 127 else 556
    return total / 1000.0 * size / PT_PER_MM


def _escape(text: str) -> str:
    out = []
    for ch in text:
        code = ord(ch)
        if ch in "\\()":
            out.append("\\" + ch)
        elif 32 <= code < 127:
            out.append(ch)
        elif code == 0xB5:           # micro sign, WinAnsi 0xB5
            out.append("\\265")
        elif code == 0xB0:           # degree
            out.append("\\260")
        elif code == 0xB1:           # plus-minus
            out.append("\\261")
        elif code == 0xD8:           # O-slash, used for diameter
            out.append("\\330")
        elif code == 0x3A9:          # Omega
            out.append("Ohm")
        else:
            out.append("?")
    return "".join(out)


class Page:
    def __init__(self, size: Tuple[float, float]) -> None:
        self.width, self.height = size
        self._ops: List[str] = []

    @staticmethod
    def _n(value: float) -> str:
        return f"{value * PT_PER_MM:.3f}"

    def line_width(self, mm: float) -> None:
        self._ops.append(f"{self._n(mm)} w")

    def gray(self, level: float) -> None:
        self._ops.append(f"{level:.3f} G {level:.3f} g")

    def dash(self, on: float = 0, off: float = 0) -> None:
        self._ops.append(f"[{self._n(on)} {self._n(off)}] 0 d" if on else "[] 0 d")

    def line(self, x1: float, y1: float, x2: float, y2: float) -> None:
        self._ops.append(f"{self._n(x1)} {self._n(y1)} m {self._n(x2)} {self._n(y2)} l S")

    def rect(self, x: float, y: float, w: float, h: float, fill: bool = False) -> None:
        self._ops.append(f"{self._n(x)} {self._n(y)} {self._n(w)} {self._n(h)} re {'f' if fill else 'S'}")

    def circle(self, cx: float, cy: float, r: float, fill: bool = False) -> None:
        k = 0.5522847498 * r
        n = self._n
        self._ops.append(
            f"{n(cx + r)} {n(cy)} m "
            f"{n(cx + r)} {n(cy + k)} {n(cx + k)} {n(cy + r)} {n(cx)} {n(cy + r)} c "
            f"{n(cx - k)} {n(cy + r)} {n(cx - r)} {n(cy + k)} {n(cx - r)} {n(cy)} c "
            f"{n(cx - r)} {n(cy - k)} {n(cx - k)} {n(cy - r)} {n(cx)} {n(cy - r)} c "
            f"{n(cx + k)} {n(cy - r)} {n(cx + r)} {n(cy - k)} {n(cx + r)} {n(cy)} c "
            f"{'f' if fill else 'S'}"
        )

    def text(self, x: float, y: float, text: str, size: float = 10, bold: bool = False,
             align: str = "left") -> None:
        width = text_width(text, size)
        if align == "right":
            x -= width
        elif align == "center":
            x -= width / 2
        font = "F2" if bold else "F1"
        self._ops.append(
            f"BT /{font} {size:.2f} Tf {self._n(x)} {self._n(y)} Td ({_escape(text)}) Tj ET"
        )

    def arrow_dimension(self, x1: float, y1: float, x2: float, y2: float, label: str,
                        size: float = 9, offset: float = 0.0) -> None:
        """A dimension line from (x1,y1) to (x2,y2) with arrowheads and the
        label centred above it (horizontal dimensions) or beside it."""
        self.line(x1, y1, x2, y2)
        angle = math.atan2(y2 - y1, x2 - x1)
        head = 2.2
        for (px, py, sign) in ((x1, y1, 1), (x2, y2, -1)):
            a = angle if sign > 0 else angle + math.pi
            for da in (0.3, -0.3):
                self.line(px, py, px + head * math.cos(a + da), py + head * math.sin(a + da))
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        if abs(y2 - y1) < 1e-9:
            self.text(mx, my + 1.2 + offset, label, size, align="center")
        else:
            self.text(mx + 1.8 + offset, my - size * 0.18, label, size)

    def content(self) -> bytes:
        return "\n".join(self._ops).encode("latin-1")


class Document:
    def __init__(self, title: str = "") -> None:
        self.title = title
        self.pages: List[Page] = []

    def add_page(self, size: Tuple[float, float] = A4_LANDSCAPE) -> Page:
        page = Page(size)
        self.pages.append(page)
        return page

    def to_bytes(self) -> bytes:
        objects: List[bytes] = []

        def add(body: bytes) -> int:
            objects.append(body)
            return len(objects)

        catalog = add(b"")          # 1, filled below
        pages_obj = add(b"")        # 2
        f1 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
        f2 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
        info = add(f"<< /Title ({_escape(self.title)}) /Producer (microwave_package) >>".encode("latin-1"))
        kids = []
        for page in self.pages:
            data = zlib.compress(page.content())
            stream = add(
                b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(data) + data + b"\nendstream"
            )
            page_obj = add(
                (f"<< /Type /Page /Parent {pages_obj} 0 R "
                 f"/MediaBox [0 0 {page.width * PT_PER_MM:.2f} {page.height * PT_PER_MM:.2f}] "
                 f"/Resources << /Font << /F1 {f1} 0 R /F2 {f2} 0 R >> >> "
                 f"/Contents {stream} 0 R >>").encode("latin-1")
            )
            kids.append(page_obj)
        objects[catalog - 1] = f"<< /Type /Catalog /Pages {pages_obj} 0 R >>".encode("latin-1")
        objects[pages_obj - 1] = (
            f"<< /Type /Pages /Count {len(kids)} /Kids [{' '.join(f'{k} 0 R' for k in kids)}] >>"
        ).encode("latin-1")

        out = bytearray(b"%PDF-1.4\n")
        offsets = []
        for number, body in enumerate(objects, start=1):
            offsets.append(len(out))
            out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
        for offset in offsets:
            out += f"{offset:010d} 00000 n \n".encode()
        out += (f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R /Info {info} 0 R >>\n"
                f"startxref\n{xref}\n%%EOF\n").encode()
        return bytes(out)

    def save(self, path: str) -> None:
        with open(path, "wb") as f:
            f.write(self.to_bytes())
