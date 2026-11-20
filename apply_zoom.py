import argparse
import json
import math
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 720
DEFAULT_FPS = 25
DEFAULT_ZOOM = 2.0
DEFAULT_RAMP_SEC = 0.5


def _fmt(v):
    if abs(v - round(v)) < 1e-6:
        return str(int(round(v)))
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return s if s and s != "-0" else "0"


def find_sidecar(video_path):
    p = Path(video_path)
    for candidate in (p.with_suffix(".zoom.json"), Path(f"{p}.zoom.json")):
        if candidate.is_file():
            return candidate
    return None


def default_output_path(video_path):
    p = Path(video_path)
    return p.with_name(f"{p.stem}_zoomed.webm")


def parse_sidecar(data):
    if isinstance(data, list):
        data = {"events": data}

    vp = data.get("viewport") or {}
    width = int(vp.get("width", DEFAULT_WIDTH))
    height = int(vp.get("height", DEFAULT_HEIGHT))
    fps = float(data.get("fps", DEFAULT_FPS))
    zoom_factor = float(data.get("zoom_factor", DEFAULT_ZOOM))
    ramp_sec = float(data.get("ramp_sec", DEFAULT_RAMP_SEC))
    top_hold = data.get("hold_sec")

    if width <= 0 or height <= 0 or fps <= 0:
        raise ValueError("viewport dimensions and fps must be positive")
    if zoom_factor < 1.0 or ramp_sec < 0.0:
        raise ValueError("invalid zoom_factor or ramp_sec")

    raw_events = data.get("events") or []
    sorted_events = sorted(enumerate(raw_events), key=lambda item: (max(0.0, float(item[1]["t"])), item[0]))

    events = []
    zoomed = False
    for _, ev in sorted_events:
        t = max(0.0, float(ev["t"]))
        x = float(ev["x"])
        y = float(ev["y"])
        ev_type = ev.get("type")

        if ev_type:
            ev_type = str(ev_type).strip().lower()
            if ev_type not in ("in", "out"):
                raise ValueError(f"invalid event type: {ev_type}")
            events.append({"t": t, "x": x, "y": y, "type": ev_type})
            zoomed = ev_type == "in"
        else:
            # Support old sidecars without "type"
            duration = ev.get("duration")
            hold_sec = ev.get("hold_sec", top_hold)
            if duration is not None or hold_sec is not None:
                t_out = t + float(duration) if duration is not None else t + ramp_sec + float(hold_sec)
                events.append({"t": t, "x": x, "y": y, "type": "in"})
                events.append({"t": t_out, "x": x, "y": y, "type": "out"})
                zoomed = False
            else:
                inferred = "out" if zoomed else "in"
                events.append({"t": t, "x": x, "y": y, "type": inferred})
                zoomed = not zoomed

    return {
        "width": width,
        "height": height,
        "fps": fps,
        "zoom_factor": zoom_factor,
        "ramp_sec": ramp_sec,
        "events": events,
    }


def load_sidecar(path):
    with open(path, "r", encoding="utf-8") as f:
        return parse_sidecar(json.load(f))


def map_coords(x, y, vp_w, vp_h, vid_w, vid_h):
    px = float(x) * (vid_w / float(vp_w))
    py = float(y) * (vid_h / float(vp_h))
    return max(0.0, min(float(vid_w), px)), max(0.0, min(float(vid_h), py))


def compute_ramp_frames(ramp_sec, fps):
    if ramp_sec <= 0:
        return 0
    return max(1, int(round(ramp_sec * fps)))


def clamp_crop(px, py, zoom, width, height):
    if zoom <= 1.0:
        return 0.0, 0.0
    crop_w = width / zoom
    crop_h = height / zoom
    x = max(0.0, min(px - crop_w / 2.0, width - crop_w))
    y = max(0.0, min(py - crop_h / 2.0, height - crop_h))
    return x, y


