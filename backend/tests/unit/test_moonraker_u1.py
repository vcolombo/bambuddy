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

    def _post_json(self, path, payload):
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
