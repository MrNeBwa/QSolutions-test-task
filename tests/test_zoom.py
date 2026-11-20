from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

import apply_zoom
import qscript


def test_ramp_frame_calculation():
    assert apply_zoom.compute_ramp_frames(0.5, 25) == 12
    assert apply_zoom.compute_ramp_frames(0.4, 25) == 10
    assert apply_zoom.compute_ramp_frames(0.0, 25) == 0
    assert apply_zoom.compute_ramp_frames(0.01, 25) == 1


def test_coordinate_mapping_and_clamping():
    # 1:1 mapping when record_video_size == viewport
    assert apply_zoom.map_coords_to_video(640, 360, 1280, 720, 1280, 720) == (640.0, 360.0)
    # Clamping out-of-range viewport coords
    assert apply_zoom.map_coords_to_video(-10, 999, 1280, 720, 1280, 720) == (0.0, 720.0)

    # Exact centering for interior point at 2x zoom (crop 640x360)
    x, y = apply_zoom.clamp_crop_origin(500, 300, 2.0, 1280, 720)
    assert (x, y) == (pytest.approx(180.0), pytest.approx(120.0))

    # Corner clamping keeps crop inside frame bounds
    assert apply_zoom.clamp_crop_origin(20, 10, 2.0, 1280, 720) == (0.0, 0.0)
    assert apply_zoom.clamp_crop_origin(1260, 710, 2.0, 1280, 720) == (
        pytest.approx(640.0),
        pytest.approx(360.0),
    )


def test_single_pair_smooth_easing_and_zoompan_filter():
    sidecar = apply_zoom.parse_sidecar_data(
        {
            "viewport": {"width": 1280, "height": 720},
            "fps": 25,
            "zoom_factor": 2.0,
            "ramp_sec": 0.4,  # 10 frames
            "events": [
                {"t": 2.0, "x": 640, "y": 360, "type": "in"},
                {"t": 6.0, "x": 640, "y": 360, "type": "out"},
            ],
        }
    )
    segs = apply_zoom.build_zoom_segments(sidecar)
    assert len(segs) == 1
    assert (segs[0].in_frame, segs[0].hold_start_frame, segs[0].out_frame, segs[0].end_frame) == (
        50,
        60,
        150,
        160,
    )

    # Before zoom -> 1.0
    assert apply_zoom.evaluate_zoom_at_frame(segs, 49, 1280, 720)[0] == pytest.approx(1.0)
    # Cosine ease-in at 20% progress (non-linear)
    z_q1 = apply_zoom.evaluate_zoom_at_frame(segs, 52, 1280, 720)[0]
    assert z_q1 == pytest.approx(1.0 + 0.5 * (1.0 - math.cos(0.2 * math.pi)))
    # Hold at zoom_factor
    assert apply_zoom.evaluate_zoom_at_frame(segs, 100, 1280, 720) == (
        pytest.approx(2.0),
        pytest.approx(320.0),
        pytest.approx(180.0),
    )
    # After ramp-out -> 1.0
    assert apply_zoom.evaluate_zoom_at_frame(segs, 160, 1280, 720)[0] == pytest.approx(1.0)

    flt = apply_zoom.build_zoompan_filter(segs, 1280, 720, 25)
    assert "zoompan=" in flt
    assert "cos(PI*(on-50)/10)" in flt
    assert "between(on,60,149)" in flt
    assert "clip(640-iw/(2*zoom),0,iw-iw/zoom)" in flt
    assert ":d=1:s=1280x720:fps=25" in flt


def test_multiple_non_overlapping_pairs():
    sidecar = apply_zoom.parse_sidecar_data(
        {
            "viewport": {"width": 1280, "height": 720},
            "fps": 25,
            "zoom_factor": 2.0,
            "ramp_sec": 0.4,
            "events": [
                {"t": 1.0, "x": 100, "y": 80, "type": "in"},
                {"t": 3.0, "x": 100, "y": 80, "type": "out"},
                {"t": 6.0, "x": 1200, "y": 680, "type": "in"},
                {"t": 8.0, "x": 1200, "y": 680, "type": "out"},
            ],
        }
    )
    segs = apply_zoom.build_zoom_segments(sidecar)
    assert len(segs) == 2
    assert segs[0].end_frame <= segs[1].in_frame
    assert apply_zoom.evaluate_zoom_at_frame(segs, 50, 1280, 720) == (pytest.approx(2.0), 0.0, 0.0)
    assert apply_zoom.evaluate_zoom_at_frame(segs, 110, 1280, 720) == (1.0, 0.0, 0.0)
    assert apply_zoom.evaluate_zoom_at_frame(segs, 175, 1280, 720) == (
        pytest.approx(2.0),
        pytest.approx(640.0),
        pytest.approx(360.0),
    )


