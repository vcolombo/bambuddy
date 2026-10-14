"""Integration tests for POST /api/v1/printers/{id}/print-file.

Upload-then-start for Moonraker printers, exercised through the real
route, the real manager branch, and the real client guards. The transport
is faked at the HTTP boundary only — every refusal and every captured
call below runs production code.
"""

import pytest
from httpx import AsyncClient

from backend.app.services.moonraker_client import MoonrakerClient
from backend.app.services.printer_manager import printer_manager


def _status(print_state="standby", filename=""):
    return {
        "print_stats": {"state": print_state, "filename": filename, "info": {}},
        "display_status": {"progress": 0.0},
        "virtual_sdcard": {"is_active": False},
        "toolhead": {"extruder": "extruder"},
        "heater_bed": {"temperature": 24.0, "target": 0.0},
        "extruder": {"temperature": 27.0, "target": 0.0},
        "extruder1": {"temperature": 27.0, "target": 0.0},
        "extruder2": {"temperature": 27.0, "target": 0.0},
        "extruder3": {"temperature": 27.0, "target": 0.0},
    }


class WireFakeClient(MoonrakerClient):
    """Real client logic, canned HTTP. Steer via .payloads."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.payloads = {"status": _status()}
        self.calls = []

    def _get(self, path, params=None):
        if path == "/server/info":
            return {"klippy_connected": True, "klippy_state": "ready", "components": []}
        if path == "/printer/info":
            return {"software_version": "test"}
        return {"status": self.payloads["status"]}

    def _post_json(self, path, payload, timeout=30):
        self.calls.append((path, payload))
        return {"result": "ok"}

    def _post_multipart(self, path, fields, filename, data):
        self.calls.append((path, fields, filename, data))
        return {"action": "create_file"}


@pytest.fixture
def u1_wire():
    """A connected U1 on the manager with a steerable transport."""
    installed = []

    def _install(printer_id, print_state="standby", filename=""):
        client = WireFakeClient(ip_address="192.168.4.102", serial_number=f"WIRE{printer_id}")
        client.payloads["status"] = _status(print_state, filename)
        client._poll_once()
        printer_manager._clients[printer_id] = client
        printer_manager._models[printer_id] = "U1"
        installed.append(printer_id)
        return client

    yield _install

    for printer_id in installed:
        printer_manager._clients.pop(printer_id, None)
        printer_manager._models.pop(printer_id, None)


class TestMoonrakerPrintFileAPI:
    """POST /api/v1/printers/{id}/print-file end to end."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_gcode_upload_then_start(self, async_client: AsyncClient, printer_factory, u1_wire):
        """User-sliced gcode stages and starts; the wire shows upload then start."""
        printer = await printer_factory(model="U1", access_code="")
        client = u1_wire(printer.id)
        response = await async_client.post(
            f"/api/v1/printers/{printer.id}/print-file",
            files={"file": ("cube.gcode", b"G28\n")},
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"started": True, "filename": "cube.gcode"}
        assert client.calls[0][0] == "/server/files/upload"
        assert client.calls[1] == ("/printer/print/start", {"filename": "cube.gcode"})

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_plate_start_needs_explicit_plate(self, async_client: AsyncClient, printer_factory, u1_wire):
        """A 3MF already on the printer starts only with a chosen plate."""
        printer = await printer_factory(model="U1", access_code="")
        client = u1_wire(printer.id)
        denied = await async_client.post(
            f"/api/v1/printers/{printer.id}/print-file",
            data={"server_path": "plates/project.3mf"},
        )
        assert denied.status_code == 400
        assert denied.json()["detail"]["code"] == "plate_required"
        assert client.calls == []
        response = await async_client.post(
            f"/api/v1/printers/{printer.id}/print-file",
            data={"server_path": "plates/project.3mf", "plate": "2"},
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"started": True, "path": "plates/project.3mf", "plate": 2}
        assert client.calls == [("/server/files/start_local_print", {"path": "plates/project.3mf", "print_plate": 2})]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_busy_printer_refuses_before_wire(self, async_client: AsyncClient, printer_factory, u1_wire):
        """A RUNNING printer refuses the upload with printer_busy; nothing posts."""
        printer = await printer_factory(model="U1", access_code="")
        client = u1_wire(printer.id, print_state="printing", filename="other.gcode")
        response = await async_client.post(
            f"/api/v1/printers/{printer.id}/print-file",
            files={"file": ("cube.gcode", b"G28\n")},
        )
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "printer_busy"
        assert client.calls == []

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_wrong_shapes_rejected(self, async_client: AsyncClient, printer_factory, u1_wire):
        """STL bytes, bare server paths, and Bambu printers never dispatch."""
        printer = await printer_factory(model="U1", access_code="")
        client = u1_wire(printer.id)
        stl = await async_client.post(
            f"/api/v1/printers/{printer.id}/print-file", files={"file": ("model.stl", b"solid\n")}
        )
        assert stl.status_code == 400
        both = await async_client.post(
            f"/api/v1/printers/{printer.id}/print-file",
            files={"file": ("cube.gcode", b"G28\n")},
            data={"server_path": "plates/project.3mf", "plate": "1"},
        )
        assert both.status_code == 400
        assert client.calls == []
        bambu = await printer_factory(model="P1S")
        refused = await async_client.post(
            f"/api/v1/printers/{bambu.id}/print-file", files={"file": ("cube.gcode", b"G28\n")}
        )
        assert refused.status_code == 400
        assert refused.json()["detail"]["code"] == "not_moonraker"


