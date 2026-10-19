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
    message = SimpleNamespace(
        reply_text=AsyncMock(), reply_document=AsyncMock(), document=None, photo=[], text=""
    )
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=1, type="private"),
        effective_user=SimpleNamespace(id=1),
        effective_message=message,
    )
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
    await asyncio.wait_for(started.wait(), 5)
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

    async def download(custom_path, **kwargs):
        custom_path.write_bytes(b"abc")

    remote = SimpleNamespace(download_to_drive=AsyncMock(side_effect=download))
    update.effective_message.document = SimpleNamespace(
        file_name="../test.png", file_size=3, get_file=AsyncMock(return_value=remote)
    )
    await bot.receive(update, None)
    assert len(store.get((1, 1)).files) == 1


async def test_conversion_failure_preserves_queue(harness):
    from universe_pdf.conversion import ConversionError

    bot, store, runner, update = harness
    session = queued(store)
    runner.side_effect = ConversionError("Файл №1: повреждён")
    await bot.compile(update, None)
    await bot.wait_tasks()
    assert session.files[0].exists()
    assert not session.busy
    update.effective_message.reply_document.assert_not_called()
    assert "Файл №1" in update.effective_message.reply_text.call_args.args[0]


async def test_restricted_access(harness):
    from dataclasses import replace

    bot, store, runner, update = harness
    bot.settings = replace(bot.settings, allowed_user_ids=frozenset({999}))
    await bot.newpdf(update, None)
    await bot.receive(update, None)
    assert not store.sessions
    assert "ограничен" in update.effective_message.reply_text.call_args.args[0]


async def test_photo_album_order(harness):
    bot, store, runner, update = harness
    for value in (b"first", b"second", b"third"):

        async def download(custom_path, content=value, **kwargs):
            custom_path.write_bytes(content)

        remote = SimpleNamespace(download_to_drive=AsyncMock(side_effect=download))
        update.effective_message.photo = [
            SimpleNamespace(file_size=len(value), get_file=AsyncMock(return_value=remote))
        ]
        await bot.receive(update, None)
    assert [path.read_bytes() for path in store.get((1, 1)).files] == [
        b"first",
        b"second",
        b"third",
    ]


async def test_failed_download_removes_partial_file(harness):
    bot, store, runner, update = harness

    async def download(custom_path, **kwargs):
        custom_path.write_bytes(b"partial")
        raise RuntimeError("download interrupted")

    remote = SimpleNamespace(download_to_drive=AsyncMock(side_effect=download))
    update.effective_message.document = SimpleNamespace(
        file_name="photo.png", file_size=10, get_file=AsyncMock(return_value=remote)
    )
    await bot.receive(update, None)
    session = store.get((1, 1))
    assert not session.files
    assert not list(session.directory.iterdir())


async def test_cancel_during_send_reports_it_is_too_late(harness):
    bot, store, runner, update = harness
    session = queued(store)
    session.busy = session.sending = True
    await bot.cancel(update, None)
    assert not session.cancelled
    assert "отправляется" in update.effective_message.reply_text.call_args.args[0]


async def upload_pdf(harness, count=20):
    import io

    from pypdf import PdfWriter

    from universe_pdf.worker import WorkerRunner

    bot, store, _, update = harness
    bot.runner = WorkerRunner(bot.settings)
    writer = PdfWriter()
    for number in range(1, count + 1):
        writer.add_blank_page(width=200 + number, height=400)
    content = io.BytesIO()
    writer.write(content)
    payload = content.getvalue()

    async def download(custom_path, **kwargs):
        custom_path.write_bytes(payload)

    async def capture(**kwargs):
        update.sent_bytes = kwargs["document"].read()

    update.effective_message.reply_document.side_effect = capture
    remote = SimpleNamespace(download_to_drive=AsyncMock(side_effect=download))
    update.effective_message.document = SimpleNamespace(
        file_name="twenty.pdf", file_size=len(payload), get_file=AsyncMock(return_value=remote)
    )
    await bot.receive(update, None)
    return store.get((1, 1))


async def choose(harness, text):
    bot, _, _, update = harness
    update.effective_message.text = text
    await bot.text(update, None)
    await bot.wait_tasks()


async def test_pdf_menu_and_real_extraction_dialog(harness):
    import io

    from pypdf import PdfReader

    bot, store, runner, update = harness
    other = queued(store).files[0]
    session = await upload_pdf(harness)
    assert "Выберите действие" in update.effective_message.reply_text.call_args.args[0]
    await choose(harness, "Извлечь страницы")
    assert "20" in update.effective_message.reply_text.call_args.args[0]
    await choose(harness, "20, 1, 3-5")
    pages = PdfReader(io.BytesIO(update.sent_bytes)).pages
    assert [int(page.mediabox.width) - 200 for page in pages] == [20, 1, 3, 4, 5]
    assert session.pdf_target.exists()
    assert other.exists()
    assert len(session.files) == 2
    assert session.pdf_action is None
    assert not (session.directory / "pages.pdf").exists()


