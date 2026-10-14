"""Snapmaker U1 dispatch: upload, guarded start, explicit plates, cancel.

End-to-end at the transport choke point: user-sliced gcode uploads then
starts by name, 3MF plates start by explicit plate number, and the busy +
extension guards refuse before anything touches the wire. The cloud-print
half of snapmakercloud is never called — there is no test for it because
there must be no code path to it.
"""

from backend.app.services.moonraker_client import MoonrakerClient
from backend.tests.unit.test_moonraker_u1 import FakeMoonrakerClient, _status


def _idle_client(**kwargs):
    client = FakeMoonrakerClient(ip_address="192.168.4.102", serial_number="U1TEST", **kwargs)
    client._poll_once()
    assert client.state.state == "IDLE"
    return client


def test_upload_stages_gcode_without_starting():
    client = _idle_client()
    assert client.upload_gcode(b"G28\n", "cube.gcode")
    assert client.posts == [("/server/files/upload", {"root": "gcodes"}, "cube.gcode", b"G28\n")]
    assert client.state.state == "IDLE"


def test_upload_rejects_non_gcode():
    client = _idle_client()
    assert not client.upload_gcode(b"data", "model.stl")
    assert not client.upload_gcode(b"data", "plate.3mf")
    assert client.posts == []


def test_start_print_posts_filename_when_idle():
    client = _idle_client()
    assert client.start_print("cube.gcode")
    assert client.posts == [("/printer/print/start", {"filename": "cube.gcode"})]


def test_start_print_refuses_busy_and_wrong_type():
    client = _idle_client()
    client.payloads["status"] = _status("printing", "other.gcode")
    client._poll_once()
    assert not client.start_print("cube.gcode")
    client.payloads["status"] = _status("paused", "other.gcode")
    client._poll_once()
    assert not client.start_print("cube.gcode")
    assert not client.start_print("model.stl")
    assert client.posts == []


def test_plate_start_carries_explicit_plate_and_refuses_defaults():
    client = _idle_client()
    assert client.start_plate_3mf("plates/project.3mf", 2)
    assert client.posts == [("/server/files/start_local_print", {"path": "plates/project.3mf", "print_plate": 2})]
    assert not client.start_plate_3mf("cube.gcode", 1)
    client.payloads["status"] = _status("printing", "other.gcode")
    client._poll_once()
    assert not client.start_plate_3mf("plates/project.3mf", 1)


def test_stop_posts_cancel():
    client = _idle_client()
    assert client.stop_print()
    assert client.posts == [("/printer/print/cancel", {})]


def test_manager_routes_u1_dispatch_to_moonraker_signature():
    from backend.app.services.printer_manager import PrinterManager

    manager = PrinterManager()
    client = FakeMoonrakerClient(ip_address="192.168.4.102", serial_number="U1TEST")
    client._poll_once()
    manager._clients[7] = client
    manager._models[7] = "U1"
    try:
        assert manager.start_print(7, "cube.gcode", plate_id=1, ams_mapping=[0], use_ams=True)
        assert client.posts == [("/printer/print/start", {"filename": "cube.gcode"})]
        assert manager.upload_print_file(7, b"G28\n", "cube.gcode")
        assert manager.stop_print(7)
    finally:
        del manager._clients[7]
        del manager._models[7]


def test_manager_refuses_upload_for_bambu_models():
    from backend.app.services.printer_manager import PrinterManager

    manager = PrinterManager()
    assert not manager.upload_print_file(999, b"G28\n", "cube.gcode")


def test_extension_matching_ignores_path_and_case():
    assert MoonrakerClient._extension("GCODES/Cube.GCODE") == ".gcode"
    assert MoonrakerClient._extension("noext") == ""


def _wait_for_post(client, timeout=2.0):
    import time

    deadline = time.monotonic() + timeout
    while not client.posts and time.monotonic() < deadline:
        time.sleep(0.01)
    return client.posts


def test_send_gcode_posts_script_to_klipper():
    client = _idle_client()
    assert client.send_gcode("G28")
    assert _wait_for_post(client) == [("/printer/gcode/script", {"script": "G28"})]


