"""Snapmaker U1 read path: model registry, transport flags, client mapping.

The U1 speaks Moonraker, not Bambu MQTT. These pin the domain decisions:
"Snapmaker U1" normalizes to "U1", the U1 selects the Moonraker transport,
filament hangs off toolheads T0–T3, and every Bambu-only set denies it by
default. Client mapping runs against canned Moonraker payloads shaped like
the live hardware probe (4 extruders, per-tool sensors).
"""

import time

import pytest

from backend.app.services.moonraker_client import MoonrakerClient
from backend.app.utils.printer_models import (
    get_rod_type,
    has_ethernet,
    is_dual_nozzle_model,
    is_nozzle_rack_model,
    is_toolchanger_model,
    normalize_printer_model,
    uses_moonraker_transport,
)

SERVER_INFO = {
    "klippy_connected": True,
    "klippy_state": "ready",
    "components": ["repeater", "snapmakercloud", "client_manager"],
    "moonraker_version": "2.0.0",
}

PRINTER_INFO = {"software_version": "2.0.0.205_20260914173503"}


def _status(print_state="standby", filename="", progress=0.0, layer=0, total=0, active="extruder"):
    extruders = {
        name: {"temperature": 27.0, "target": 0.0} for name in ("extruder", "extruder1", "extruder2", "extruder3")
    }
    return {
        "print_stats": {
            "state": print_state,
            "filename": filename,
            "info": {"current_layer": layer, "total_layer": total},
        },
        "display_status": {"progress": progress},
        "virtual_sdcard": {"is_active": False},
        "toolhead": {"extruder": active},
        "heater_bed": {"temperature": 24.0, "target": 0.0},
        "fan": {"speed": 0.5, "rpm": 4000},
        "fan_generic cavity_fan": {"speed": 0.25, "rpm": 1200},
        "led cavity_led": {"color_data": [[0.0, 0.0, 0.0, 1.0]]},
        "temperature_sensor cavity": {"temperature": 32.0},
        **extruders,
    }


class FakeMoonrakerClient(MoonrakerClient):
    """MoonrakerClient with canned transport; set .payloads to steer it."""

    offline = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.payloads = {
            "info": dict(SERVER_INFO),
            "status": _status(),
            "printer_info": dict(PRINTER_INFO),
        }
        self.posts = []

    def _get(self, path, params=None):
        if self.offline:
            raise ConnectionError("unreachable")
        if path == "/server/info":
            return self.payloads["info"]
        if path == "/printer/info":
            return self.payloads["printer_info"]
        return {"status": self.payloads["status"]}

    def _post_json(self, path, payload, timeout=30):
        self.posts.append((path, payload))
        return {"result": "ok"}

    def _post_multipart(self, path, fields, filename, data):
        self.posts.append((path, fields, filename, data))
        return {"action": "create_file"}


def test_snapmaker_u1_normalizes_to_u1():
    assert normalize_printer_model("Snapmaker U1") == "U1"


def test_u1_selects_moonraker_transport_and_toolheads():
    # Predicates take normalized short names, like every other set here.
    assert normalize_printer_model("Snapmaker U1") == "U1"
    assert uses_moonraker_transport("U1")
    assert is_toolchanger_model("U1")
    assert not uses_moonraker_transport("P1S")
    assert not uses_moonraker_transport(None)
    assert not is_toolchanger_model("H2D")


def test_u1_denied_by_every_bambu_only_set():
    # Default-deny: the U1 is in no Bambu capability set.
    assert not is_dual_nozzle_model("U1")
    assert not is_nozzle_rack_model("U1")
    assert get_rod_type("U1") is None


def test_idle_maps_to_bambuddy_idle_with_four_tool_temps():
    client = FakeMoonrakerClient(ip_address="192.168.4.102", serial_number="U1TEST")
    assert client._poll_once()
    state = client.state
    assert state.connected
    assert state.state == "IDLE"
    assert state.temperatures["nozzle"] == 27.0
    assert state.temperatures["bed"] == 24.0
    assert state.temperatures["chamber"] == 32.0
    assert state.cooling_fan_speed == 50
    assert state.big_fan2_speed == 25
    assert state.chamber_light is True
    assert state.firmware_version == "2.0.0.205_20260914173503"