class TestMoonrakerDispatchLoopLiveness:
    """A slow upload must not stall the API loop (the #1445 class)."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_loop_stays_responsive_during_slow_upload(self, async_client: AsyncClient, printer_factory, u1_wire):
        import asyncio
        import time

        printer = await printer_factory(model="U1", access_code="")
        client = u1_wire(printer.id)
        real_upload = MoonrakerClient.upload_gcode.__get__(client)

        def slow_upload(data, filename):
            time.sleep(3)
            return real_upload(data, filename)

        client.upload_gcode = slow_upload
        try:
            post = asyncio.create_task(
                async_client.post(
                    f"/api/v1/printers/{printer.id}/print-file",
                    files={"file": ("cube.gcode", b"G28\n")},
                )
            )
            # The ASGI app runs inline on this loop: if the route blocks,
            # even a bare sleep stretches past the upload.
            t0 = time.monotonic()
            await asyncio.sleep(0.5)
            sleep_elapsed = time.monotonic() - t0
            post_result = await post
            assert post_result.status_code == 200, post_result.text
            assert sleep_elapsed < 2.0, f"loop stalled {sleep_elapsed:.1f}s behind upload"
        finally:
            del client.upload_gcode


class TestMoonrakerNozzleBounds:
    """The card submits status.active_extruder; T2/T3 must not 422 on a U1."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_t3_nozzle_temperature_accepted_on_u1(self, async_client: AsyncClient, printer_factory, u1_wire):
        printer = await printer_factory(model="U1", access_code="")
        client = u1_wire(printer.id)
        response = await async_client.post(f"/api/v1/printers/{printer.id}/temperature/nozzle?target=210&nozzle=3")
        assert response.status_code == 200, response.text
        assert ("/printer/gcode/script", {"script": "M104 T3 S210"}) in client.calls

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_nozzle_bounds_stay_model_appropriate(self, async_client: AsyncClient, printer_factory, u1_wire):
        u1 = await printer_factory(model="U1", access_code="")
        u1_wire(u1.id)
        too_far = await async_client.post(f"/api/v1/printers/{u1.id}/temperature/nozzle?target=210&nozzle=4")
        assert too_far.status_code == 422
        bambu = await printer_factory(model="P1S")
        dual_only = await async_client.post(f"/api/v1/printers/{bambu.id}/temperature/nozzle?target=210&nozzle=2")
        assert dual_only.status_code == 400
