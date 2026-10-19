import io
import os
import shutil
import subprocess
import tempfile
import warnings
from pathlib import Path

from PIL import Image, ImageOps, ImageSequence
from pillow_heif import register_heif_opener
from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from .formats import IMAGE, OFFICE, TEXT

register_heif_opener()
Image.MAX_IMAGE_PIXELS = 40_000_000


class ConversionError(Exception):
    """Безопасное сообщение об ошибке для пользователя."""


class Converter:
    def __init__(
        self,
        *,
        font_path=None,
        office_binary="soffice",
        office_timeout=60,
        max_pages=500,
        max_output_bytes=49_000_000,
    ):
        self.font_path = font_path
        self.office_binary = office_binary
        self.office_timeout = office_timeout
        self.max_pages = max_pages
        self.max_output_bytes = max_output_bytes

    @staticmethod
    def normalize_image(image):
        image = ImageOps.exif_transpose(image)
        image.thumbnail((2400, 2400), Image.Resampling.LANCZOS)
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, "white")
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background

    def compile(self, files, output):
        output = Path(output)
        output.unlink(missing_ok=True)
        if not files:
            raise ConversionError("Сначала добавьте хотя бы один файл.")
        writer = PdfWriter()
        try:
            with tempfile.TemporaryDirectory(prefix="convert-", dir=output.parent) as scratch:
                for index, source in enumerate(files, 1):
                    source = Path(source)
                    target = Path(scratch) / f"part-{index}.pdf"
                    try:
                        if source.suffix.lower() in IMAGE:
                            self.image_pdf(source, target)
                        elif source.suffix.lower() in TEXT:
                            self.text_pdf(source, target)
                        elif source.suffix.lower() in OFFICE:
                            self.office_pdf(source, target)
                        elif source.suffix.lower() == ".pdf":
                            target = source
                        else:
                            raise ConversionError("Этот формат не поддерживается.")
                        reader = PdfReader(target)
                        if reader.is_encrypted:
                            raise ConversionError("PDF защищён паролем. Пришлите файл без пароля.")
                        if not reader.pages:
                            raise ConversionError("Документ не содержит страниц.")
                        if len(writer.pages) + len(reader.pages) > self.max_pages:
                            raise ConversionError(f"Превышен лимит страниц: {self.max_pages}.")
                        for page in reader.pages:
                            # Исключаем интерактивные действия и вложения исходного PDF.
                            for key in ("/AA", "/Annots"):
                                page.pop(key, None)
                            writer.add_page(page)
                    except ConversionError as error:
                        raise ConversionError(f"Файл №{index}: {error}") from None
                    except Exception:
                        raise ConversionError(
                            f"Файл №{index}: не удалось прочитать документ. Проверьте формат и целостность."
                        ) from None
                writer.add_metadata({"/Title": "Общий документ"})
                writer.write(output)
            if output.stat().st_size > self.max_output_bytes:
                raise ConversionError(
                    "Итоговый размер PDF слишком большой. Уменьшите число файлов."
                )
        except Exception:
            output.unlink(missing_ok=True)
            raise
        finally:
            writer.close()

    def image_pdf(self, source, target):
        pdf = canvas.Canvas(str(target), pagesize=A4)
        width, height = A4
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source) as image:
                for index, frame in enumerate(ImageSequence.Iterator(image), 1):
                    if index > self.max_pages:
                        raise ConversionError(f"Превышен лимит страниц: {self.max_pages}.")
                    if frame.width * frame.height > 40_000_000:
                        raise ConversionError(
                            "Изображение слишком большое: максимум 40 млн пикселей."
                        )
                    normalized = self.normalize_image(frame.copy())
                    encoded = io.BytesIO()
                    normalized.save(encoded, "JPEG", quality=90)
                    scale = min((width - 48) / normalized.width, (height - 48) / normalized.height)
                    w, h = normalized.width * scale, normalized.height * scale
                    pdf.drawImage(ImageReader(encoded), (width - w) / 2, (height - h) / 2, w, h)
                    pdf.showPage()
        pdf.save()

    def text_pdf(self, source, target):
        candidates = (
            [Path(self.font_path)]
            if self.font_path
            else [
                Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
                Path("C:/Windows/Fonts/arial.ttf"),
                Path("/Library/Fonts/Arial.ttf"),
            ]
        )
        font = next((path for path in candidates if path.is_file()), None)
        if font is None:
            raise ConversionError(
                "Не найден шрифт с кириллицей. Администратору нужно указать FONT_PATH."
            )
        pdfmetrics.registerFont(TTFont("DocumentText", str(font)))
        data = source.read_bytes()
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = data.decode("utf-16")
        else:
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = data.decode("cp1251")
        if "\x00" in text:
            raise ConversionError("Текстовый файл содержит двоичные данные.")
        pdf = canvas.Canvas(str(target), pagesize=A4)
        width, height = A4
        y, pages = height - 42, 1
        pdf.setFont("DocumentText", 10)

        def draw(line):
            nonlocal y, pages
            if y < 42:
                pages += 1
                if pages > self.max_pages:
                    raise ConversionError(f"Превышен лимит страниц: {self.max_pages}.")
                pdf.showPage()
                pdf.setFont("DocumentText", 10)
                y = height - 42
            pdf.drawString(42, y, line)
            y -= 14

        for raw in text.expandtabs(4).splitlines() or [""]:
            line, used = [], 0.0
            for char in raw:
                char_width = pdfmetrics.stringWidth(char, "DocumentText", 10)
                if used + char_width > width - 84 and line:
                    draw("".join(line))
                    line, used = [], 0.0
                line.append(char)
                used += char_width
            draw("".join(line))
        pdf.save()

    def office_pdf(self, source, target):
        binary = shutil.which(self.office_binary)
        if binary is None:
            raise ConversionError(
                "Для офисных файлов нужен LibreOffice. Обратитесь к администратору."
            )
        with tempfile.TemporaryDirectory(prefix="office-", dir=target.parent) as directory:
            root = Path(directory)
            profile = root / "profile"
            (profile / "user").mkdir(parents=True)
            (profile / "user" / "registrymodifications.xcu").write_text(
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
                '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
                '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>'
                "</item></oor:items>",
                encoding="utf-8",
            )
            # Не передаём пользовательское имя как параметр команды.
            local_input = root / ("input" + source.suffix.lower())
            shutil.copyfile(source, local_input)
            with subprocess.Popen(
                [
                    binary,
                    f"-env:UserInstallation={profile.resolve().as_uri()}",
                    "--headless",
                    "--nologo",
                    "--nodefault",
                    "--norestore",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    str(root),
                    str(local_input),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ) as process:
                try:
                    process.wait(timeout=self.office_timeout)
                except subprocess.TimeoutExpired:
                    if os.name == "nt":
                        subprocess.run(
                            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            check=False,
                        )
                    process.kill()
                    process.wait()
                    raise ConversionError(
                        "LibreOffice не успел обработать файл. Попробуйте файл поменьше."
                    ) from None
            generated = root / "input.pdf"
            if process.returncode != 0 or not generated.exists():
                raise ConversionError("LibreOffice не смог преобразовать документ.")
            shutil.move(generated, target)