def test_printing_maps_state_progress_layers_and_active_tool():
    client = FakeMoonrakerClient(ip_address="192.168.4.102", serial_number="U1TEST")
    client.payloads["status"] = _status("printing", "cube.gcode", 0.42, 84, 200, "extruder2")
    client._poll_once()
    state = client.state
    assert state.state == "RUNNING"
    assert state.current_print == "cube.gcode"
    assert state.gcode_file == "cube.gcode"
    assert state.progress == 42.0
    assert (state.layer_num, state.total_layers) == (84, 200)
    assert state.active_extruder == 2


@pytest.mark.parametrize(
    ("klippy", "bambuddy"),
    [("paused", "PAUSE"), ("complete", "FINISH"), ("cancelled", "FAILED"), ("error", "FAILED")],
)
def test_terminal_and_pause_states_map(klippy, bambuddy):
    client = FakeMoonrakerClient(ip_address="192.168.4.102", serial_number="U1TEST")
    client.payloads["status"] = _status(klippy, "cube.gcode")
    client._poll_once()
    assert client.state.state == bambuddy


def test_start_and_complete_fire_once_on_transition():
    events = []
    client = FakeMoonrakerClient(
        ip_address="192.168.4.102",
        serial_number="U1TEST",
        on_print_start=lambda d: events.append(("start", d["filename"])),
        on_print_complete=lambda d: events.append(("complete", d["filename"])),
        on_print_running_observed=lambda d: events.append(("observed", d["filename"])),
    )
    client._poll_once()
    client.payloads["status"] = _status("printing", "cube.gcode")
    client._poll_once()
    client._poll_once()
    client.payloads["status"] = _status("complete", "cube.gcode", 1.0, 200, 200)
    client._poll_once()
    assert events == [("start", "cube.gcode"), ("complete", "cube.gcode")]


def test_resume_does_not_restart_the_job():
    events = []
    client = FakeMoonrakerClient(
        ip_address="192.168.4.102",
        serial_number="U1TEST",
        on_print_start=lambda d: events.append(("start", d["filename"])),
        on_print_complete=lambda d: events.append(("complete", d["filename"])),
    )
    client._poll_once()
    client.payloads["status"] = _status("printing", "cube.gcode")
    client._poll_once()
    client.payloads["status"] = _status("paused", "cube.gcode", 0.5, 100, 200)
    client._poll_once()
    client.payloads["status"] = _status("printing", "cube.gcode", 0.5, 100, 200)
    client._poll_once()
    assert events == [("start", "cube.gcode")]


def test_cancel_while_paused_still_completes_the_job():
    events = []
    client = FakeMoonrakerClient(
        ip_address="192.168.4.102",
        serial_number="U1TEST",
        on_print_start=lambda d: events.append(("start", d["filename"])),
        on_print_complete=lambda d: events.append(("complete", d["filename"])),
    )
    client._poll_once()
    client.payloads["status"] = _status("printing", "cube.gcode")
    client._poll_once()
    client.payloads["status"] = _status("paused", "cube.gcode", 0.5, 100, 200)
    client._poll_once()
    client.payloads["status"] = _status("cancelled", "cube.gcode", 0.5, 100, 200)
    client._poll_once()
    assert events == [("start", "cube.gcode"), ("complete", "cube.gcode")]


def test_adopted_mid_print_reports_observed_never_started():
    events = []
    client = FakeMoonrakerClient(
        ip_address="192.168.4.102",
        serial_number="U1TEST",
        on_print_start=lambda d: events.append(("start", d["filename"])),
        on_print_running_observed=lambda d: events.append(("observed", d["filename"])),
    )
    client.payloads["status"] = _status("printing", "cube.gcode", 0.3, 60, 200)
    client._poll_once()
    assert events == [("observed", "cube.gcode")]


def test_temperature_only_change_broadcasts_status():
    broadcasts = []
    client = FakeMoonrakerClient(
        ip_address="192.168.4.102",
        serial_number="U1TEST",
        on_state_change=lambda s: broadcasts.append(dict(s.temperatures)),
    )
    client._poll_once()
    assert len(broadcasts) == 1
    status = _status()
    status["extruder"]["temperature"] = 215.0
    client.payloads["status"] = status
    client._poll_once()
    assert len(broadcasts) == 2
    assert broadcasts[1]["nozzle"] == 215.0
    client._poll_once()
    assert len(broadcasts) == 2


