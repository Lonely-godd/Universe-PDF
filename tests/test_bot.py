import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from universe_pdf.bot import Controller
from universe_pdf.config import Settings
from universe_pdf.sessions import SessionStore


@pytest.fixture
def harness(tmp_path):
    settings = Settings(token="123:example")
    store = SessionStore(tmp_path)
    runner = AsyncMock()

    async def build(files, output):
        output.write_bytes(b"%PDF-test")

    runner.side_effect = build
    controller = Controller(settings, store, runner)
    message = SimpleNamespace(reply_text=AsyncMock(), reply_document=AsyncMock(),
                              document=None, photo=[], text="")
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=1, type="private"),
                             effective_user=SimpleNamespace(id=1), effective_message=message)
    return controller, store, runner, update


def queued(store):
    session = store.get((1, 1))
    path = store.allocate(session, "photo.png", 3)
    path.write_bytes(b"abc")
    store.accept(session, path)
    return session


async def test_help_and_empty_compile(harness):
    bot, store, runner, update = harness
    await bot.help(update, None)
    assert "/compile" in update.effective_message.reply_text.call_args.args[0]
    await bot.compile(update, None)
    runner.assert_not_called()
    assert "файл" in update.effective_message.reply_text.call_args.args[0]


async def test_compile_sends_and_cleans(harness):
    bot, store, runner, update = harness
    session = queued(store)
    await bot.compile(update, None)
    await bot.wait_tasks()
    update.effective_message.reply_document.assert_awaited_once()
    assert not session.directory.exists()


async def test_delivery_failure_keeps_queue(harness):
    bot, store, runner, update = harness
    session = queued(store)
    update.effective_message.reply_document.side_effect = RuntimeError("network")
    await bot.compile(update, None)
    await bot.wait_tasks()
    assert session.files[0].exists()
    assert not session.busy
    assert not (session.directory / "document.pdf").exists()


async def test_cancel_during_conversion_suppresses_delivery(harness):
    bot, store, runner, update = harness
    session = queued(store)
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed(files, output):
        started.set()
        await release.wait()
        output.write_bytes(b"pdf")

    runner.side_effect = delayed
    await bot.compile(update, None)
    await started.wait()
    await bot.cancel(update, None)
    release.set()
    await bot.wait_tasks()
    update.effective_message.reply_document.assert_not_called()
    assert not session.directory.exists()


async def test_newpdf_and_group_rejection(harness):
    bot, store, runner, update = harness
    session = queued(store)
    await bot.newpdf(update, None)
    assert not session.directory.exists()
    update.effective_chat.type = "group"
    await bot.compile(update, None)
    assert "личн" in update.effective_message.reply_text.call_args.args[0]


async def test_document_download(harness):
    bot, store, runner, update = harness

    async def download(custom_path):
        custom_path.write_bytes(b"abc")

    remote = SimpleNamespace(download_to_drive=AsyncMock(side_effect=download))
    update.effective_message.document = SimpleNamespace(
        file_name="../test.png", file_size=3, get_file=AsyncMock(return_value=remote)
    )
    await bot.receive(update, None)
    assert len(store.get((1, 1)).files) == 1
