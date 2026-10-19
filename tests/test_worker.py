import pytest
from pypdf import PdfReader

from universe_pdf.config import Settings
from universe_pdf.conversion import ConversionError
from universe_pdf.worker import WorkerRunner


async def test_real_worker(tmp_path, picture, font):
    output = tmp_path / "out.pdf"
    runner = WorkerRunner(Settings(token="123:example", font_path=str(font)))
    await runner([picture], output)
    assert len(PdfReader(output).pages) == 1


async def test_worker_error(tmp_path, font):
    path = tmp_path / "bad.png"
    path.write_bytes(b"broken")
    runner = WorkerRunner(Settings(token="123:example", font_path=str(font)))
    with pytest.raises(ConversionError):
        await runner([path], tmp_path / "out.pdf")
