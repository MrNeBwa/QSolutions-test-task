import json
import math
from pathlib import Path

import pytest

import apply_zoom
import qscript


def test_ramp_frames():
    assert apply_zoom.compute_ramp_frames(0.5, 25) == 12
    assert apply_zoom.compute_ramp_frames(0.4, 25) == 10
    assert apply_zoom.compute_ramp_frames(0.0, 25) == 0


def test_coordinate_mapping_and_clamping():
    assert apply_zoom.map_coords(640, 360, 1280, 720, 1280, 720) == (640.0, 360.0)
    assert apply_zoom.map_coords(-10, 999, 1280, 720, 1280, 720) == (0.0, 720.0)

    # 2x zoom on 1280x720 -> crop 640x360
    assert apply_zoom.clamp_crop(500, 300, 2.0, 1280, 720) == (pytest.approx(180.0), pytest.approx(120.0))
    assert apply_zoom.clamp_crop(20, 10, 2.0, 1280, 720) == (0.0, 0.0)
    assert apply_zoom.clamp_crop(1260, 710, 2.0, 1280, 720) == (pytest.approx(640.0), pytest.approx(360.0))


def test_single_pair_easing_and_filter():
    sidecar = apply_zoom.parse_sidecar({
        "viewport": {"width": 1280, "height": 720},
        "fps": 25,
        "zoom_factor": 2.0,
        "ramp_sec": 0.4,
        "events": [
            {"t": 2.0, "x": 640, "y": 360, "type": "in"},
            {"t": 6.0, "x": 640, "y": 360, "type": "out"},
        ],
    })
    segs = apply_zoom.build_segments(sidecar)
    assert len(segs) == 1
    assert (segs[0]["in_frame"], segs[0]["hold_start"], segs[0]["out_frame"], segs[0]["end_frame"]) == (50, 60, 150, 160)

    # Smooth cosine ramp-in, flat hold, return to 1.0
    assert apply_zoom.evaluate_zoom(segs, 49, 1280, 720)[0] == pytest.approx(1.0)
    assert apply_zoom.evaluate_zoom(segs, 52, 1280, 720)[0] == pytest.approx(1.0 + 0.5 * (1.0 - math.cos(0.2 * math.pi)))
    assert apply_zoom.evaluate_zoom(segs, 100, 1280, 720) == (pytest.approx(2.0), pytest.approx(320.0), pytest.approx(180.0))
    assert apply_zoom.evaluate_zoom(segs, 160, 1280, 720)[0] == pytest.approx(1.0)

    vf = apply_zoom.build_zoompan_filter(segs, 1280, 720, 25)
    assert "zoompan=" in vf
    assert "cos(PI*(on-50)/10)" in vf
    assert "between(on,60,149)" in vf
    assert "clip(640-iw/(2*zoom),0,iw-iw/zoom)" in vf
    assert ":d=1:s=1280x720:fps=25" in vf


def test_multiple_pairs():
    sidecar = apply_zoom.parse_sidecar({
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
    })
    segs = apply_zoom.build_segments(sidecar)
    assert len(segs) == 2
    assert segs[0]["end_frame"] <= segs[1]["in_frame"]
    assert apply_zoom.evaluate_zoom(segs, 50, 1280, 720) == (pytest.approx(2.0), 0.0, 0.0)
    assert apply_zoom.evaluate_zoom(segs, 110, 1280, 720) == (1.0, 0.0, 0.0)
    assert apply_zoom.evaluate_zoom(segs, 175, 1280, 720) == (pytest.approx(2.0), pytest.approx(640.0), pytest.approx(360.0))


def test_open_ended_zoom_holds_to_end():
    sidecar = apply_zoom.parse_sidecar({
        "viewport": {"width": 1280, "height": 720},
        "fps": 25,
        "zoom_factor": 2.0,
        "ramp_sec": 0.5,
        "events": [{"t": 2.0, "x": 500, "y": 300, "type": "in"}],
    })
    segs = apply_zoom.build_segments(sidecar)
    assert len(segs) == 1
    assert segs[0]["out_frame"] is None
    assert apply_zoom.evaluate_zoom(segs, 5000, 1280, 720)[0] == pytest.approx(2.0)
    assert "gte(on,62)" in apply_zoom.build_zoompan_filter(segs, 1280, 720, 25)


def test_old_sidecars_without_type():
    sc_toggle = apply_zoom.parse_sidecar({
        "events": [
            {"t": 2.0, "x": 400, "y": 300},
            {"t": 5.0, "x": 400, "y": 300},
            {"t": 8.0, "x": 900, "y": 500},
        ]
    })
    assert [e["type"] for e in sc_toggle["events"]] == ["in", "out", "in"]

    sc_hold = apply_zoom.parse_sidecar({
        "ramp_sec": 0.5,
        "hold_sec": 2.0,
        "events": [{"t": 3.0, "x": 640, "y": 360}],
    })
    assert [(e["type"], e["t"]) for e in sc_hold["events"]] == [("in", 3.0), ("out", 5.5)]


def test_no_events_copies_unchanged(tmp_path: Path):
    src = tmp_path / "clip.webm"
    src.write_bytes(b"RAW_WEBM_DATA")
    (tmp_path / "clip.zoom.json").write_text(json.dumps({"events": []}), encoding="utf-8")

    res = apply_zoom.process_video(src, ffmpeg="/nonexistent/ffmpeg")
    assert res["action"] == "copy"
    assert Path(res["output"]).read_bytes() == b"RAW_WEBM_DATA"


def test_dry_run_and_custom_ffmpeg(tmp_path: Path, capsys):
    vid = tmp_path / "ACME.webm"
    (tmp_path / "ACME.zoom.json").write_text(json.dumps({
        "viewport": {"width": 1280, "height": 720},
        "fps": 25,
        "zoom_factor": 2.0,
        "ramp_sec": 0.5,
        "events": [
            {"t": 3.0, "x": 640, "y": 360, "type": "in"},
            {"t": 8.0, "x": 640, "y": 360, "type": "out"},
        ],
    }), encoding="utf-8")

    rc = apply_zoom.main(["--video", str(vid), "--dry-run", "--ffmpeg", "/custom/ffmpeg"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("/custom/ffmpeg ")
    assert "zoompan=" in out
    assert not (tmp_path / "ACME_zoomed.webm").exists()


def test_zoom_recorder_writes_sidecar_only_with_events(tmp_path: Path):
    vid = tmp_path / "ACME.webm"
    vid.write_bytes(b"webm")
    sidecar = tmp_path / "ACME.zoom.json"

    empty = qscript.ZoomRecorder(clock=lambda: 0.0)
    assert empty.write_sidecar(str(vid)) is None
    assert not sidecar.exists()

    ticks = iter([10.0, 13.0, 18.0])
    rec = qscript.ZoomRecorder(clock=lambda: next(ticks))
    assert rec.toggle(640, 360)["type"] == "in"
    assert rec.toggle(640, 360)["type"] == "out"
    assert rec.write_sidecar(str(vid)) == str(sidecar)
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert [e["type"] for e in data["events"]] == ["in", "out"]
