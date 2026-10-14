"""Moonraker (Klipper) printer client for non-Bambu machines.

Read-only companion to ``bambu_mqtt.py``: polls a Moonraker instance over
HTTP on a background thread and maintains a ``PrinterState`` with the same
shape, so ``PrinterManager`` can treat both transports uniformly. The first
(and currently only) Moonraker machine is the Snapmaker U1.

Deliberately narrow: HTTP polling only (no websocket subscriptions yet —
those are needed for repeater ``camera/*`` calls and LAN access-code auth,
neither of which v1 uses), and no emit paths at all (deny-on-emit).
"""

import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from backend.app.services.bambu_mqtt import PrinterState

logger = logging.getLogger(__name__)

MOONRAKER_DEFAULT_PORT = 7125
POLL_INTERVAL_SECONDS = 5.0
STALE_TIMEOUT_SECONDS = 60.0

# Canonical domain tools T0–T3 to Klipper transport names.
TOOLS = {"T0": "extruder", "T1": "extruder1", "T2": "extruder2", "T3": "extruder3"}

# Klipper print_stats.state to Bambuddy state names.
PRINT_STATE_MAP = {
    "standby": "IDLE",
    "printing": "RUNNING",
    "paused": "PAUSE",
    "complete": "FINISH",
    "cancelled": "FAILED",
    "error": "FAILED",
}


