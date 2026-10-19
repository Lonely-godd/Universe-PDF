import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, NetworkError, TimedOut

from universe_pdf.download import download_file


@pytest.fixture
def transfer(tmp_path, monkeypatch):
    monkeypatch.setattr("universe_pdf.download.sleep", AsyncMock())
    path = tmp_path / "upload.pdf"

    async def save(custom_path, **kwargs):
        custom_path.write_bytes(b"complete")

    remote = SimpleNamespace(download_to_drive=AsyncMock(side_effect=save))
    incoming = SimpleNamespace(file_size=8, get_file=AsyncMock(return_value=remote))
    return incoming, remote, path


@pytest.mark.parametrize("stage", ["get_file", "download_to_drive"])
async def test_retries_transient_failure_and_keeps_one_complete_file(transfer, stage):
    incoming, remote, path = transfer
    if stage == "get_file":
        incoming.get_file.side_effect = [TimedOut(), remote]
    else:
        calls = 0

        async def interrupted(custom_path, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                custom_path.write_bytes(b"partial")
                raise NetworkError("connection lost")
            assert not custom_path.exists()
            custom_path.write_bytes(b"complete")

        remote.download_to_drive.side_effect = interrupted
    await download_file(incoming, path)
    assert incoming.get_file.await_count == 2
    assert path.read_bytes() == b"complete"
    assert list(path.parent.iterdir()) == [path]


async def test_download_uses_longer_timeouts_on_both_requests(transfer):
    incoming, remote, path = transfer
    await download_file(incoming, path)
    for request in (incoming.get_file, remote.download_to_drive):
        assert request.call_args.kwargs["read_timeout"] >= 30
        assert request.call_args.kwargs["connect_timeout"] >= 10


async def test_retry_limit_removes_partial_file(transfer):
    incoming, remote, path = transfer

    async def broken(custom_path, **kwargs):
        custom_path.write_bytes(b"partial")
        raise TimedOut()

    remote.download_to_drive.side_effect = broken
    with pytest.raises(TimedOut):
        await download_file(incoming, path)
    assert incoming.get_file.await_count == 3
    assert not path.exists()


async def test_invalid_file_is_not_retried(transfer):
    incoming, remote, path = transfer
    incoming.get_file.side_effect = BadRequest("file is too big")
    with pytest.raises(BadRequest):
        await download_file(incoming, path)
    assert incoming.get_file.await_count == 1
    remote.download_to_drive.assert_not_awaited()


async def test_total_deadline_stops_download_and_cleans(transfer):
    incoming, remote, path = transfer

    async def stalled(custom_path, **kwargs):
        custom_path.write_bytes(b"partial")
        await asyncio.sleep(10)

    remote.download_to_drive.side_effect = stalled
    with pytest.raises(TimeoutError):
        await download_file(incoming, path, total_timeout=0.02)
    assert not path.exists()
    assert incoming.get_file.await_count == 1


async def test_cancellation_cleans_without_retry(transfer):
    incoming, remote, path = transfer
    started = asyncio.Event()

    async def stalled(custom_path, **kwargs):
        custom_path.write_bytes(b"partial")
        started.set()
        await asyncio.Event().wait()

    remote.download_to_drive.side_effect = stalled
    task = asyncio.create_task(download_file(incoming, path))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not path.exists()
    assert incoming.get_file.await_count == 1


async def test_logs_stage_cause_and_size_without_private_data(transfer, caplog):
    incoming, remote, path = transfer
    private = "private-document-URL-and-token"
    error = TimedOut(private)
    error.__cause__ = ConnectionError(private)
    incoming.get_file.side_effect = error
    with pytest.raises(TimedOut):
        await download_file(incoming, path)
    assert "get_file" in caplog.text
    assert "ConnectionError" in caplog.text
    assert "size=8" in caplog.text
    assert private not in caplog.text


async def test_success_is_logged(transfer, caplog):
    incoming, remote, path = transfer
    with caplog.at_level("INFO"):
        await download_file(incoming, path)
    assert "Загрузка завершена" in caplog.text
    assert "bytes=8" in caplog.text


async def test_real_http_slow_response_overrides_short_client_timeout(tmp_path):
    from telegram import File
    from telegram.request import HTTPXRequest

    handlers = set()

    async def serve(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 8\r\nConnection: close\r\n\r\ncomp")
            await writer.drain()
            await asyncio.sleep(0.1)
            writer.write(b"lete")
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(task)

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    path = tmp_path / "slow.pdf"
    try:
        async with server, HTTPXRequest(read_timeout=0.01) as request:
            remote = File("example", "example", file_size=8, file_path=f"http://127.0.0.1:{port}/f")
            remote.set_bot(SimpleNamespace(request=request))
            incoming = SimpleNamespace(file_size=8, get_file=AsyncMock(return_value=remote))
            with pytest.raises(TimedOut):
                await remote.download_to_drive(custom_path=path)
            await download_file(incoming, path)
            assert path.read_bytes() == b"complete"
    finally:
        if handlers:
            await asyncio.gather(*handlers)