def test_open_ended_zoom_holds_to_end():
    sidecar = apply_zoom.parse_sidecar_data(
        {
            "viewport": {"width": 1280, "height": 720},
            "fps": 25,
            "zoom_factor": 2.0,
            "ramp_sec": 0.5,
            "events": [{"t": 2.0, "x": 500, "y": 300, "type": "in"}],
        }
    )
    segs = apply_zoom.build_zoom_segments(sidecar)
    assert len(segs) == 1
    assert segs[0].out_frame is None
    assert apply_zoom.evaluate_zoom_at_frame(segs, 5000, 1280, 720)[0] == pytest.approx(2.0)
    assert "gte(on,62)" in apply_zoom.build_zoompan_filter(segs, 1280, 720, 25)


def test_old_sidecars_without_type():
    # Legacy alternating toggle events without "type"
    sc_toggle = apply_zoom.parse_sidecar_data(
        {
            "events": [
                {"t": 2.0, "x": 400, "y": 300},
                {"t": 5.0, "x": 400, "y": 300},
                {"t": 8.0, "x": 900, "y": 500},
            ]
        }
    )
    assert [e.type for e in sc_toggle.events] == ["in", "out", "in"]

    # Legacy fixed hold_sec without "type"
    sc_hold = apply_zoom.parse_sidecar_data(
        {
            "ramp_sec": 0.5,
            "hold_sec": 2.0,
            "events": [{"t": 3.0, "x": 640, "y": 360}],
        }
    )
    assert [(e.type, e.t) for e in sc_hold.events] == [("in", 3.0), ("out", 5.5)]


def test_no_events_copies_unchanged(tmp_path: Path):
    src = tmp_path / "clip.webm"
    src.write_bytes(b"RAW_WEBM_DATA")
    (tmp_path / "clip.zoom.json").write_text(json.dumps({"events": []}), encoding="utf-8")

    res = apply_zoom.process_video(src, ffmpeg_bin="/nonexistent/ffmpeg")
    assert res["action"] == "copy"
    assert Path(res["output"]).read_bytes() == b"RAW_WEBM_DATA"


def test_dry_run_prints_ffmpeg_command_without_encoding(tmp_path: Path, capsys):
    vid = tmp_path / "ACME.webm"
    (tmp_path / "ACME.zoom.json").write_text(
        json.dumps(
            {
                "viewport": {"width": 1280, "height": 720},
                "fps": 25,
                "zoom_factor": 2.0,
                "ramp_sec": 0.5,
                "events": [
                    {"t": 3.0, "x": 640, "y": 360, "type": "in"},
                    {"t": 8.0, "x": 640, "y": 360, "type": "out"},
                ],
            }
        ),
        encoding="utf-8",
    )

    rc = apply_zoom.main(["--video", str(vid), "--dry-run", "--ffmpeg", "/custom/ffmpeg"])
    assert rc == 0
    stdout = capsys.readouterr().out.strip()
    assert stdout.startswith("/custom/ffmpeg ")
    assert "zoompan=" in stdout
    assert not (tmp_path / "ACME_zoomed.webm").exists()


def test_zoom_recorder_writes_sidecar_only_when_events_exist(tmp_path: Path):
    vid = tmp_path / "ACME.webm"
    vid.write_bytes(b"webm")
    sidecar = tmp_path / "ACME.zoom.json"

    # No events -> no sidecar
    empty_rec = qscript.ZoomRecorder(clock=lambda: 0.0)
    assert empty_rec.write_sidecar(str(vid)) is None
    assert not sidecar.exists()

    # Toggle in / out -> writes sidecar
    ticks = iter([10.0, 13.0, 18.0])
    rec = qscript.ZoomRecorder(clock=lambda: next(ticks))
    assert rec.toggle(640, 360)["type"] == "in"
    assert rec.toggle(640, 360)["type"] == "out"
    assert rec.write_sidecar(str(vid)) == str(sidecar)
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert [e["type"] for e in data["events"]] == ["in", "out"]