class MoonrakerClient:
    """Polling Moonraker client exposing the Bambu client surface.

    Satisfies what ``PrinterManager`` needs: ``state``, ``connect()``,
    ``disconnect(timeout)`` and ``check_staleness()``, plus
    ``last_connect_error`` for the add-printer probe.
    """

    def __init__(
        self,
        ip_address: str,
        serial_number: str,
        access_code: str = "",
        model: str | None = None,
        on_state_change: Callable[[PrinterState], None] | None = None,
        on_print_start: Callable[[dict[str, Any]], None] | None = None,
        on_print_complete: Callable[[dict[str, Any]], None] | None = None,
        on_print_progress: Callable[[int], None] | None = None,
        on_layer_change: Callable[[int], None] | None = None,
        on_print_running_observed: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.ip_address = ip_address
        self.serial_number = serial_number
        self.access_code = access_code
        self.model = model
        self.on_state_change = on_state_change
        self.on_print_start = on_print_start
        self.on_print_complete = on_print_complete
        self.on_print_progress = on_print_progress
        self.on_layer_change = on_layer_change
        self.on_print_running_observed = on_print_running_observed
        self.state = PrinterState()
        self.last_connect_error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_poll_time = 0.0
        self._active_job: str | None = None
        self._first_poll = True
        self._monitor_started_at = 0.0
        self._monitor_attempt_at = 0.0
        self._gcode_in_flight = False
        self._gcode_lock = threading.Lock()
        self._prev_progress: int | None = None
        self._prev_layer: int | None = None

    @property
    def base_url(self) -> str:
        host = self.ip_address
        if ":" not in host:
            host = f"{host}:{MOONRAKER_DEFAULT_PORT}"
        return f"http://{host}"

    def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=15) as resp:
            return json.load(resp)["result"]

    def _post_json(self, path: str, payload: dict[str, Any], timeout: float = 30) -> Any:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(self.base_url + path, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp).get("result")

    def _post_multipart(self, path: str, fields: dict[str, str], filename: str, data: bytes) -> Any:
        boundary = "bambuddy-upload-boundary"
        body = b""
        for key, value in fields.items():
            body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode()
        body += (
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                f'filename="{urllib.parse.quote(filename)}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode()
            + data
            + f"\r\n--{boundary}--\r\n".encode()
        )
        req = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.load(resp)

    def _get_bytes(self, path: str) -> bytes:
        with urllib.request.urlopen(self.base_url + path, timeout=15) as resp:
            return resp.read()

    def _ws_call(self, method: str, params: dict[str, Any]) -> Any:
        """One JSON-RPC call over a short-lived Moonraker websocket.

        The Snapmaker repeater endpoints (camera/*, system/*) accept every
        transport except HTTP, so they ride this instead of _post_json.
        Notifications interleaved before the response are skipped.
        """
        from websockets.sync.client import connect as ws_connect

        host = self.ip_address if ":" in self.ip_address else f"{self.ip_address}:{MOONRAKER_DEFAULT_PORT}"
        with ws_connect(f"ws://{host}/websocket", open_timeout=10) as ws:
            ws.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "server.connection.identify",
                        "params": {
                            "client_name": "bambuddy",
                            "version": "1.0",
                            "type": "web",
                            "url": "http://localhost",
                        },
                        "id": 1,
                    }
                )
            )
            json.loads(ws.recv(timeout=10))
            ws.send(json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 2}))
            for _ in range(10):
                msg = json.loads(ws.recv(timeout=15))
                if msg.get("id") != 2:
                    continue
                if "error" in msg:
                    raise RuntimeError(msg["error"].get("message", "Moonraker websocket error"))
                return msg.get("result")
        raise RuntimeError(f"No response to {method}")

    # Camera monitor lifecycle (hardware-verified community findings): the
    # firmware stops the monitor ~6 min after the last start_monitor, first
    # frame lands ~1.1s after a cold start, and start_monitor must not be
    # hammered (5s cooldown).
    MONITOR_RENEW_SECONDS = 240.0
    MONITOR_COOLDOWN_SECONDS = 5.0
    # A running monitor writes ~1 frame/s, so an mtime that does not advance
    # within FRAME_ADVANCE_TIMEOUT means it is not running; after a (re)start
    # the first frame lands ~1.1s in, so START_ADVANCE_TIMEOUT is generous.
    FRAME_ADVANCE_TIMEOUT = 2.5
    START_ADVANCE_TIMEOUT = 5.0
    ADVANCE_POLL_SECONDS = 0.25

    def _monitor_mtime(self) -> float | None:
        """Printer-side mtime of monitor.jpg, or None when the file is absent.

        Transport errors propagate: "could not look" must never read as
        "no file", or a failed lookup would pass for a fresh start.
        """
        entries = self._get("/server/files/list", {"root": "camera"})
        for entry in entries or []:
            if entry.get("path") == "monitor.jpg":
                return float(entry.get("modified") or 0)
        return None

    def _wait_for_advance(self, baseline: float | None, timeout: float) -> float | None:
        """Poll until monitor.jpg's mtime exceeds ``baseline``; None on timeout.

        Compares the printer's own timestamps with each other, so host/printer
        clock skew cannot make a dead camera look alive. A file appearing
        where there was none counts as an advance.
        """
        deadline = time.monotonic() + timeout
        while True:
            mtime = self._monitor_mtime()
            if mtime is not None and (baseline is None or mtime > baseline):
                return mtime
            if time.monotonic() >= deadline:
                return None
            time.sleep(self.ADVANCE_POLL_SECONDS)

    @staticmethod
    def _is_complete_jpeg(frame: bytes) -> bool:
        """SOI at the head and EOI at the tail: rejects torn or foreign bytes."""
        return frame.startswith(b"\xff\xd8") and frame.rstrip(b"\x00").endswith(b"\xff\xd9")

    def _ensure_monitor(self) -> bool:
        """Start (or renew) the camera monitor. Returns True if (re)started."""
        now = time.time()
        if now - self._monitor_attempt_at < self.MONITOR_COOLDOWN_SECONDS:
            return False
        self._monitor_attempt_at = now
        result = self._ws_call(
            "camera.start_monitor",
            {"req_id": int(now * 1000) % 2_147_483_647, "domain": "lan", "interval": 0},
        )
        if not (isinstance(result, dict) and result.get("state") == "success"):
            raise RuntimeError(f"camera.start_monitor refused: {result!r}")
        self._monitor_started_at = now
        return True

    FETCH_ATTEMPTS = 3
    FETCH_RETRY_SECONDS = 0.15

    def _fetch_complete_frame(self) -> bytes | None:
        """Fetch monitor.jpg, retrying a read that lands mid-rewrite.

        The firmware rewrites the file ~1/s, so a read can come back short
        (IncompleteRead) or truncated — seen live on the first deployed
        stream run. The next write is complete, so a quick retry succeeds,
        and it can only return a frame at least as new as the advance
        already proven. Anything that never arrives complete is None, never
        a torn image.
        """
        for attempt in range(self.FETCH_ATTEMPTS):
            try:
                frame = self._get_bytes("/server/files/camera/monitor.jpg")
            except Exception as e:
                logger.debug(f"[{self.serial_number}] camera frame read failed (attempt {attempt + 1}): {e}")
            else:
                if self._is_complete_jpeg(frame):
                    return frame
            if attempt + 1 < self.FETCH_ATTEMPTS:
                time.sleep(self.FETCH_RETRY_SECONDS)
        return None

    def capture_snapshot(self) -> bytes | None:
        """Capture one fresh JPEG still via the repeater monitor, or None.

        "Fresh" is proven, not assumed: monitor.jpg's printer-side mtime must
        advance while we watch. If it does not (monitor stopped, or a
        start_monitor that succeeded but never produced a frame), the monitor
        is restarted and advancement is required again — otherwise None. The
        caller may stamp the result as current (the frame buffer feeds plate
        checks), so a stale file must never come back as a success.

        Keeps the monitor warm across calls (renewed before the firmware's
        6-minute auto-stop) and never stops it explicitly: other viewers may
        be watching, and the firmware reaps an idle monitor itself.
        """
        try:
            baseline = self._monitor_mtime()
            advanced: float | None = None
            if time.time() - self._monitor_started_at <= self.MONITOR_RENEW_SECONDS:
                advanced = self._wait_for_advance(baseline, self.FRAME_ADVANCE_TIMEOUT)
            if advanced is None:
                self._ensure_monitor()
                advanced = self._wait_for_advance(baseline, self.START_ADVANCE_TIMEOUT)
            if advanced is None:
                logger.warning(f"[{self.serial_number}] camera monitor produced no new frame; not serving stale image")
                return None
            frame = self._fetch_complete_frame()
            if frame is None:
                logger.warning(f"[{self.serial_number}] camera frame never arrived complete; discarding")
                return None
            return frame
        except Exception as e:
            logger.warning(f"[{self.serial_number}] camera snapshot failed: {e}")
            return None

    # Motion dispatch is single-flight: homing and moves execute
    # synchronously inside Klipper's script processing, so the POST can
    # take as long as the motion itself. One in-flight motion at a time,
    # on its own thread — a second click is refused rather than queued,
    # so no stale jog can execute minutes later behind a failed or
    # disconnected home. There is deliberately no pending backlog.
    def send_gcode(self, gcode: str) -> bool:
        """Dispatch G-code script(s) to Klipper. Mirrors the Bambu
        contract (True = dispatched, not completed), plus the
        single-flight rule above: False while a previous motion is
        still executing.
        """
        if not self.state.connected:
            logger.warning("[%s] Cannot send G-code: not connected", self.serial_number)
            return False
        with self._gcode_lock:
            if self._gcode_in_flight:
                logger.warning("[%s] G-code refused: motion already in flight", self.serial_number)
                return False
            self._gcode_in_flight = True

        def _send() -> None:
            try:
                self._post_json("/printer/gcode/script", {"script": gcode}, timeout=180)
            except Exception as e:
                logger.warning(f"[{self.serial_number}] G-code failed ({gcode[:50]!r}): {e}")
            finally:
                with self._gcode_lock:
                    self._gcode_in_flight = False

        threading.Thread(target=_send, daemon=True).start()
        return True

    def _poll_once(self) -> bool:
        """Fetch status and fold it into ``self.state``. Returns reachability."""
        try:
            info = self._get("/server/info")
            query = self._get(
                "/printer/objects/query",
                {
                    "print_stats": "",
                    "display_status": "",
                    "virtual_sdcard": "",
                    "toolhead": "",
                    "heater_bed": "",
                    **dict.fromkeys(TOOLS.values(), ""),
                },
            )
        except Exception as e:
            logger.warning(f"[{self.serial_number}] Moonraker poll failed: {e}")
            return False
        self._last_poll_time = time.time()
        before = self._snapshot()
        self._apply(info, query["status"])
        if self._snapshot() != before and self.on_state_change:
            self.on_state_change(self.state)
        return True

    def _snapshot(self) -> tuple:
        """Comparable view of everything a poll can change."""
        s = self.state
        return (
            s.connected,
            s.state,
            s.current_print,
            s.progress,
            s.layer_num,
            s.total_layers,
            tuple(sorted((s.temperatures or {}).items())),
            s.active_extruder,
            s.firmware_version,
        )

    def _apply(self, info: dict[str, Any], s: dict[str, Any]) -> None:
        state = self.state
        klippy_ready = info.get("klippy_state") == "ready" and info.get("klippy_connected", False)
        if not state.connected and klippy_ready:
            try:
                printer_info = self._get("/printer/info")
                state.firmware_version = printer_info.get("software_version", "")
            except Exception as e:
                logger.warning(f"[{self.serial_number}] Moonraker printer/info failed: {e}")
        state.connected = klippy_ready

        stats = s.get("print_stats", {})
        klippy_state = stats.get("state", "standby")
        new_state = PRINT_STATE_MAP.get(klippy_state, "IDLE")
        filename = stats.get("filename") or None
        state.state = new_state
        state.current_print = filename
        state.gcode_file = filename
        state.subtask_name = filename
        self._track_job(new_state, klippy_state, filename)
        self._first_poll = False

        progress = round(float(s.get("display_status", {}).get("progress") or 0.0) * 100, 1)
        state.progress = progress
        progress_int = int(progress)
        if self._prev_progress != progress_int:
            self._prev_progress = progress_int
            if self.on_print_progress:
                self.on_print_progress(progress_int)

        info_layers = stats.get("info", {}) or {}
        layer = int(info_layers.get("current_layer") or 0)
        state.layer_num = layer
        state.total_layers = int(info_layers.get("total_layer") or 0)
        if self._prev_layer != layer:
            self._prev_layer = layer
            if layer and self.on_layer_change:
                self.on_layer_change(layer)

        active_tool = self._active_tool_index(s.get("toolhead", {}).get("extruder", "extruder"))
        temps: dict[str, float] = {}
        for tool, klippy_name in TOOLS.items():
            extruder = s.get(klippy_name, {})
            if tool == active_tool:
                temps["nozzle"] = extruder.get("temperature", 0.0)
                temps["nozzle_target"] = extruder.get("target", 0.0)
        bed = s.get("heater_bed", {})
        temps["bed"] = bed.get("temperature", 0.0)
        temps["bed_target"] = bed.get("target", 0.0)
        state.temperatures = temps
        state.active_extruder = int(active_tool[1:])

    def _active_tool_index(self, klippy_name: str) -> str:
        for tool, name in TOOLS.items():
            if name == klippy_name:
                return tool
        return "T0"

    # Klipper terminal state -> the completion consumer's status vocabulary
    # (main.on_print_complete reads data["status"], defaulting a missing one
    # to "completed" — a cancelled U1 print must never ride that default).
    TERMINAL_STATUS_MAP = {"complete": "completed", "cancelled": "cancelled", "error": "failed"}

    def _track_job(self, new_state: str, klippy_state: str, filename: str | None) -> None:
        """Fire print start/complete around job identity, not raw state flips.

        A pause is not a new job, so PAUSE→RUNNING must not re-fire start;
        a cancel lands from PAUSE as well as RUNNING, so completion keys
        off the tracked job, not the previous state. A RUNNING job seen on
        the very first poll was adopted mid-print (the #1304 guard): it
        reports as observed, never as started.
        """
        if new_state == "RUNNING" and filename is not None:
            if self._active_job is None:
                self._active_job = filename
                data = {"filename": filename, "subtask_name": filename}
                if self._first_poll:
                    if self.on_print_running_observed:
                        self.on_print_running_observed(data)
                elif self.on_print_start:
                    self.on_print_start(data)
            elif filename != self._active_job:
                old, self._active_job = self._active_job, filename
                if self.on_print_complete:
                    # Superseded mid-print with no terminal state observed:
                    # Bambu's else-branch vocabulary for that is "aborted".
                    self.on_print_complete({"status": "aborted", "filename": old, "subtask_name": old})
                if self.on_print_start:
                    self.on_print_start({"filename": filename, "subtask_name": filename})
        elif new_state in ("FINISH", "FAILED") and self._active_job is not None:
            old, self._active_job = self._active_job, None
            if self.on_print_complete:
                status = self.TERMINAL_STATUS_MAP.get(klippy_state, "completed" if new_state == "FINISH" else "failed")
                self.on_print_complete({"status": status, "filename": old, "subtask_name": old})

    def _loop(self) -> None:
        while not self._stop.wait(POLL_INTERVAL_SECONDS):
            self._poll_once()

    def connect(self) -> None:
        """Poll immediately (so probes see the state), then start the loop."""
        reachable = self._poll_once()
        if not reachable:
            self.last_connect_error = "unreachable"
            logger.warning(f"[{self.serial_number}] Moonraker unreachable at {self.base_url}")
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def disconnect(self, timeout: float = 0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout if timeout > 0 else 2.0)
        self._thread = None
        self.state.connected = False

    def is_stale(self) -> bool:
        return time.time() - self._last_poll_time > STALE_TIMEOUT_SECONDS

    def check_staleness(self) -> bool:
        """Mirror the Bambu surface: drop to disconnected when polls stop landing."""
        if self.state.connected and self.is_stale():
            logger.warning(f"[{self.serial_number}] Moonraker stale, marking disconnected")
            self.state.connected = False
            if self.on_state_change:
                self.on_state_change(self.state)
        return self.state.connected

    def request_status_update(self) -> bool:
        """Polling already refreshes every interval; nothing to request."""
        return False

    # --- Inert Bambu-only surface (deny-by-default, never AttributeError) ---
    # Manager/routes reach for these unconditionally; a Moonraker machine
    # answers "no" instead of crashing the status surface.

    logging_enabled = False  # MQTT log capture is a Bambu concept

    @property
    def _last_message_time(self) -> float:
        # Routes probe this as an ack signal; the last successful poll is
        # the Moonraker equivalent.
        return self._last_poll_time

    def enable_logging(self, enabled: bool = True) -> None:
        pass

    def get_logs(self) -> list:
        return []

    def clear_logs(self) -> None:
        pass

    def send_drying_command(
        self, ams_id: int, temp: int, duration: int, mode: int = 1, filament: str = "", rotate_tray: bool = False
    ) -> bool:
        return False  # AMS drying does not exist on a toolchanger

    def execute_hms_action(self, print_error: str, action: str, job_id: str | None = None) -> bool:
        return False  # HMS is a Bambu fault domain

    def mark_power_off(self) -> bool:
        """Presume power loss (smart plug off). The next successful poll
        undoes it if the printer is actually still there."""
        if not self.state.connected:
            return False
        self.state.connected = False
        return True

    # --- Dispatch (the authorized emit surface for this track) ---

    # Bambuddy states that refuse a new job. Mirrors the Bambu choke point:
    # only a printer doing nothing may be handed work. cancel lands from
    # PAUSE as well as RUNNING, so both refuse.
    BUSY_STATES = frozenset({"RUNNING", "PAUSE"})

    # What the local upload path accepts (Moonraker VALID_GCODE_EXTS).
    GCODE_EXTENSIONS = frozenset({".gcode", ".g", ".gco", ".ufp"})

    def _refuse_if_busy(self, action: str) -> bool:
        if self.state.state in self.BUSY_STATES:
            logger.warning(
                "[%s] %s refused: printer busy (state=%s)",
                self.serial_number,
                action,
                self.state.state,
            )
            return True
        return False

    @staticmethod
    def _extension(filename: str) -> str:
        name = filename.rsplit("/", 1)[-1]
        dot = name.rfind(".")
        return name[dot:].lower() if dot >= 0 else ""

    def upload_gcode(self, data: bytes, filename: str) -> bool:
        """Upload user-sliced gcode to the printer. Never starts the print."""
        if self._extension(filename) not in self.GCODE_EXTENSIONS:
            logger.warning("[%s] upload refused: %s is not printable gcode", self.serial_number, filename)
            return False
        try:
            result = self._post_multipart("/server/files/upload", {"root": "gcodes"}, filename.rsplit("/", 1)[-1], data)
        except Exception as e:
            logger.warning(f"[{self.serial_number}] upload failed for {filename}: {e}")
            return False
        return result.get("action") == "create_file"

    def start_print(self, filename: str, plate_id: int = 1) -> bool:
        """Start an already-uploaded gcode file. plate_id is accepted for
        manager-signature compatibility and ignored: gcode has no plates."""
        if self._extension(filename) not in self.GCODE_EXTENSIONS:
            logger.warning("[%s] start refused: %s is not printable gcode", self.serial_number, filename)
            return False
        if self._refuse_if_busy(f"start {filename}"):
            return False
        try:
            self._post_json("/printer/print/start", {"filename": filename})
        except Exception as e:
            logger.warning(f"[{self.serial_number}] start failed for {filename}: {e}")
            return False
        return True

    def start_plate_3mf(self, server_path: str, plate: int) -> bool:
        """Start one plate of a 3MF already on the printer. The plate is
        required — no silent default (a wrong plate wastes filament).
        Conversion to per-plate gcode happens on-device; the cloud-print
        half of snapmakercloud is never called."""
        if self._extension(server_path) != ".3mf":
            logger.warning("[%s] plate start refused: %s is not a 3MF", self.serial_number, server_path)
            return False
        if self._refuse_if_busy(f"plate {plate} of {server_path}"):
            return False
        try:
            self._post_json(
                "/server/files/start_local_print",
                {"path": server_path, "print_plate": plate},
            )
        except Exception as e:
            logger.warning(f"[{self.serial_number}] plate start failed for {server_path}: {e}")
            return False
        return True

    def stop_print(self) -> bool:
        """Cancel the current print."""
        try:
            self._post_json("/printer/print/cancel", {})
        except Exception as e:
            logger.warning(f"[{self.serial_number}] cancel failed: {e}")
            return False
        return True
