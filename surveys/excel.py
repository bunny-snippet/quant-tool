"""Small, dependency-free XLSX writer for streamed report exports.

The production image intentionally keeps a lean Python dependency set.  This
module writes the small Open XML surface Excel needs while rows are iterated,
so exports do not require loading an entire report into application memory.
"""

from __future__ import annotations

import re
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from xml.sax.saxutils import escape

from django.http import FileResponse


EXCEL_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_INVALID_XML = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")


@dataclass(frozen=True)
class ExcelSheet:
    name: str
    headers: list[str] | tuple[str, ...]
    rows: object
    widths: list[float] | tuple[float, ...] | None = None


def _safe_sheet_name(name: str, index: int) -> str:
    cleaned = re.sub(r"[\\/*?:\[\]]", " ", str(name)).strip()[:31]
    return cleaned or f"Sheet {index}"


def _column_name(number: int) -> str:
    letters = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _text(value) -> str:
    if isinstance(value, datetime):
        value = value.isoformat(sep=" ")
    elif isinstance(value, date):
        value = value.isoformat()
    elif isinstance(value, (dict, list, tuple)):
        value = ", ".join(str(item) for item in value)
    value = _INVALID_XML.sub("", str(value))
    return value[:32767]


def _cell_xml(reference: str, value, row_style: int) -> str:
    if value is None or value == "":
        return f'<c r="{reference}" s="{row_style}"/>'
    if isinstance(value, bool):
        return f'<c r="{reference}" s="{row_style}" t="b"><v>{int(value)}</v></c>'
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        numeric_style = 4 if row_style == 2 else 5
        return f'<c r="{reference}" s="{numeric_style}"><v>{value}</v></c>'
    content = escape(_text(value), {'"': "&quot;"})
    preserve = ' xml:space="preserve"' if content[:1].isspace() or content[-1:].isspace() else ""
    # Force report identifiers and other text through Excel's text format.
    # This prevents long numeric-looking IDs from being displayed in scientific
    # notation or rounded by spreadsheet applications.
    text_style = 6 if row_style == 2 else 7 if row_style == 3 else row_style
    return f'<c r="{reference}" s="{text_style}" t="inlineStr"><is><t{preserve}>{content}</t></is></c>'


def _sheet_xml(archive: zipfile.ZipFile, path: str, sheet: ExcelSheet) -> None:
    headers = list(sheet.headers)
    last_column = _column_name(max(1, len(headers)))
    widths = list(sheet.widths or [])
    with archive.open(path, "w") as output:
        output.write(
            b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            b'<sheetViews><sheetView showGridLines="0" workbookViewId="0">'
            b'<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
            b'</sheetView></sheetViews>'
        )
        if headers:
            output.write(b"<cols>")
            for index, header in enumerate(headers, 1):
                width = widths[index - 1] if index <= len(widths) else min(42, max(12, len(header) + 3))
                output.write(
                    f'<col min="{index}" max="{index}" width="{width:.1f}" customWidth="1"/>'.encode()
                )
            output.write(b"</cols>")
        output.write(b"<sheetData>")
        header_cells = "".join(
            _cell_xml(f"{_column_name(index)}1", value, 1)
            for index, value in enumerate(headers, 1)
        )
        output.write(f'<row r="1" ht="24" customHeight="1">{header_cells}</row>'.encode())
        last_row = 1
        for row_number, row in enumerate(sheet.rows, 2):
            last_row = row_number
            style = 3 if row_number % 2 == 0 else 2
            values = list(row)
            cells = "".join(
                _cell_xml(f"{_column_name(index)}{row_number}", value, style)
                for index, value in enumerate(values[: len(headers)], 1)
            )
            output.write(f'<row r="{row_number}" ht="20" customHeight="1">{cells}</row>'.encode())
        output.write(b"</sheetData>")
        if headers:
            output.write(f'<autoFilter ref="A1:{last_column}{max(1, last_row)}"/>'.encode())
        output.write(b'<pageMargins left="0.3" right="0.3" top="0.5" bottom="0.5" header="0.2" footer="0.2"/>')
        output.write(b"</worksheet>")


def _styles_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="2"><font><sz val="10"/><name val="Aptos"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="10"/><name val="Aptos"/></font></fonts>
  <fills count="4"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF17233F"/><bgColor indexed="64"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFF5F8FB"/><bgColor indexed="64"/></patternFill></fill></fills>
  <borders count="2"><border/><border><bottom style="thin"><color rgb="FFDDE4EC"/></bottom></border></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="8">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0"><alignment vertical="center"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0"><alignment vertical="center"/></xf>
    <xf numFmtId="0" fontId="0" fillId="3" borderId="1" xfId="0"><alignment vertical="center"/></xf>
    <xf numFmtId="4" fontId="0" fillId="0" borderId="1" xfId="0"><alignment vertical="center"/></xf>
    <xf numFmtId="4" fontId="0" fillId="3" borderId="1" xfId="0"><alignment vertical="center"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" quotePrefix="1"><alignment vertical="center"/></xf>
    <xf numFmtId="0" fontId="0" fillId="3" borderId="1" xfId="0" quotePrefix="1"><alignment vertical="center"/></xf>
  </cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""


def build_excel_response(filename: str, sheets: list[ExcelSheet]) -> FileResponse:
    workbook_sheets = [
        ExcelSheet(_safe_sheet_name(sheet.name, index), sheet.headers, sheet.rows, sheet.widths)
        for index, sheet in enumerate(sheets, 1)
    ]
    output = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        content_overrides = "".join(
            f'<Override PartName="/xl/worksheets/sheet{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for index in range(1, len(workbook_sheets) + 1)
        )
        archive.writestr("[Content_Types].xml", f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>{content_overrides}<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/><Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/></Types>''')
        archive.writestr("_rels/.rels", '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/><Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/></Relationships>''')
        workbook_nodes = "".join(
            f'<sheet name="{escape(sheet.name, {chr(34): "&quot;"})}" sheetId="{index}" r:id="rId{index}"/>'
            for index, sheet in enumerate(workbook_sheets, 1)
        )
        archive.writestr("xl/workbook.xml", f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><bookViews><workbookView/></bookViews><sheets>{workbook_nodes}</sheets></workbook>''')
        relationships = "".join(
            f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{index}.xml"/>'
            for index in range(1, len(workbook_sheets) + 1)
        )
        style_id = len(workbook_sheets) + 1
        archive.writestr("xl/_rels/workbook.xml.rels", f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{relationships}<Relationship Id="rId{style_id}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>''')
        archive.writestr("xl/styles.xml", _styles_xml())
        archive.writestr("docProps/core.xml", '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:creator>Exchange Hub</dc:creator><dc:title>Exchange Hub export</dc:title></cp:coreProperties>''')
        archive.writestr("docProps/app.xml", '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"><Application>Exchange Hub</Application></Properties>''')
        for index, sheet in enumerate(workbook_sheets, 1):
            _sheet_xml(archive, f"xl/worksheets/sheet{index}.xml", sheet)
    output.seek(0)
    response = FileResponse(output, content_type=EXCEL_CONTENT_TYPE, as_attachment=True, filename=filename)
    response["X-Content-Type-Options"] = "nosniff"
    return response
