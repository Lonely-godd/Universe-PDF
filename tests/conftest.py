from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfWriter


@pytest.fixture
def picture(tmp_path):
    path = tmp_path / "photo.png"
    Image.new("RGBA", (120, 60), (255, 0, 0, 128)).save(path)
    return path


@pytest.fixture
def existing_pdf(tmp_path):
    path = tmp_path / "source.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=321, height=456)
    writer.write(path)
    return path


@pytest.fixture
def font():
    candidates = [
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    return next(path for path in candidates if path.exists())