def build_segments(sidecar, video_width=None, video_height=None, fps=None):
    vid_w = int(video_width or sidecar["width"])
    vid_h = int(video_height or sidecar["height"])
    eff_fps = float(fps or sidecar["fps"])
    zoom = float(sidecar["zoom_factor"])
    ramp = compute_ramp_frames(sidecar["ramp_sec"], eff_fps)

    if zoom <= 1.0 or not sidecar["events"]:
        return []

    pairs = []
    pending_in = None
    for ev in sidecar["events"]:
        if ev["type"] == "in" and pending_in is None:
            pending_in = ev
        elif ev["type"] == "out" and pending_in is not None:
            pairs.append((pending_in, ev))
            pending_in = None
    if pending_in is not None:
        pairs.append((pending_in, None))

    segments = []
    for in_ev, out_ev in pairs:
        f_in = max(0, int(round(in_ev["t"] * eff_fps)))
        px, py = map_coords(in_ev["x"], in_ev["y"], sidecar["width"], sidecar["height"], vid_w, vid_h)
        if out_ev is None:
            segments.append({
                "in_frame": f_in,
                "hold_start": f_in + ramp,
                "out_frame": None,
                "end_frame": None,
                "px": px,
                "py": py,
                "zoom": zoom,
            })
        else:
            f_out = max(f_in, int(round(out_ev["t"] * eff_fps)))
            if f_out == f_in:
                continue
            r_in = min(ramp, f_out - f_in)
            segments.append({
                "in_frame": f_in,
                "hold_start": f_in + r_in,
                "out_frame": f_out,
                "end_frame": f_out + ramp,
                "px": px,
                "py": py,
                "zoom": zoom,
            })

    # Keep consecutive segments non-overlapping
    for i in range(len(segments) - 1):
        if segments[i]["end_frame"] is not None and segments[i]["end_frame"] > segments[i + 1]["in_frame"]:
            segments[i]["end_frame"] = max(segments[i]["out_frame"], segments[i + 1]["in_frame"])

    return segments


def evaluate_zoom(segments, frame, width, height):
    for seg in segments:
        if frame < seg["in_frame"]:
            continue
        if seg["end_frame"] is not None and frame >= seg["end_frame"]:
            continue

        dz = seg["zoom"] - 1.0
        r_in = seg["hold_start"] - seg["in_frame"]
        if r_in > 0 and frame < seg["hold_start"]:
            p = (frame - seg["in_frame"]) / float(r_in)
            z = 1.0 + dz * 0.5 * (1.0 - math.cos(math.pi * p))
        elif seg["out_frame"] is None or frame < seg["out_frame"]:
            z = seg["zoom"]
        else:
            r_out = seg["end_frame"] - seg["out_frame"]
            p = (frame - seg["out_frame"]) / float(r_out) if r_out > 0 else 1.0
            z = 1.0 + dz * 0.5 * (1.0 + math.cos(math.pi * p))

        cx, cy = clamp_crop(seg["px"], seg["py"], z, width, height)
        return z, cx, cy
    return 1.0, 0.0, 0.0


def build_zoompan_filter(segments, width, height, fps):
    if not segments:
        raise ValueError("segments cannot be empty")

    z_terms, x_terms, y_terms = [], [], []
    for seg in segments:
        dz = _fmt(seg["zoom"] - 1.0)
        parts = []

        r_in = seg["hold_start"] - seg["in_frame"]
        if r_in > 0:
            parts.append(
                f"between(on,{seg['in_frame']},{seg['hold_start'] - 1})"
                f"*0.5*(1-cos(PI*(on-{seg['in_frame']})/{r_in}))"
            )

        if seg["out_frame"] is None:
            parts.append(f"gte(on,{seg['hold_start']})")
            active = f"gte(on,{seg['in_frame']})"
        else:
            if seg["out_frame"] > seg["hold_start"]:
                parts.append(f"between(on,{seg['hold_start']},{seg['out_frame'] - 1})")
            r_out = seg["end_frame"] - seg["out_frame"]
            if r_out > 0:
                parts.append(
                    f"between(on,{seg['out_frame']},{seg['end_frame'] - 1})"
                    f"*0.5*(1+cos(PI*(on-{seg['out_frame']})/{r_out}))"
                )
            active = f"between(on,{seg['in_frame']},{seg['end_frame'] - 1})"

        expr = "+".join(parts)
        z_terms.append(expr if dz == "1" else f"{dz}*({expr})")
        x_terms.append(f"{active}*{_fmt(seg['px'])}")
        y_terms.append(f"{active}*{_fmt(seg['py'])}")

    z_expr = "1+" + "+".join(z_terms)
    tx = _fmt(segments[0]["px"]) if len(segments) == 1 else "(" + "+".join(x_terms) + ")"
    ty = _fmt(segments[0]["py"]) if len(segments) == 1 else "(" + "+".join(y_terms) + ")"
    x_expr = f"clip({tx}-iw/(2*zoom),0,iw-iw/zoom)"
    y_expr = f"clip({ty}-ih/(2*zoom),0,ih-ih/zoom)"

    fps_s = _fmt(fps)
    return f"fps={fps_s},zoompan=z='{z_expr}':x='{x_expr}':y='{y_expr}':d=1:s={int(width)}x{int(height)}:fps={fps_s}"


