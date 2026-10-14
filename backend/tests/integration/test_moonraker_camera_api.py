"""Integration tests for GET /api/v1/printers/{id}/camera/snapshot on a U1.

Real route + real manager lookup; only the client's capture is scripted.
The contract under test: a Moonraker printer is served from the repeater
monitor (never the Bambu RTSP/chamber path), and a failed capture is a 503,
not an empty 200.
"""

import pytest
from httpx import AsyncClient

from backend.app.services.moonraker_client import MoonrakerClient
from backend.app.services.printer_manager import printer_manager

JPEG = b"\xff\xd8" + b"frame" + b"\xff\xd9"


class ScriptedCameraClient(MoonrakerClient):
    def __init__(self, frame):
        super().__init__(ip_address="192.168.4.102", serial_number="CAMTEST")
        self._frame = frame
        self.captures = 0

    def capture_snapshot(self):
        self.captures += 1
        return self._frame


@pytest.fixture
def u1_camera():
    installed = []

    def _install(printer_id, frame):
        client = ScriptedCameraClient(frame)
        printer_manager._clients[printer_id] = client
        printer_manager._models[printer_id] = "U1"
        installed.append(printer_id)
        return client

    yield _install

    for printer_id in installed:
        printer_manager._clients.pop(printer_id, None)
        printer_manager._models.pop(printer_id, None)


class TestMoonrakerCameraSnapshot:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_snapshot_serves_the_monitor_frame(self, async_client: AsyncClient, printer_factory, u1_camera):
        printer = await printer_factory(model="U1", access_code="")
        client = u1_camera(printer.id, JPEG)
        response = await async_client.get(f"/api/v1/printers/{printer.id}/camera/snapshot")
        assert response.status_code == 200, response.text
        assert response.headers["content-type"] == "image/jpeg"
        assert response.content == JPEG
        assert client.captures == 1

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_failed_capture_is_503_not_an_empty_success(
        self, async_client: AsyncClient, printer_factory, u1_camera
    ):
        printer = await printer_factory(model="U1", access_code="")
        u1_camera(printer.id, None)
        response = await async_client.get(f"/api/v1/printers/{printer.id}/camera/snapshot")
        assert response.status_code == 503