def test_unreachable_poll_marks_error_and_stale_flips_connected():
    client = FakeMoonrakerClient(ip_address="192.168.4.102", serial_number="U1TEST")
    client.offline = True
    assert not client._poll_once()
    client.connect()
    try:
        assert client.last_connect_error == "unreachable"
        assert not client.state.connected
    finally:
        client.disconnect()
    client.offline = False
    client._poll_once()
    assert client.state.connected
    client._last_poll_time = time.time() - 61.0
    assert not client.check_staleness()
    assert not client.state.connected


def test_host_port_suffix_is_respected():
    client = FakeMoonrakerClient(ip_address="192.168.4.102:7126", serial_number="U1TEST")
    assert client.base_url == "http://192.168.4.102:7126"
    assert FakeMoonrakerClient(ip_address="192.168.4.102", serial_number="x").base_url.endswith(":7125")


def test_has_ethernet_shape_for_u1_is_boolean():
    # U1 ethernet presence is unconfirmed on hardware; the predicate must at
    # least answer without raising so capability review has a value to check.
    assert isinstance(has_ethernet("U1"), bool)


def test_bambu_only_surface_answers_no_instead_of_crashing():
    # Manager/routes reach for these unconditionally (status route included);
    # a Moonraker client must degrade, never AttributeError. Caught live:
    # get_drying_targets crashed GET /printers/1/status on first deploy.
    from backend.app.services.printer_manager import PrinterManager

    manager = PrinterManager()
    client = FakeMoonrakerClient(ip_address="192.168.4.102", serial_number="U1TEST")
    client._poll_once()
    manager._clients[3] = client
    manager._models[3] = "U1"
    try:
        assert manager.get_drying_targets(3) is None
        assert manager.get_logs(3) == []
        assert manager.is_logging_enabled(3) is False
        assert manager.send_drying_command(3, 0, 45, 60) is False
        assert client.execute_hms_action("0300", "resume") is False
        assert client._last_message_time > 0
    finally:
        del manager._clients[3]
        del manager._models[3]


def test_mark_power_off_undoes_on_next_poll():
    client = FakeMoonrakerClient(ip_address="192.168.4.102", serial_number="U1TEST")
    client._poll_once()
    assert client.state.connected
    assert client.mark_power_off() is True
    assert not client.state.connected
    assert client.mark_power_off() is False
    client._poll_once()
    assert client.state.connected


@pytest.mark.parametrize(
    ("klippy_terminal", "expected_status"),
    [("complete", "completed"), ("cancelled", "cancelled"), ("error", "failed")],
)
def test_completion_payload_speaks_consumer_status_vocabulary(klippy_terminal, expected_status):
    # main.on_print_complete reads data["status"] and defaults a missing
    # one to "completed" — a cancelled U1 print must never ride that
    # default into the archive/notification path as a success.
    completed = []
    client = FakeMoonrakerClient(
        ip_address="192.168.4.102",
        serial_number="U1TEST",
        on_print_complete=lambda d: completed.append(d),
    )
    client._poll_once()
    client.payloads["status"] = _status("printing", "cube.gcode")
    client._poll_once()
    client.payloads["status"] = _status(klippy_terminal, "cube.gcode", 1.0, 200, 200)
    client._poll_once()
    assert completed == [{"status": expected_status, "filename": "cube.gcode", "subtask_name": "cube.gcode"}]


