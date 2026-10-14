"""Snapmaker U1 camera stills: freshness must be proven, never assumed.

The frame buffer these stills feed is stamped "current" and consumed as
plate-check evidence, so a dead camera must never come back as a success:
a stale monitor.jpg, a start_monitor that succeeds but never produces a
frame, and a torn write all have to end in None.
"""

import time

from backend.tests.unit.test_moonraker_u1 import FakeMoonrakerClient

JPEG = b"\xff\xd8" + b"frame-bytes" + b"\xff\xd9"

JPEG_B = b"\xff\xd8" + b"second-frame" + b"\xff\xd9"
JPEG_C = b"\xff\xd8" + b"cached-frame" + b"\xff\xd9"


class IncompleteReadStandIn(Exception):
    """http.client.IncompleteRead is an Exception subclass; any one will do."""


class CameraFake(FakeMoonrakerClient):
    """FakeMoonrakerClient with a scripted monitor.jpg on the printer.

    ``mtimes`` is consumed one value per file-list call (the last value
    repeats), which is how a frozen file is expressed.
    """

    def __init__(self, *args, mtimes, frame=JPEG, start_result=None, advance_on_start=None, webcam="", **kwargs):
        super().__init__(*args, **kwargs)
        self.FRAME_ADVANCE_TIMEOUT = 0.05
        self.START_ADVANCE_TIMEOUT = 0.05
        self.ADVANCE_POLL_SECONDS = 0.005
        self.MONITOR_COOLDOWN_SECONDS = 0.0
        self.FETCH_RETRY_SECONDS = 0.005
        self._mtimes = list(mtimes)
        # One outcome per read (last repeats): bytes, or an Exception to raise.
        self.WEBCAM_PROOF_GAP_SECONDS = 0.005
        self._frames = list(frame) if isinstance(frame, list) else [frame]
        self._start_result = start_result if start_result is not None else {"state": "success"}
        # A real start_monitor makes the firmware write a new frame; model
        # that only when asked, so a "success that never produces a frame"
        # stays expressible.
        self._advance_on_start = advance_on_start
        self.start_calls = 0
        self._webcam = webcam

    def _get(self, path, params=None):
        if path == "/server/webcams/list":
            cams = [{"name": "case", "enabled": True, "snapshot_url": self._webcam}] if self._webcam else []
            return {"webcams": cams}
        if path == "/server/files/list":
            mtime = self._mtimes.pop(0) if len(self._mtimes) > 1 else self._mtimes[0]
            return [] if mtime is None else [{"path": "monitor.jpg", "modified": mtime}]
        return super()._get(path, params)

    def _get_bytes(self, path):
        outcome = self._frames.pop(0) if len(self._frames) > 1 else self._frames[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def _ws_call(self, method, params):
        assert method == "camera.start_monitor"
        self.start_calls += 1
        if self._advance_on_start is not None:
            self._mtimes = [self._advance_on_start]
        return self._start_result


def _client(**kwargs):
    return CameraFake(ip_address="192.168.4.102", serial_number="U1TEST", **kwargs)


def test_advancing_monitor_returns_the_frame():
    client = _client(mtimes=[100.0, 101.0])
    client._monitor_started_at = time.time()  # warm: no restart needed
    assert client.capture_snapshot() == JPEG
    assert client.start_calls == 0


def test_started_monitor_that_never_advances_returns_none():
    # The reviewed failure: start_monitor says success, the file stays frozen.
    client = _client(mtimes=[100.0])
    assert client.capture_snapshot() is None
    assert client.start_calls == 1


def test_stopped_monitor_is_restarted_and_serves_once_it_advances():
    client = _client(mtimes=[100.0], advance_on_start=105.0)
    client._monitor_started_at = time.time()
    assert client.capture_snapshot() == JPEG
    assert client.start_calls == 1


def test_missing_file_then_first_frame_counts_as_fresh():
    client = _client(mtimes=[None, 50.0])
    assert client.capture_snapshot() == JPEG


def test_refused_start_never_serves_the_stale_file():
    client = _client(mtimes=[100.0], start_result={"state": "failed"})
    assert client.capture_snapshot() is None


def test_torn_or_foreign_bytes_are_discarded():
    for bad in (b"", b"<html>502</html>", b"\xff\xd8 truncated no eoi"):
        client = _client(mtimes=[100.0, 101.0], frame=bad)
        client._monitor_started_at = time.time()
        assert client.capture_snapshot() is None, bad


def test_lookup_failure_is_not_mistaken_for_a_missing_file():
    client = _client(mtimes=[100.0])

    def boom(path, params=None):
        raise ConnectionError("unreachable")

    client._get = boom
    assert client.capture_snapshot() is None
    assert client.start_calls == 0


def test_due_renewal_restarts_even_while_the_monitor_advances():
    client = _client(mtimes=[100.0, 101.0, 102.0])
    client._monitor_started_at = time.time() - (client.MONITOR_RENEW_SECONDS + 1)
    assert client.capture_snapshot() == JPEG
    assert client.start_calls == 1


def test_read_landing_mid_rewrite_is_retried_not_dropped():
    # Seen live on the first deployed stream run: monitor.jpg is rewritten
    # ~1/s, so a read can come back short. The next read is complete.
    torn = IncompleteReadStandIn("147602 bytes read, 495 more expected")
    client = _client(mtimes=[100.0, 101.0], frame=[torn, JPEG[:6], JPEG])
    client._monitor_started_at = time.time()
    assert client.capture_snapshot() == JPEG


def test_reads_that_never_arrive_complete_give_up_as_none():
    torn = IncompleteReadStandIn("always torn")
    client = _client(mtimes=[100.0, 101.0], frame=torn)
    client._monitor_started_at = time.time()
    assert client.capture_snapshot() is None


def test_webcam_service_is_preferred_over_the_monitor():
    # Extended firmware registers a camerastreamer webcam: real frame
    # rates, no start_monitor dance at all. The served frame is the
    # second fetch — the advance observed inside the capture.
    client = _client(mtimes=[100.0], frame=[JPEG, JPEG_B], webcam="/webcam/snapshot.jpg")
    assert client.capture_snapshot() == JPEG_B
    assert client.start_calls == 0


def test_webcam_snapshot_url_resolves_against_printer_host():
    client = _client(mtimes=[100.0], webcam="/webcam/snapshot.jpg")
    assert client._resolve_webcam_snapshot_url() == "http://192.168.4.102/webcam/snapshot.jpg"


def test_frozen_webcam_first_capture_returns_none():
    # A dead service serving one cached frame forever: "different from
    # nothing" is not proof of life. Fall through to the monitor, which
    # is frozen here, so the only honest answer is None.
    client = _client(mtimes=[100.0], webcam="/webcam/snapshot.jpg")
    assert client.capture_snapshot() is None
    assert client.start_calls == 1  # the fallback at least tried the monitor


def test_frozen_webcam_after_idle_gap_returns_none():
    # The cached frame differs from what was served before the gap, so a
    # naive hash comparison would call it fresh. Proof must be observed
    # during this capture once the proof window has expired.
    client = _client(mtimes=[100.0], frame=[JPEG, JPEG_B, JPEG_C], webcam="/webcam/snapshot.jpg")
    assert client.capture_snapshot() == JPEG_B
    client._webcam_proven_at -= client.WEBCAM_PROOF_WINDOW_SECONDS + 60
    assert client.capture_snapshot() is None
    assert client.start_calls == 1


def test_webcam_failure_falls_back_to_the_monitor():
    client = _client(
        mtimes=[100.0, 101.0],
        frame=[ConnectionError("webcam down"), JPEG],
        webcam="/webcam/snapshot.jpg",
    )
    client._monitor_started_at = time.time()
    assert client.capture_snapshot() == JPEG
    assert client.start_calls == 0