def probe_video(video_path):
    if not os.path.isfile(video_path):
        return None
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,avg_frame_rate",
        "-of", "json", str(video_path),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if res.returncode != 0:
            return None
        stream = json.loads(res.stdout)["streams"][0]
        rate = stream.get("avg_frame_rate", "")
        fps = None
        if "/" in rate:
            num, den = map(float, rate.split("/", 1))
            if den > 0 and num > 0:
                fps = num / den
        return {"width": int(stream["width"]), "height": int(stream["height"]), "fps": fps}
    except Exception:
        return None


def build_ffmpeg_cmd(video_path, output_path, vf, ffmpeg="ffmpeg"):
    return [
        ffmpeg, "-y", "-i", str(video_path),
        "-vf", vf,
        "-c:v", "libvpx-vp9", "-crf", "30", "-b:v", "0",
        "-deadline", "good", "-cpu-used", "4", "-row-mt", "1",
        "-pix_fmt", "yuv420p", "-c:a", "libopus",
        str(output_path),
    ]


def process_video(video_path, output_path=None, sidecar_path=None, dry_run=False, ffmpeg="ffmpeg"):
    in_path = Path(video_path)
    out_path = Path(output_path) if output_path else default_output_path(in_path)

    if sidecar_path:
        sidecar = load_sidecar(sidecar_path)
    else:
        found = find_sidecar(in_path)
        sidecar = load_sidecar(found) if found else parse_sidecar({"events": []})

    info = probe_video(in_path)
    width = info["width"] if info else sidecar["width"]
    height = info["height"] if info else sidecar["height"]
    fps = info["fps"] if (info and info["fps"]) else sidecar["fps"]

    segments = build_segments(sidecar, video_width=width, video_height=height, fps=fps)

    if not segments:
        if not dry_run:
            if not in_path.is_file():
                raise FileNotFoundError(f"Video not found: {in_path}")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            if in_path.resolve() != out_path.resolve():
                shutil.copy2(str(in_path), str(out_path))
        return {"action": "copy", "command": None, "output": str(out_path), "segments": 0}

    vf = build_zoompan_filter(segments, width, height, fps)
    cmd = build_ffmpeg_cmd(in_path, out_path, vf, ffmpeg=ffmpeg)

    if dry_run:
        return {"action": "dry_run", "command": cmd, "output": str(out_path), "segments": len(segments)}

    if not in_path.is_file():
        raise FileNotFoundError(f"Video not found: {in_path}")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg failed ({res.returncode}): {res.stderr.strip()}")
    return {"action": "encoded", "command": cmd, "output": str(out_path), "segments": len(segments)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Apply smooth zoom effect to a recorded video.")
    parser.add_argument("--video", required=True, help="Path to input video")
    parser.add_argument("-o", "--output", help="Path to output .webm (default: <video>_zoomed.webm)")
    parser.add_argument("--sidecar", help="Optional path to .zoom.json sidecar")
    parser.add_argument("--dry-run", action="store_true", help="Print ffmpeg command without encoding")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="Path to ffmpeg binary")
    args = parser.parse_args(argv)

    try:
        result = process_video(
            args.video,
            output_path=args.output,
            sidecar_path=args.sidecar,
            dry_run=args.dry_run,
            ffmpeg=args.ffmpeg,
        )
    except Exception as e:
        print("Error:", e, file=sys.stderr)
        return 1

    if result["action"] == "dry_run":
        print(shlex.join(result["command"]))
    elif result["action"] == "copy":
        print(f"No zoom events, copied unchanged -> {result['output']}")
    else:
        print(f"Saved zoomed video: {result['output']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
