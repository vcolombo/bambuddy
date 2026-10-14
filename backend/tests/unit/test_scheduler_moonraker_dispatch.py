"""Queue dispatch to a Moonraker (Snapmaker U1) printer.

A pinned library .gcode item must travel the Moonraker branch end to end:
real filename (no Bambu ".3mf" wrapper), HTTP upload via the manager, no
Bambu FTP anywhere (upload, pre-delete, or failure cleanup), no 3MF cover
cache, and start by that same name — the name every downstream matcher
(expected-print, completion) keys on.
"""

from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.app.models  # noqa: F401 - populate Base.metadata
import backend.app.services.print_scheduler as scheduler_module
from backend.app.core.database import Base
from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services.print_scheduler import PrintScheduler
from backend.tests._fixtures.background_tasks import discarding_spawn_patch

GCODE = b"; printer_model = Snapmaker U1\nG28\n"


@pytest.fixture
async def dispatch_case(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    base_dir = tmp_path / "case"
    archive_rel = Path("archives") / "job.gcode"
    archive_abs = base_dir / archive_rel
    archive_abs.parent.mkdir(parents=True, exist_ok=True)
    archive_abs.write_bytes(GCODE)

    async with session_maker() as db:
        printer = Printer(
            name="Snapmaker U1",
            serial_number="8110026012700168D0YK",
            ip_address="192.168.4.102",
            access_code="",
            model="U1",
        )
        db.add(printer)
        await db.flush()
        archive = PrintArchive(
            printer_id=printer.id,
            filename="job.gcode",
            file_path=str(archive_rel),
            file_size=len(GCODE),
            status="completed",
        )
        db.add(archive)
        await db.flush()
        item = PrintQueueItem(printer_id=printer.id, archive_id=archive.id, status="pending")
        db.add(item)
        await db.commit()
        ids = SimpleNamespace(printer_id=printer.id, archive_id=archive.id, item_id=item.id)

    try:
        yield SimpleNamespace(session_maker=session_maker, base_dir=base_dir, ids=ids)
    finally:
        await engine.dispose()


def _patches(scheduler, ctx, *, upload_print_file, start_print, get_status):
    return [
        patch.object(scheduler_module.settings, "base_dir", ctx.base_dir),
        patch("backend.app.services.print_scheduler.printer_manager.is_connected", MagicMock(return_value=True)),
        patch("backend.app.services.print_scheduler.printer_manager.get_status", get_status),
        patch("backend.app.services.print_scheduler.printer_manager.start_print", start_print),
        patch("backend.app.services.print_scheduler.printer_manager.upload_print_file", upload_print_file),
        patch("backend.app.services.print_scheduler.printer_manager.set_awaiting_plate_clear", MagicMock()),
        patch(
            "backend.app.services.print_scheduler.get_ftp_retry_settings",
            AsyncMock(return_value=(False, 0, 0, 1.0)),
        ),
        patch("backend.app.services.print_scheduler.delete_file_async", AsyncMock(return_value=True)),
        patch("backend.app.services.print_scheduler.upload_file_async", AsyncMock(return_value=True)),
        patch("backend.app.services.print_scheduler.cache_3mf_download", MagicMock()),
        discarding_spawn_patch(),
        patch.object(scheduler, "_propagate_owner_to_printer_manager", AsyncMock()),
        patch.object(scheduler, "_power_off_if_needed", AsyncMock()),
        patch.object(scheduler, "_preheat_and_soak", AsyncMock()),
    ]


@pytest.mark.asyncio
async def test_u1_dispatch_uploads_over_http_and_starts_by_real_name(dispatch_case):
    scheduler = PrintScheduler()
    upload_print_file = MagicMock(return_value=True)
    start_print = MagicMock(return_value=True)
    get_status = MagicMock(return_value=SimpleNamespace(state="IDLE", subtask_id=None, gcode_file=None))
    ftp_upload = AsyncMock(return_value=True)
    ftp_delete = AsyncMock(return_value=True)
    cache_3mf = MagicMock()

    patches = _patches(
        scheduler,
        dispatch_case,
        upload_print_file=upload_print_file,
        start_print=start_print,
        get_status=get_status,
    )
    patches += [
        patch("backend.app.services.print_scheduler.upload_file_async", ftp_upload),
        patch("backend.app.services.print_scheduler.delete_file_async", ftp_delete),
        patch("backend.app.services.print_scheduler.cache_3mf_download", cache_3mf),
    ]

    async with dispatch_case.session_maker() as db:
        item = await db.get(PrintQueueItem, dispatch_case.ids.item_id)
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            await scheduler._start_print(db, item)

    upload_print_file.assert_called_once_with(dispatch_case.ids.printer_id, GCODE, "job.gcode")
    start_print.assert_called_once()
    assert start_print.call_args.args[1] == "job.gcode", "start must use the Moonraker name, not job.gcode.3mf"
    ftp_upload.assert_not_awaited()
    ftp_delete.assert_not_awaited()
    cache_3mf.assert_not_called()

    async with dispatch_case.session_maker() as db:
        final = await db.get(PrintQueueItem, dispatch_case.ids.item_id)
    assert final.status == "printing"


@pytest.mark.asyncio
async def test_u1_dispatch_never_touches_ftp(dispatch_case):
    scheduler = PrintScheduler()
    upload_print_file = MagicMock(return_value=True)
    holder = {"state": "IDLE"}

    class _Status:
        subtask_id = None
        gcode_file = None

        @property
        def state(self):
            return holder["state"]

    def _busy_start(*args, **kwargs):
        holder["state"] = "RUNNING"
        return False

    start_print = MagicMock(side_effect=_busy_start)
    ftp_upload = AsyncMock(return_value=True)
    ftp_delete = AsyncMock(return_value=True)
    cache_3mf = MagicMock()

    patches = _patches(
        scheduler,
        dispatch_case,
        upload_print_file=upload_print_file,
        start_print=start_print,
        get_status=MagicMock(return_value=_Status()),
    )
    patches += [
        patch("backend.app.services.print_scheduler.upload_file_async", ftp_upload),
        patch("backend.app.services.print_scheduler.delete_file_async", ftp_delete),
        patch("backend.app.services.print_scheduler.cache_3mf_download", cache_3mf),
    ]

    async with dispatch_case.session_maker() as db:
        item = await db.get(PrintQueueItem, dispatch_case.ids.item_id)
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            await scheduler._start_print(db, item)

    # Busy-refused: deferred, and no FTP emit of any kind happened on the way.
    async with dispatch_case.session_maker() as db:
        final = await db.get(PrintQueueItem, dispatch_case.ids.item_id)
    assert final.status == "pending"
    ftp_upload.assert_not_awaited()
    ftp_delete.assert_not_awaited()
    cache_3mf.assert_not_called()


@pytest.mark.asyncio
async def test_bambu_sliced_file_is_failed_before_any_upload(dispatch_case):
    """Exact-match guard: a slice declaring a Bambu model must never be
    uploaded to Klipper, even though pinned items skip every other
    compat check in the pipeline."""
    scheduler = PrintScheduler()
    upload_print_file = MagicMock(return_value=True)
    start_print = MagicMock(return_value=True)
    get_status = MagicMock(return_value=SimpleNamespace(state="IDLE", subtask_id=None, gcode_file=None))

    async with dispatch_case.session_maker() as db:
        archive = await db.get(PrintArchive, dispatch_case.ids.archive_id)
        archive.sliced_for_model = "X1C"
        await db.commit()

    async with dispatch_case.session_maker() as db:
        item = await db.get(PrintQueueItem, dispatch_case.ids.item_id)
        with ExitStack() as stack:
            for p in _patches(
                scheduler,
                dispatch_case,
                upload_print_file=upload_print_file,
                start_print=start_print,
                get_status=get_status,
            ):
                stack.enter_context(p)
            await scheduler._start_print(db, item)

    upload_print_file.assert_not_called()
    start_print.assert_not_called()
    async with dispatch_case.session_maker() as db:
        final = await db.get(PrintQueueItem, dispatch_case.ids.item_id)
    assert final.status == "failed"
    assert "X1C" in (final.error_message or "")


@pytest.mark.asyncio
async def test_slow_moonraker_start_keeps_the_loop_responsive(dispatch_case):
    """Real manager + real client branch: the Moonraker start POST is
    synchronous HTTP that can wait on Klipper. The scheduler must offload
    it, or one slow start stalls every other request (the #1445 class).
    A mocked start_print cannot catch that, so nothing here mocks it.
    """
    import asyncio
    import threading
    import time

    from backend.app.services.printer_manager import printer_manager as real_manager
    from backend.tests.unit.test_moonraker_u1 import FakeMoonrakerClient

    scheduler = PrintScheduler()
    client = FakeMoonrakerClient(ip_address="192.168.4.102", serial_number="U1TEST")
    client._poll_once()

    real_post = client._post_json
    beats = [0]
    beats_at_enter: list[int] = []
    beats_at_release: list[int] = []

    def slow_start_post(path, payload, timeout=30):
        if path == "/printer/print/start":
            release = threading.Event()

            def _release():
                beats_at_release.append(beats[0])
                release.set()

            beats_at_enter.append(beats[0])
            threading.Timer(2.5, _release).start()
            release.wait(timeout=10)
        return real_post(path, payload)

    client._post_json = slow_start_post
    printer_id = dispatch_case.ids.printer_id
    real_manager._clients[printer_id] = client
    real_manager._models[printer_id] = "U1"

    patches = [
        patch.object(scheduler_module.settings, "base_dir", dispatch_case.base_dir),
        patch(
            "backend.app.services.print_scheduler.get_ftp_retry_settings",
            AsyncMock(return_value=(False, 0, 0, 1.0)),
        ),
        discarding_spawn_patch(),
        patch.object(scheduler, "_propagate_owner_to_printer_manager", AsyncMock()),
        patch.object(scheduler, "_power_off_if_needed", AsyncMock()),
        patch.object(scheduler, "_preheat_and_soak", AsyncMock()),
    ]

    async def _heartbeat():
        while True:
            beats[0] += 1
            await asyncio.sleep(0.05)

    try:
        async with dispatch_case.session_maker() as db:
            item = await db.get(PrintQueueItem, dispatch_case.ids.item_id)
            with ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                heartbeat = asyncio.create_task(_heartbeat())
                try:
                    await scheduler._start_print(db, item)
                finally:
                    heartbeat.cancel()
        # The heartbeat only advances while the loop runs. If the slow
        # start blocked the loop, the count between the POST entering
        # and its 2.5s release barely moves.
        assert beats_at_enter and beats_at_release
        advanced = beats_at_release[0] - beats_at_enter[0]
        assert advanced >= 10, f"loop stalled behind slow start ({advanced} heartbeats in 2.5s)"
        assert ("/printer/print/start", {"filename": "job.gcode"}) in client.posts
    finally:
        real_manager._clients.pop(printer_id, None)
        real_manager._models.pop(printer_id, None)

    async with dispatch_case.session_maker() as db:
        final = await db.get(PrintQueueItem, dispatch_case.ids.item_id)
    assert final.status == "printing"