@pytest.mark.asyncio
async def test_u1_completion_never_attempts_sd_card_delete():
    """Emit-side deny (domain model): print completion must not fire Bambu
    FTP deletes at a Moonraker printer. Caught live on first validation
    print: SD cleanup burned 3 retries x2 candidates against the U1."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock, patch

    printer = SimpleNamespace(model="U1", name="Snapmaker U1", ip_address="192.168.4.102", access_code="")

    async def fake_execute(stmt, *args, **kwargs):
        result = MagicMock()
        is_printer_lookup = "from printers" in str(stmt).lower()
        result.scalar_one_or_none = MagicMock(return_value=printer if is_printer_lookup else None)
        result.scalars = MagicMock(
            return_value=MagicMock(all=MagicMock(return_value=[]), first=MagicMock(return_value=None))
        )
        result.scalar = MagicMock(return_value=None)
        return result

    with (
        patch("backend.app.main.async_session") as mock_session_maker,
        patch("backend.app.main.ws_manager") as mock_ws,
        patch("backend.app.main.printer_manager") as mock_pm,
        patch("backend.app.main.mqtt_relay") as mock_relay,
        patch("backend.app.main.spawn_background_task"),
        patch("backend.app.main.clear_3mf_cache"),
        patch("backend.app.services.bambu_ftp.delete_file_async", new=AsyncMock()) as mock_delete,
    ):
        mock_ws.send_print_complete = AsyncMock()
        mock_relay.on_print_complete = AsyncMock()
        mock_pm.get_printer = MagicMock(return_value=MagicMock(name="Snapmaker U1", serial_number="U1TEST"))
        mock_pm.get_current_print_user = MagicMock(return_value=None)
        mock_pm.clear_current_print_user = MagicMock()
        mock_pm.set_awaiting_plate_clear = MagicMock()

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock()
        mock_session.execute = AsyncMock(side_effect=fake_execute)
        mock_session_maker.return_value = mock_session

        from backend.app.main import on_print_complete

        await on_print_complete(
            1,
            {"filename": "cube.gcode", "subtask_name": "cube.gcode", "status": "completed"},
        )

        mock_delete.assert_not_called()


@pytest.mark.asyncio
async def test_finish_photo_plate_moves_never_fire_for_u1():
    """Emit-side deny (domain model): the automatic finish-photo plate
    restore/park compensates for Bambu end G-code. Adding send_gcode must
    not silently enable that automatic motion on a Moonraker printer."""
    import logging
    from unittest.mock import MagicMock, patch

    sent = []
    client = FakeMoonrakerClient(ip_address="192.168.4.102", serial_number="U1TEST")
    client._poll_once()
    client.state.state = "FINISH"
    client.send_gcode = MagicMock(side_effect=lambda g: sent.append(g) or True)

    with patch("backend.app.main.printer_manager") as mock_pm:
        mock_pm.get_client = MagicMock(return_value=client)
        mock_pm.get_model = MagicMock(return_value="U1")

        from backend.app.main import _park_plate_after_finish_photo, _restore_plate_for_finish_photo

        logger = logging.getLogger("test")
        assert await _restore_plate_for_finish_photo(1, 20.0, logger) is False
        _park_plate_after_finish_photo(1, 20.0, logger)

    assert sent == []


@pytest.mark.asyncio
async def test_automatic_plate_check_emits_nothing_for_u1():
    """Emit-side deny (domain model): the automatic plate check switches
    the chamber light and can pause the print. With plate detection
    enabled on a U1 it must not run at all — no detection call, no
    light, no pause. Mirrors the harness in test_plate_check_chamber_light.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock, patch

    from backend.app.main import (
        _active_prints,
        _expected_print_creators,
        _expected_print_registered_at,
        _expected_prints,
        _print_ams_mappings,
        _timelapse_baselines,
    )

    for d in (
        _active_prints,
        _expected_prints,
        _expected_print_creators,
        _expected_print_registered_at,
        _print_ams_mappings,
        _timelapse_baselines,
    ):
        d.clear()

    events: list[str] = []
    printer = MagicMock()
    printer.id = 1
    printer.name = "Snapmaker U1"
    printer.model = "U1"
    printer.plate_detection_enabled = True
    printer.plate_detection_roi_x = None
    printer.plate_detection_roi_y = None
    printer.plate_detection_roi_w = None
    printer.plate_detection_roi_h = None
    printer.auto_archive = False
    printer.external_camera_enabled = False
    printer.external_camera_url = None

    client = MagicMock()
    client.state = SimpleNamespace(chamber_light=False)
    client.set_chamber_light = MagicMock(side_effect=lambda on: events.append(f"light:{'on' if on else 'off'}"))
    client.pause_print = MagicMock(side_effect=lambda: events.append("pause"))

    check_plate_empty = AsyncMock(
        return_value=SimpleNamespace(needs_calibration=False, is_empty=False, confidence=0.9, difference_percent=12.0)
    )

    def execute_router(stmt, *args, **kwargs):
        found = printer if "from printers" in str(stmt).lower() else None
        return MagicMock(
            scalar_one_or_none=MagicMock(return_value=found),
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[found] if found else []))),
        )

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock()
    session.execute = AsyncMock(side_effect=execute_router)

    with (
        patch("backend.app.main.async_session", return_value=session),
        patch("backend.app.main.notification_service") as mock_notif,
        patch("backend.app.main.smart_plug_manager") as mock_plug,
        patch("backend.app.main.ws_manager") as mock_ws,
        patch("backend.app.main.printer_manager") as mock_pm,
        patch("backend.app.main.mqtt_relay") as mock_relay,
        patch("backend.app.main.asyncio.sleep", new_callable=AsyncMock),
        patch("backend.app.main._capture_snapshot_for_notification", new_callable=AsyncMock, return_value=b"jpeg"),
        patch("backend.app.main._record_energy_start", new_callable=AsyncMock),
        patch("backend.app.main._load_objects_from_archive"),
        patch("backend.app.main._store_spoolman_print_data", new_callable=AsyncMock),
        patch("backend.app.main._send_print_start_notification", new_callable=AsyncMock),
        patch("backend.app.services.plate_detection.check_plate_empty", check_plate_empty),
    ):
        mock_notif.on_plate_not_empty = AsyncMock()
        mock_notif.on_print_start = AsyncMock()
        mock_plug.on_print_start = AsyncMock()
        mock_ws.broadcast = AsyncMock()
        mock_ws.send_print_start = AsyncMock()
        mock_ws.send_archive_updated = AsyncMock()
        mock_relay.on_print_start = AsyncMock()
        mock_pm.get_client = MagicMock(return_value=client)
        mock_pm.get_printer = MagicMock(return_value=MagicMock(name="Snapmaker U1", serial_number="U1TEST"))

        from backend.app.main import on_print_start

        await on_print_start(1, {"filename": "cube.gcode", "subtask_name": "cube.gcode"})

    check_plate_empty.assert_not_called()
    assert events == []


