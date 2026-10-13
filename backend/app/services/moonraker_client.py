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

    def _post_json(self, path: str, payload: dict[str, Any]) -> Any:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(self.base_url + path, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
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
        self._track_job(new_state, filename)
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

    def _track_job(self, new_state: str, filename: str | None) -> None:
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
                data = {"filename": filename, "state": new_state}
                if self._first_poll:
                    if self.on_print_running_observed:
                        self.on_print_running_observed(data)
                elif self.on_print_start:
                    self.on_print_start(data)
            elif filename != self._active_job:
                old, self._active_job = self._active_job, filename
                if self.on_print_complete:
                    self.on_print_complete({"filename": old, "state": new_state})
                if self.on_print_start:
                    self.on_print_start({"filename": filename, "state": new_state})
        elif new_state in ("FINISH", "FAILED") and self._active_job is not None:
            old, self._active_job = self._active_job, None
            if self.on_print_complete:
                self.on_print_complete({"filename": old, "state": new_state})

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
