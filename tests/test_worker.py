import asyncio
from unittest.mock import AsyncMock

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


async def test_timeout_terminates_worker_and_excludes_secrets(tmp_path, picture, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "do-not-pass-this-token")
    monkeypatch.setenv("UNRELATED_SECRET", "do-not-pass-this-either")
    process = AsyncMock()
    process.returncode = None

    async def wait():
        await asyncio.sleep(10)

    process.wait.side_effect = wait
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr("universe_pdf.worker.asyncio.create_subprocess_exec", spawn)
    runner = WorkerRunner(Settings(token="123:example", conversion_timeout=0.01))
    runner.stop_tree = AsyncMock()
    with pytest.raises(ConversionError, match="Время"):
        await runner([picture], tmp_path / "out.pdf")
    runner.stop_tree.assert_awaited_once_with(process)
    assert "BOT_TOKEN" not in spawn.call_args.kwargs["env"]
    assert "UNRELATED_SECRET" not in spawn.call_args.kwargs["env"]
    assert not (tmp_path / "job.json").exists()
