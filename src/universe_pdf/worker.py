import asyncio
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from .conversion import ConversionError, Converter


class WorkerRunner:
    def __init__(self, settings):
        self.settings = settings
        self.slots = asyncio.Semaphore(settings.conversion_concurrency)

    async def __call__(self, files, output):
        async with self.slots:
            manifest = output.parent / "job.json"
            status = output.parent / "status.json"
            manifest.write_text(
                json.dumps(
                    {
                        "files": [str(path.resolve()) for path in files],
                        "output": str(output.resolve()),
                        "status": str(status.resolve()),
                        "options": {
                            "font_path": self.settings.font_path,
                            "office_binary": self.settings.office_binary,
                            "office_timeout": self.settings.office_timeout,
                            "max_pages": self.settings.max_pages,
                            "max_output_bytes": self.settings.max_output_bytes,
                        },
                    }
                ),
                encoding="utf-8",
            )
            # Рабочий процесс не получает токен бота и другие секреты окружения.
            safe_keys = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "LANG", "LC_ALL"}
            environment = {
                key: value for key, value in os.environ.items() if key.upper() in safe_keys
            }
            environment["PYTHONUTF8"] = "1"
            process = None
            try:
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "universe_pdf.worker",
                    str(manifest.resolve()),
                    env=environment,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=(os.name != "nt"),
                )
                try:
                    await asyncio.wait_for(process.wait(), self.settings.conversion_timeout)
                except TimeoutError:
                    raise ConversionError(
                        "Время сборки истекло. Уменьшите число или размер файлов."
                    ) from None
                if not status.exists():
                    raise ConversionError(
                        "Не удалось завершить обработку. Попробуйте меньше файлов."
                    )
                result = json.loads(status.read_text(encoding="utf-8"))
                if process.returncode != 0 or not result.get("ok"):
                    raise ConversionError(result.get("error", "Не удалось собрать PDF."))
            finally:
                if process is not None:
                    await self.stop_tree(process)
                manifest.unlink(missing_ok=True)
                status.unlink(missing_ok=True)

    @staticmethod
    async def stop_tree(process):
        if os.name == "nt":
            if process.returncode is None:
                await asyncio.to_thread(
                    subprocess.run,
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
        else:
            # Включая дочерние процессы LibreOffice, оставшиеся после таймаута.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.returncode is None:
            await process.wait()


def main():
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    try:
        Converter(**job["options"]).compile([Path(p) for p in job["files"]], Path(job["output"]))
        result = {"ok": True}
    except ConversionError as error:
        result = {"ok": False, "error": str(error)}
    except Exception:
        result = {"ok": False, "error": "Ошибка обработки документа. Попробуйте другой файл."}
    Path(job["status"]).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