async def test_invalid_input_then_remove(harness):
    import io

    from pypdf import PdfReader

    bot, store, runner, update = harness
    session = await upload_pdf(harness)
    await choose(harness, "Удалить страницы")
    await choose(harness, "0")
    assert "от 1 до 20" in update.effective_message.reply_text.call_args.args[0]
    assert session.pdf_action == "remove"
    update.effective_message.reply_document.assert_not_called()
    await choose(harness, "2-19")
    assert len(PdfReader(io.BytesIO(update.sent_bytes)).pages) == 2


async def test_split_dialog_and_reopen_menu(harness):
    import io
    import zipfile

    bot, store, runner, update = harness
    session = await upload_pdf(harness)
    await choose(harness, "Разделить на части")
    await choose(harness, "по 5")
    with zipfile.ZipFile(io.BytesIO(update.sent_bytes)) as archive:
        assert len(archive.namelist()) == 4
    await bot.pdf_menu(update, None)
    assert session.pdf_target.exists()
    await choose(harness, "Добавить в сборку")
    assert len(session.files) == 1  # Повторно не добавляем.


async def test_pdf_delivery_failure_can_retry(harness):
    bot, store, runner, update = harness
    session = await upload_pdf(harness)
    capture = update.effective_message.reply_document.side_effect
    await choose(harness, "Извлечь страницы")
    update.effective_message.reply_document.side_effect = RuntimeError("network")
    await choose(harness, "1")
    assert session.pdf_action == "extract"
    assert session.pdf_target.exists()
    assert not session.busy
    update.effective_message.reply_document.side_effect = capture
    await choose(harness, "1")
    assert session.pdf_action is None


async def test_cancel_pdf_operation_suppresses_send(harness):
    bot, store, runner, update = harness
    session = await upload_pdf(harness)
    await choose(harness, "Извлечь страницы")
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed(source, output, action, selection):
        started.set()
        await release.wait()
        output.write_bytes(b"pdf")

    bot.runner.edit_pdf = delayed
    update.effective_message.text = "1"
    await bot.text(update, None)
    await asyncio.wait_for(started.wait(), 5)

    await bot.cancel(update, None)
    release.set()
    await bot.wait_tasks()
    update.effective_message.reply_document.assert_not_called()
    assert not session.directory.exists()


async def test_latest_pdf_replaces_selection_and_cancel_cleans(harness):
    bot, store, runner, update = harness
    session = await upload_pdf(harness)
    await choose(harness, "Извлечь страницы")
    first = session.pdf_target
    await upload_pdf(harness, count=3)
    assert session.pdf_target != first
    assert session.pdf_action is None
    await choose(harness, "Извлечь страницы")
    assert session.pdf_page_count == 3
    await bot.cancel(update, None)
    await bot.pdf_menu(update, None)
    assert "Пришлите PDF" in update.effective_message.reply_text.call_args.args[0]


async def test_cancel_during_pdf_inspection(harness):
    bot, store, runner, update = harness
    session = await upload_pdf(harness)
    started, release = asyncio.Event(), asyncio.Event()

    async def inspect(source):
        started.set()
        await release.wait()
        return 20

    bot.runner.inspect_pdf = inspect
    update.effective_message.text = "Извлечь страницы"
    await bot.text(update, None)
    await asyncio.wait_for(started.wait(), 5)
    await bot.cancel(update, None)
    release.set()
    await bot.wait_tasks()
    assert not session.directory.exists()
    assert not any(
        "В PDF страниц" in call.args[0]
        for call in update.effective_message.reply_text.call_args_list
    )


async def test_pdf_is_not_visible_to_another_user(harness):
    bot, store, runner, update = harness
    original = await upload_pdf(harness)
    update.effective_user.id = 2
    update.effective_chat.id = 2
    await choose(harness, "Извлечь страницы")
    assert "Пришлите PDF" in update.effective_message.reply_text.call_args.args[0]
    assert original.pdf_target.exists()


async def test_unknown_text_without_sender_is_ignored(harness):
    bot, store, runner, update = harness
    update.effective_user = None
    update.effective_message.text = "1-3"
    await bot.text(update, None)
    update.effective_message.reply_text.assert_not_called()


async def test_compile_after_pdf_tools_still_works(harness):
    import io

    from pypdf import PdfReader

    bot, store, runner, update = harness
    session = await upload_pdf(harness)
    await choose(harness, "Извлечь страницы")
    await choose(harness, "1-2")
    await bot.compile(update, None)
    await bot.wait_tasks()
    assert len(PdfReader(io.BytesIO(update.sent_bytes)).pages) == 20
    assert not session.directory.exists()
