import shutil
import zipfile

import pytest
from pypdf import PdfReader

from universe_pdf.conversion import ConversionError, Converter


def test_missing_office(tmp_path, font):
    path = tmp_path / "file.docx"
    path.write_bytes(b"test")
    with pytest.raises(ConversionError, match="LibreOffice"):
        Converter(font_path=font, office_binary="missing-soffice-binary").compile(
            [path], tmp_path / "out.pdf"
        )


@pytest.mark.office
@pytest.mark.skipif(not shutil.which("soffice"), reason="LibreOffice не установлен")
def test_real_docx(tmp_path, font):
    path = tmp_path / "document.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", '''<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>''')
        archive.writestr("_rels/.rels", '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>''')
        archive.writestr("word/document.xml", '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Тест документа</w:t></w:r></w:p></w:body></w:document>''')
    output = tmp_path / "result.pdf"
    Converter(font_path=font).compile([path], output)
    assert len(PdfReader(output).pages) >= 1
