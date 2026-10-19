import io
import zipfile

import pytest
from pypdf import PdfReader, PdfWriter

from universe_pdf.conversion import ConversionError
from universe_pdf.pdf_tools import PDFTools, parse_pages, split_groups


@pytest.fixture
def twenty_pages(tmp_path):
    path = tmp_path / "twenty.pdf"
    writer = PdfWriter()
    for number in range(1, 21):
        writer.add_blank_page(width=200 + number, height=400)
    writer.write(path)
    return path


def widths(reader):
    return [int(page.mediabox.width) - 200 for page in reader.pages]


def test_page_numbers_are_one_based_and_keep_requested_order():
    assert parse_pages("20, 1, 3–5", 20) == [19, 0, 2, 3, 4]
    assert parse_pages("1 — 3, 20", 20) == [0, 1, 2, 19]


@pytest.mark.parametrize(
    "selection",
    ["", "0", "21", "-1", "5-2", "1,,2", "hello", "1,1", "1-3,3-4", "1.5", "1-999999999"],
)
def test_bad_page_input(selection):
    with pytest.raises(ConversionError):
        parse_pages(selection, 20)


def test_split_syntax_and_last_short_part():
    assert split_groups("по 8", 20) == [list(range(8)), list(range(8, 16)), list(range(16, 20))]
    assert split_groups("1-10; 11-20", 20) == [list(range(10)), list(range(10, 20))]


@pytest.mark.parametrize(
    "selection", ["0", "20", "по 21", "1-10;10-20", "1-5;7-20", "1-20", "1-10;", "по -2"]
)
def test_invalid_split(selection):
    with pytest.raises(ConversionError):
        split_groups(selection, 20)


def test_extract_exact_pages_and_preserve_original(twenty_pages, tmp_path):
    original = twenty_pages.read_bytes()
    output = tmp_path / "pages.pdf"
    tools = PDFTools()
    assert tools.inspect(twenty_pages) == 20
    tools.edit(twenty_pages, output, "extract", "20, 1, 3-5")
    assert widths(PdfReader(output)) == [20, 1, 3, 4, 5]
    assert twenty_pages.read_bytes() == original


def test_remove_and_no_empty_result(twenty_pages, tmp_path):
    output = tmp_path / "pages.pdf"
    PDFTools().edit(twenty_pages, output, "remove", "2-19")
    assert widths(PdfReader(output)) == [1, 20]
    with pytest.raises(ConversionError, match="все"):
        PDFTools().edit(twenty_pages, output, "remove", "1-20")
    assert not output.exists()


def test_split_archive(twenty_pages, tmp_path):
    output = tmp_path / "parts.zip"
    PDFTools().edit(twenty_pages, output, "split", "по 8")
    with zipfile.ZipFile(output) as archive:
        assert archive.namelist() == ["часть-001.pdf", "часть-002.pdf", "часть-003.pdf"]
        groups = [widths(PdfReader(io.BytesIO(archive.read(name)))) for name in archive.namelist()]
        assert groups == [list(range(1, 9)), list(range(9, 17)), list(range(17, 21))]


def test_pdf_limits_and_password(twenty_pages, tmp_path):
    with pytest.raises(ConversionError, match="страниц"):
        PDFTools(max_pages=19).inspect(twenty_pages)
    output = tmp_path / "pages.pdf"
    with pytest.raises(ConversionError, match="размер"):
        PDFTools(max_output_bytes=10).edit(twenty_pages, output, "split", "по 5")
    assert not output.exists()
    encrypted = tmp_path / "encrypted.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=400)
    writer.encrypt("password")
    writer.write(encrypted)
    with pytest.raises(ConversionError, match="парол"):
        PDFTools().inspect(encrypted)


def test_extraction_preserves_page_content(tmp_path):
    from reportlab.pdfgen import canvas

    source = tmp_path / "source.pdf"
    pdf = canvas.Canvas(str(source))
    for number in range(1, 4):
        pdf.drawString(50, 750, f"Page {number}")
        pdf.showPage()
    pdf.save()
    output = tmp_path / "selection.pdf"
    PDFTools().edit(source, output, "extract", "3, 1")
    assert [page.extract_text().strip() for page in PdfReader(output).pages] == ["Page 3", "Page 1"]


def test_damaged_pdf_and_protection_against_overwriting_source(tmp_path, twenty_pages):
    broken = tmp_path / "bad.pdf"
    broken.write_bytes(b"not a PDF")
    with pytest.raises(ConversionError, match="прочитать"):
        PDFTools().inspect(broken)
    original = twenty_pages.read_bytes()
    with pytest.raises(ConversionError, match="отдельно"):
        PDFTools().edit(twenty_pages, twenty_pages, "extract", "1")
    assert twenty_pages.read_bytes() == original
