import re
import tempfile
import zipfile
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from .conversion import ConversionError


def parse_pages(selection: str, count: int) -> list[int]:
    """Преобразует физические номера 1…N в индексы, сохраняя порядок."""
    selection = selection.strip().replace("–", "-").replace("—", "-")
    if not selection or len(selection) > 4096:
        raise ConversionError("Укажите страницы, например: 1, 3-5.")
    pages, seen = [], set()
    for item in selection.split(","):
        match = re.fullmatch(r"\s*([0-9]+)\s*(?:-\s*([0-9]+)\s*)?", item)
        if not match:
            raise ConversionError("Неверный формат. Пример: 1, 3-5. Используйте запятые и дефис.")
        first = int(match[1])
        last = int(match[2]) if match[2] else first
        if not 1 <= first <= last <= count:
            raise ConversionError(
                f"Номера должны быть от 1 до {count}, диапазоны — по возрастанию."
            )
        for number in range(first - 1, last):
            if number in seen:
                raise ConversionError(f"Страница {number + 1} указана несколько раз.")
            seen.add(number)
            pages.append(number)
    return pages


def split_groups(selection: str, count: int) -> list[list[int]]:
    if not selection.strip() or len(selection) > 4096:
        raise ConversionError("Укажите части, например: по 5 или 1-5; 6-10.")
    match = re.fullmatch(r"\s*(?:по\s+)?([0-9]+)\s*", selection, flags=re.IGNORECASE)
    if match:
        size = int(match[1])
        if not 1 <= size < count:
            raise ConversionError("Размер части должен быть от 1 до числа страниц минус один.")
        return [list(range(start, min(start + size, count))) for start in range(0, count, size)]
    groups = [parse_pages(part, count) for part in selection.split(";")]
    flat = [page for group in groups for page in group]
    if len(groups) < 2:
        raise ConversionError("Нужно минимум две части. Пример: по 5 или 1-5; 6-10.")
    if len(set(flat)) != len(flat):
        raise ConversionError("Части пересекаются: каждая страница должна встречаться один раз.")
    if set(flat) != set(range(count)):
        raise ConversionError(f"Части должны включать все страницы от 1 до {count}.")
    return groups


def selection_groups(action: str, selection: str, count: int) -> list[list[int]]:
    if action == "split":
        return split_groups(selection, count)
    if action not in {"extract", "remove"}:
        raise ConversionError("Неизвестное действие с PDF. Откройте /pdf.")
    pages = parse_pages(selection, count)
    if action == "remove":
        removed = set(pages)
        pages = [page for page in range(count) if page not in removed]
        if not pages:
            raise ConversionError("Нельзя удалить все страницы. Оставьте хотя бы одну.")
    return [pages]


class PDFTools:
    def __init__(self, *, max_pages=500, max_output_bytes=49_000_000):
        self.max_pages = max_pages
        self.max_output_bytes = max_output_bytes

    def read(self, source):
        try:
            reader = PdfReader(source)
            if reader.is_encrypted:
                raise ConversionError("PDF защищён паролем. Пришлите файл без пароля.")
            if not 1 <= len(reader.pages) <= self.max_pages:
                raise ConversionError(f"Допустимое число страниц PDF: от 1 до {self.max_pages}.")
            return reader
        except ConversionError:
            raise
        except Exception:
            raise ConversionError(
                "Не удалось прочитать PDF. Проверьте целостность файла."
            ) from None

    def inspect(self, source):
        return len(self.read(source).pages)

    def write_pages(self, reader, pages, target):
        with PdfWriter() as writer:
            for index in pages:
                page = reader.pages[index]
                for key in ("/AA", "/Annots"):
                    page.pop(key, None)
                writer.add_page(page)
            writer.add_metadata({"/Title": "Страницы документа"})
            writer.write(target)
        return target.stat().st_size

    def edit(self, source, output, action, selection):
        source, output = Path(source), Path(output)
        if source.resolve() == output.resolve():
            raise ConversionError("Результат нужно сохранять отдельно от исходного PDF.")
        output.unlink(missing_ok=True)
        try:
            reader = self.read(source)
            groups = selection_groups(action, selection, len(reader.pages))
            if action == "split":
                with tempfile.TemporaryDirectory(prefix="parts-", dir=output.parent) as directory:
                    total = 0
                    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                        for number, pages in enumerate(groups, 1):
                            target = Path(directory) / f"часть-{number:03d}.pdf"
                            total += self.write_pages(reader, pages, target)
                            self.check_size(total)
                            archive.write(target, arcname=target.name)
                            target.unlink()
            else:
                self.write_pages(reader, groups[0], output)
            self.check_size(output.stat().st_size)
        except Exception:
            output.unlink(missing_ok=True)
            raise

    def check_size(self, size):
        if size > self.max_output_bytes:
            raise ConversionError("Итоговый размер слишком большой. Выберите меньше страниц.")
