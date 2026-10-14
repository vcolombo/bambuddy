"""The Snapmaker wipe-tower sentinel: only the exact string "0" is dropped.

Snapmaker's Orca fork ships wipe_tower_filament as the string "0" in its
leaf presets; its own CLI segfaults loading a settings file carrying it
in that form (verified against 2.4.0). Omission is semantically the same
(0 = compiled-in default). Anything else — an int, a real pick, garbage —
must pass through byte-identical.
"""

import json

from backend.app.services.slicer_api import _drop_wipe_tower_sentinel


def test_string_zero_is_dropped():
    out = _drop_wipe_tower_sentinel('{"wipe_tower_filament": "0", "layer_height": "0.2"}')
    assert json.loads(out) == {"layer_height": "0.2"}


def test_int_zero_and_real_picks_pass_through():
    for raw in ('{"wipe_tower_filament": 0}', '{"wipe_tower_filament": "2"}', '{"wipe_tower_filament": 2}'):
        assert _drop_wipe_tower_sentinel(raw) == raw


def test_missing_key_and_garbage_pass_through():
    assert _drop_wipe_tower_sentinel('{"layer_height": "0.2"}') == '{"layer_height": "0.2"}'
    assert _drop_wipe_tower_sentinel("not json") == "not json"
