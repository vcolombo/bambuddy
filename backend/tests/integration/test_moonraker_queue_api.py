"""Integration: library upload -> queue -> Moonraker dispatch, end to end.

Raw .gcode ingestion is declaration-gated (a Moonraker slice is printable
as-is; a Bambu slice keeps the #1401 rejection), and a pinned U1 queue
item then dispatches over HTTP with no FTP anywhere in the path. Only
the printer transport is faked (the unit fake); the upload, queue,
archive-parse and scheduler code is all production.
"""

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

from backend.app.models.print_queue import PrintQueueItem
from backend.app.services.print_scheduler import PrintScheduler
from backend.app.services.printer_manager import printer_manager as real_manager
from backend.tests._fixtures.background_tasks import discarding_spawn_patch
from backend.tests.unit.test_moonraker_u1 import FakeMoonrakerClient


def _gcode(declared_model: str) -> bytes:
    filler = b"G1 X1 Y1\n" * 20000  # push the declaration past the head window
    return b"; generated\n" + filler + f"; printer_model = {declared_model}\n".encode()


class TestMoonrakerQueueUpload:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_snapmaker_gcode_uploads_bambu_gcode_still_rejected(self, async_client: AsyncClient):
        ok = await async_client.post(
            "/api/v1/library/files/",
            files={"file": ("tolerance.gcode", _gcode("Snapmaker U1"))},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["file_type"] == "gcode"

        rejected = await async_client.post(
            "/api/v1/library/files/",
            files={"file": ("benchy.gcode", _gcode("Bambu Lab X1 Carbon"))},
        )
        assert rejected.status_code == 400

        undeclared = await async_client.post(
            "/api/v1/library/files/",
            files={"file": ("mystery.gcode", b"G28\nG1 X1\n")},
        )
        assert undeclared.status_code == 400

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_upload_queue_dispatch_reaches_the_printer(
        self, async_client: AsyncClient, printer_factory, db_session
    ):
        import backend.app.services.print_scheduler as scheduler_module

        printer = await printer_factory(model="U1", access_code="")

        upload = await async_client.post(
            "/api/v1/library/files/",
            files={"file": ("tolerance.gcode", _gcode("Snapmaker U1"))},
        )
        assert upload.status_code == 200, upload.text
        library_file_id = upload.json()["id"]

        queued = await async_client.post(
            "/api/v1/queue/",
            json={"printer_id": printer.id, "library_file_id": library_file_id},
        )
        assert queued.status_code in (200, 201), queued.text
        item_id = queued.json()["id"]

        client = FakeMoonrakerClient(ip_address="192.168.4.102", serial_number="U1TEST")
        client._poll_once()
        real_manager._clients[printer.id] = client
        real_manager._models[printer.id] = "U1"

        ftp_upload = AsyncMock(return_value=True)
        ftp_delete = AsyncMock(return_value=True)
        cache_3mf = MagicMock()
        scheduler = PrintScheduler()
        patches = [
            patch(
                "backend.app.services.print_scheduler.get_ftp_retry_settings",
                AsyncMock(return_value=(False, 0, 0, 1.0)),
            ),
            patch("backend.app.services.print_scheduler.upload_file_async", ftp_upload),
            patch("backend.app.services.print_scheduler.delete_file_async", ftp_delete),
            patch("backend.app.services.print_scheduler.cache_3mf_download", cache_3mf),
            discarding_spawn_patch(),
            patch.object(scheduler, "_propagate_owner_to_printer_manager", AsyncMock()),
            patch.object(scheduler, "_power_off_if_needed", AsyncMock()),
            patch.object(scheduler, "_preheat_and_soak", AsyncMock()),
        ]
        try:
            item = await db_session.get(PrintQueueItem, item_id)
            with ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                await scheduler._start_print(db_session, item)
        finally:
            real_manager._clients.pop(printer.id, None)
            real_manager._models.pop(printer.id, None)
            try:
                from backend.app import main as main_mod

                for key in [k for k in main_mod._expected_prints if k[0] == printer.id]:
                    main_mod._expected_prints.pop(key, None)
            except Exception:
                pass

        db_session.expire_all()
        final = await db_session.get(PrintQueueItem, item_id)
        assert final.status == "printing"
        posted = [p for p in client.posts if p[0] == "/printer/print/start"]
        assert posted == [("/printer/print/start", {"filename": "tolerance.gcode"})]
        ftp_upload.assert_not_awaited()
        ftp_delete.assert_not_awaited()
        cache_3mf.assert_not_called()
        assert scheduler_module is not None  # import surface used by patches above
