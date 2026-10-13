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