def test_send_gcode_passes_multiline_jog_verbatim():
    client = _idle_client()
    assert client.send_gcode("G91\nG1 X10.00 F6000\nG90")
    assert _wait_for_post(client) == [("/printer/gcode/script", {"script": "G91\nG1 X10.00 F6000\nG90"})]


def test_send_gcode_refused_when_disconnected():
    from backend.tests.unit.test_moonraker_u1 import FakeMoonrakerClient

    client = FakeMoonrakerClient(ip_address="192.168.4.102", serial_number="U1TEST")
    assert not client.state.connected
    assert client.send_gcode("G28") is False
    assert _wait_for_post(client, timeout=0.2) == []


def test_concurrent_motion_is_refused_not_queued():
    # A second click while a home is executing must be refused outright:
    # queueing it would run a stale jog minutes later, after the user has
    # long forgotten clicking it. Dispatch is synchronous, so the first
    # send occupies its caller until Klipper finishes.
    import threading

    client = _idle_client()
    release = threading.Event()
    entered = threading.Event()
    posted = []

    def blocking_post(path, payload, timeout=30):
        posted.append(payload["script"])
        entered.set()
        release.wait(timeout=5)
        return {"result": "ok"}

    client._post_json = blocking_post
    results = []
    home = threading.Thread(target=lambda: results.append(client.send_gcode("G28")))
    home.start()
    assert entered.wait(timeout=2)
    assert client.send_gcode("G91\nG1 X10 F6000\nG90") is False
    release.set()
    home.join(timeout=2)
    assert results == [True]
    assert posted == ["G28"]
    assert client.send_gcode("G91\nG1 X10 F6000\nG90") is True
    assert posted == ["G28", "G91\nG1 X10 F6000\nG90"]


def test_failed_motion_propagates_and_releases_the_flight_slot():
    # A POST that blows up (timeout, connection drop) surfaces as False —
    # not dispatched-and-forgotten — and the printer is not wedged out
    # of motion controls until reconnect.
    client = _idle_client()
    attempts = []

    def flaky_post(path, payload, timeout=30):
        attempts.append(payload["script"])
        if len(attempts) == 1:
            raise ConnectionError("dropped mid-home")
        return {"result": "ok"}

    client._post_json = flaky_post
    assert client.send_gcode("G28") is False
    assert client.send_gcode("M115") is True
    assert attempts == ["G28", "M115"]


def test_pause_resume_use_moonraker_print_endpoints():
    client = _idle_client()
    assert client.pause_print()
    assert client.resume_print()
    assert client.posts == [("/printer/print/pause", {}), ("/printer/print/resume", {})]


def test_temps_fans_and_light_map_to_klipper_commands():
    client = _idle_client()
    assert client.set_bed_temperature(60)
    assert client.set_nozzle_temperature(210, 2)
    assert client.set_fan_speed(1, 255)
    assert client.set_fan_speed(3, 128)
    assert client.set_chamber_light(True)
    assert client.set_chamber_light(False)
    scripts = [payload["script"] for _, payload in client.posts]
    assert scripts == [
        "M140 S60",
        "M104 T2 S210",
        "M106 S255",
        "SET_FAN_SPEED FAN=cavity_fan SPEED=0.502",
        "SET_LED LED=cavity_led WHITE=1.0",
        "SET_LED LED=cavity_led WHITE=0.0",
    ]
    assert all(path == "/printer/gcode/script" for path, _ in client.posts)


def test_unsupported_controls_deny_cleanly_without_emitting():
    client = _idle_client()
    assert client.set_nozzle_temperature(200, 4) is False  # only T0-T3 exist
    assert client.set_fan_speed(2, 255) is False  # no aux fan on a U1
    assert client.set_chamber_temperature(40) is False  # sensor + fan, no heater
    assert client.set_print_speed(3) is False  # Bambu speed-level vocabulary
    assert client.select_extruder(1) is False  # a toolchange, not a toggle
    assert client.set_airduct_mode("cooling") is False
    assert client.posts == []