@pytest.mark.parametrize(
    ("declared", "expected"),
    [("Bambu Lab X1 Carbon", "X1C"), ("Snapmaker U1", "U1")],
)
def test_plain_gcode_trailer_model_reaches_sliced_for(tmp_path, declared, expected):
    """Bare .gcode (no zip container) must still yield sliced_for_model:
    Orca-family slicers put printer_model in the trailing CONFIG_BLOCK,
    past the head window. Without this the exact-match dispatch guard
    sees None and fail-opens on a declared Bambu slice.
    """
    from backend.app.services.archive import ThreeMFParser

    filler = b"G1 X1 Y1\n" * 20000  # push the trailer past the 64KB head window
    path = tmp_path / "job.gcode"
    path.write_bytes(b"; generated\n" + filler + f"; printer_model = {declared}\n".encode())

    assert ThreeMFParser(path).parse().get("sliced_for_model") == expected


@pytest.mark.asyncio
async def test_moonraker_completion_clears_the_queue_item():
    """Lifecycle close-out: a Moonraker completion payload (subtask name
    carrying its .gcode extension, Klipper-style) must match the printing
    queue item and move it out of "printing". Before the symmetric
    extension normalisation this comparison failed and the row stranded.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import backend.app.models  # noqa: F401 - populate Base.metadata
    from backend.app.core.database import Base
    from backend.app.main import (
        _active_prints,
        _expected_print_creators,
        _expected_print_registered_at,
        _expected_prints,
        _print_ams_mappings,
        _timelapse_baselines,
    )
    from backend.app.models.archive import PrintArchive
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.models.printer import Printer
    from backend.tests._fixtures.background_tasks import discarding_spawn_patch

    for d in (
        _active_prints,
        _expected_prints,
        _expected_print_creators,
        _expected_print_registered_at,
        _print_ams_mappings,
        _timelapse_baselines,
    ):
        d.clear()

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with session_maker() as db:
        printer = Printer(
            name="Snapmaker U1", serial_number="U1TEST", ip_address="192.168.4.102", access_code="", model="U1"
        )
        db.add(printer)
        await db.flush()
        archive = PrintArchive(
            printer_id=printer.id,
            filename="tolerance.gcode",
            file_path="archives/tolerance.gcode",
            file_size=10,
            status="printing",
        )
        db.add(archive)
        await db.flush()
        item = PrintQueueItem(printer_id=printer.id, archive_id=archive.id, status="printing")
        db.add(item)
        await db.commit()
        printer_id, item_id = printer.id, item.id

    with (
        patch("backend.app.main.async_session", session_maker),
        patch("backend.app.core.database.async_session", session_maker),
        patch("backend.app.main.ws_manager") as mock_ws,
        patch("backend.app.main.printer_manager") as mock_pm,
        patch("backend.app.main.mqtt_relay") as mock_relay,
        patch("backend.app.main.notification_service") as mock_notif,
        patch("backend.app.main.clear_3mf_cache"),
        patch("backend.app.services.usage_tracker.on_print_complete", new_callable=AsyncMock, return_value=None),
        discarding_spawn_patch(),
    ):
        mock_ws.send_print_complete = AsyncMock()
        mock_relay.on_print_complete = AsyncMock()
        mock_notif.on_print_complete = AsyncMock()
        mock_pm.get_printer = MagicMock(return_value=MagicMock(name="Snapmaker U1", serial_number="U1TEST"))
        mock_pm.get_current_print_user = MagicMock(return_value=None)
        mock_pm.clear_current_print_user = MagicMock()
        mock_pm.set_awaiting_plate_clear = MagicMock()

        from backend.app.main import on_print_complete

        # Exactly what MoonrakerClient._track_job fires.
        await on_print_complete(
            printer_id,
            {"status": "completed", "filename": "tolerance.gcode", "subtask_name": "tolerance.gcode"},
        )

    async with session_maker() as db:
        final = await db.get(PrintQueueItem, item_id)
    assert final.status == "completed"
    assert final.completed_at is not None
    await engine.dispose()


def test_klipper_exception_surfaces_as_hms_error_with_its_sentence():
    # The card must say WHY it paused ("detected dirty bed"), in the HMS
    # shape every consumer renders - not a bare PAUSE.
    client = FakeMoonrakerClient(ip_address="192.168.4.102", serial_number="U1TEST")
    status = _status("paused", "cube.gcode", 0.0, 0, 62)
    status["print_stats"]["exception"] = {"id": 532, "code": 1, "message": "detected dirty bed", "level": 2}
    status["print_stats"]["message"] = "detected dirty bed"
    client.payloads["status"] = status
    client._poll_once()
    errors = client.state.hms_errors
    assert len(errors) == 1
    assert errors[0].description == "detected dirty bed"
    assert errors[0].severity == 2
    assert errors[0].actions == []

    client.payloads["status"] = _status("printing", "cube.gcode", 0.1, 1, 62)
    client._poll_once()
    assert client.state.hms_errors == []


@pytest.mark.asyncio
async def test_timelapse_listing_never_attempts_ftp_for_u1():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from backend.app.main import _list_timelapse_videos

    printer = SimpleNamespace(id=1, model="U1", ip_address="192.168.4.102", access_code="")
    with patch("backend.app.services.bambu_ftp.list_files_async", new=AsyncMock(return_value=[])) as mock_list:
        videos, path = await _list_timelapse_videos(printer)
    assert (videos, path) == ([], None)
    mock_list.assert_not_awaited()


@pytest.mark.asyncio
async def test_notification_snapshot_uses_moonraker_capture_not_bambu_camera():
    import logging
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock, patch

    from backend.app.main import _capture_snapshot_for_notification
    from backend.app.services.printer_manager import printer_manager as real_manager

    client = FakeMoonrakerClient(ip_address="192.168.4.102", serial_number="U1TEST")
    client._poll_once()
    client.capture_snapshot = MagicMock(return_value=b"\xff\xd8frame\xff\xd9")
    real_manager._clients[77] = client
    real_manager._models[77] = "U1"

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))

    printer = SimpleNamespace(model="U1", external_camera_enabled=False, external_camera_url=None, camera_rotation=0)
    try:
        with (
            patch("backend.app.main.async_session", return_value=session),
            patch("backend.app.services.camera.capture_camera_frame_bytes", new_callable=AsyncMock) as bambu_capture,
        ):
            frame = await _capture_snapshot_for_notification(77, printer, logging.getLogger("test"))
    finally:
        real_manager._clients.pop(77, None)
        real_manager._models.pop(77, None)

    assert frame == b"\xff\xd8frame\xff\xd9"
    bambu_capture.assert_not_awaited()
