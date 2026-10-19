import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter

from universe_pdf.conversion import ConversionError, Converter


def test_mixed_order_and_cyrillic(tmp_path, picture, existing_pdf, font):
    note = tmp_path / "note.txt"
    note.write_text("Привет, мир!\nДокументы собраны вместе.", encoding="utf-8")
    output = tmp_path / "result.pdf"
    Converter(font_path=font).compile([picture, existing_pdf, note], output)
    pages = PdfReader(output).pages
    assert len(pages) == 3
    assert float(pages[0].mediabox.width) == pytest.approx(595.276, abs=0.01)
    assert float(pages[1].mediabox.width) == 321
    assert "Привет, мир!" in pages[2].extract_text()


def test_multiframe_tiff(tmp_path, font):
    path = tmp_path / "scan.tiff"
    Image.new("RGB", (20, 20), "red").save(
        path, save_all=True, append_images=[Image.new("RGB", (20, 20), "blue")]
    )
    output = tmp_path / "result.pdf"
    Converter(font_path=font).compile([path], output)
    assert len(PdfReader(output).pages) == 2


def test_exif_orientation_and_transparency():
    image = Image.new("RGBA", (100, 50), (255, 0, 0, 0))
    image.getexif()[274] = 6
    normalized = Converter.normalize_image(image)
    assert normalized.size == (50, 100)
    assert normalized.mode == "RGB"
    assert normalized.getpixel((0, 0)) == (255, 255, 255)


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "cp1251"])
def test_text_encodings_and_pagination(tmp_path, font, encoding):
    path = tmp_path / "note.txt"
    path.write_text("Привет " * 3000, encoding=encoding)
    output = tmp_path / "result.pdf"
    Converter(font_path=font).compile([path], output)
    reader = PdfReader(output)
    assert len(reader.pages) > 1
    assert "Привет" in reader.pages[0].extract_text()


@pytest.mark.parametrize("name,content", [("bad.exe", b"MZ"), ("bad.jpg", b"broken"),
                                         ("bad.pdf", b"broken")])
def test_bad_files_fail_without_partial_output(tmp_path, font, name, content):
    path = tmp_path / name
    path.write_bytes(content)
    output = tmp_path / "result.pdf"
    with pytest.raises(ConversionError):
        Converter(font_path=font).compile([path], output)
    assert not output.exists()


def test_encrypted_pdf_rejected(tmp_path, font):
    path = tmp_path / "locked.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.encrypt("secret")
    writer.write(path)
    with pytest.raises(ConversionError, match="парол"):
        Converter(font_path=font).compile([path], tmp_path / "out.pdf")


def test_page_and_output_limits(tmp_path, picture, existing_pdf, font):
    with pytest.raises(ConversionError, match="страниц"):
        Converter(font_path=font, max_pages=1).compile(
            [picture, existing_pdf], tmp_path / "out.pdf"
        )
    with pytest.raises(ConversionError, match="размер"):
        Converter(font_path=font, max_output_bytes=10).compile([picture], tmp_path / "out.pdf")


def test_empty_queue_rejected(tmp_path, font):
    with pytest.raises(ConversionError, match="файл"):
        Converter(font_path=font).compile([], tmp_path / "out.pdf")
