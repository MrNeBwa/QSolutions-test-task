from __future__ import annotations

import argparse
import json
import math
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple, Union

DEFAULT_VIEWPORT_WIDTH = 1280
DEFAULT_VIEWPORT_HEIGHT = 720
DEFAULT_FPS = 25.0
DEFAULT_ZOOM_FACTOR = 2.0
DEFAULT_RAMP_SEC = 0.5


@dataclass(frozen=True)
class ZoomEvent:
    """Single zoom toggle event recorded relative to video start."""

    t: float
    x: float
    y: float
    type: str  # "in" or "out"


@dataclass(frozen=True)
class ZoomSidecar:
    """Parsed zoom sidecar configuration and normalized events."""

    viewport_width: int
    viewport_height: int
    fps: float
    zoom_factor: float
    ramp_sec: float
    events: Tuple[ZoomEvent, ...]


@dataclass(frozen=True)
class ZoomSegment:
    """Frame-aligned zoom segment (ramp-in -> hold -> optional ramp-out).

    When ``out_frame`` and ``end_frame`` are ``None``, the segment is open-ended
    and holds ``zoom_factor`` until the end of the video.
    """

    in_frame: int
    hold_start_frame: int
    out_frame: Optional[int]
    end_frame: Optional[int]
    px: float
    py: float
    zoom_factor: float


def _fmt_num(value: float) -> str:
    """Format a float concisely for FFmpeg filter expressions."""
    if abs(value - round(value)) < 1e-6:
        return str(int(round(value)))
    formatted = f"{value:.4f}".rstrip("0").rstrip(".")
    return formatted if formatted and formatted != "-0" else "0"


def get_sidecar_path(video_path: Union[str, Path]) -> Path:
    """Return the canonical sidecar path (<stem>.zoom.json) for a video."""
    path = Path(video_path)
    return path.with_suffix(".zoom.json")


def find_sidecar_path(video_path: Union[str, Path]) -> Optional[Path]:
    """Locate an existing sidecar next to a video file.

    Checks both ``<stem>.zoom.json`` (e.g. ``ACME.zoom.json``) and
    ``<video>.zoom.json`` (e.g. ``ACME.webm.zoom.json``).
    """
    path = Path(video_path)
    candidates = [
        path.with_suffix(".zoom.json"),
        Path(f"{path}.zoom.json"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def default_output_path(video_path: Union[str, Path]) -> Path:
    """Return the default zoomed WebM output path for a given input video."""
    path = Path(video_path)
    return path.with_name(f"{path.stem}_zoomed.webm")


def parse_sidecar_data(data: Any) -> ZoomSidecar:
    """Parse and normalize sidecar JSON data into a :class:`ZoomSidecar`.

    Supports both the current sidecar format (events with explicit
    ``"type": "in" | "out"``) and legacy sidecars whose events omit ``"type"``:
      - If ``"hold_sec"`` (top-level or per-event) or ``"duration"`` is present
        on an untyped event, each event expands into an ``in``/``out`` pair.
      - Otherwise, untyped events alternate ``"in"`` / ``"out"`` toggles.
    """
    if isinstance(data, list):
        raw_dict: dict[str, Any] = {"events": data}
    elif isinstance(data, dict):
        raw_dict = data
    else:
        raise ValueError("Sidecar data must be a JSON object or list of events")

    viewport = raw_dict.get("viewport") or {}
    if not isinstance(viewport, dict):
        raise ValueError("'viewport' must be an object with 'width' and 'height'")

    viewport_width = int(viewport.get("width", DEFAULT_VIEWPORT_WIDTH))
    viewport_height = int(viewport.get("height", DEFAULT_VIEWPORT_HEIGHT))
    if viewport_width <= 0 or viewport_height <= 0:
        raise ValueError("Viewport width and height must be positive")

    fps = float(raw_dict.get("fps", DEFAULT_FPS))
    if fps <= 0:
        raise ValueError("fps must be positive")

    zoom_factor = float(raw_dict.get("zoom_factor", DEFAULT_ZOOM_FACTOR))
    if zoom_factor < 1.0:
        raise ValueError("zoom_factor must be >= 1.0")

    ramp_sec = float(raw_dict.get("ramp_sec", DEFAULT_RAMP_SEC))
    if ramp_sec < 0.0:
        raise ValueError("ramp_sec must be >= 0.0")

    top_hold_sec = raw_dict.get("hold_sec")
    if top_hold_sec is not None:
        top_hold_sec = float(top_hold_sec)
        if top_hold_sec < 0.0:
            raise ValueError("hold_sec must be >= 0.0")

    raw_events = raw_dict.get("events", [])
    if raw_events is None:
        raw_events = []
    if not isinstance(raw_events, list):
        raise ValueError("'events' must be a list")

    indexed_events: List[Tuple[float, int, dict[str, Any]]] = []
    for idx, ev in enumerate(raw_events):
        if not isinstance(ev, dict):
            raise ValueError(f"Event at index {idx} must be an object")
        for key in ("t", "x", "y"):
            if key not in ev:
                raise ValueError(f"Event at index {idx} is missing required field '{key}'")
        t_val = max(0.0, float(ev["t"]))
        indexed_events.append((t_val, idx, ev))

    # Stable sort by timestamp so events with equal timestamps keep original order.
    indexed_events.sort(key=lambda item: (item[0], item[1]))

    normalized: List[ZoomEvent] = []
    currently_zoomed = False

    for t_val, idx, ev in indexed_events:
        x_val = float(ev["x"])
        y_val = float(ev["y"])
        raw_type = ev.get("type")

        if raw_type is not None and str(raw_type).strip() != "":
            ev_type = str(raw_type).strip().lower()
            if ev_type not in ("in", "out"):
                raise ValueError(
                    f"Event at index {idx} has invalid type {raw_type!r}; expected 'in' or 'out'"
                )
            normalized.append(ZoomEvent(t=t_val, x=x_val, y=y_val, type=ev_type))
            currently_zoomed = ev_type == "in"
        else:
            # Backwards compatibility for old sidecars without "type".
            ev_duration = ev.get("duration")
            ev_hold_sec = ev.get("hold_sec", top_hold_sec)
            if ev_duration is not None or ev_hold_sec is not None:
                if ev_duration is not None:
                    duration = float(ev_duration)
                    if duration < 0.0:
                        raise ValueError(f"Event at index {idx} has negative duration")
                    t_out = t_val + duration
                else:
                    hold = float(ev_hold_sec)
                    if hold < 0.0:
                        raise ValueError(f"Event at index {idx} has negative hold_sec")
                    t_out = t_val + ramp_sec + hold
                normalized.append(ZoomEvent(t=t_val, x=x_val, y=y_val, type="in"))
                normalized.append(ZoomEvent(t=t_out, x=x_val, y=y_val, type="out"))
                currently_zoomed = False
            else:
                inferred_type = "out" if currently_zoomed else "in"
                normalized.append(ZoomEvent(t=t_val, x=x_val, y=y_val, type=inferred_type))
                currently_zoomed = not currently_zoomed

    return ZoomSidecar(
        viewport_width=viewport_width,
        viewport_height=viewport_height,
        fps=fps,
        zoom_factor=zoom_factor,
        ramp_sec=ramp_sec,
        events=tuple(normalized),
    )


def load_sidecar(sidecar_path: Union[str, Path]) -> ZoomSidecar:
    """Load and parse a zoom sidecar JSON file from disk."""
    with open(sidecar_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return parse_sidecar_data(data)


def map_coords_to_video(
    x: float,
    y: float,
    viewport_width: int,
    viewport_height: int,
    video_width: int,
    video_height: int,
) -> Tuple[float, float]:
    """Map viewport mouse coordinates to video pixel coordinates, clamped to frame bounds.

    When ``video_width == viewport_width`` and ``video_height == viewport_height``,
    the mapping is 1:1.
    """
    if viewport_width <= 0 or viewport_height <= 0:
        raise ValueError("Viewport dimensions must be positive")
    if video_width <= 0 or video_height <= 0:
        raise ValueError("Video dimensions must be positive")

    scale_x = video_width / float(viewport_width)
    scale_y = video_height / float(viewport_height)
    px = max(0.0, min(float(video_width), float(x) * scale_x))
    py = max(0.0, min(float(video_height), float(y) * scale_y))
    return px, py


def compute_ramp_frames(ramp_sec: float, fps: float) -> int:
    """Compute the number of frames for a zoom-in or zoom-out ramp."""
    if fps <= 0:
        raise ValueError("fps must be positive")
    if ramp_sec <= 0:
        return 0
    return max(1, int(round(ramp_sec * fps)))


def clamp_crop_origin(
    px: float,
    py: float,
    zoom: float,
    video_width: int,
    video_height: int,
) -> Tuple[float, float]:
    """Compute top-left crop coordinates ``(x, y)`` centered on ``(px, py)`` within bounds.

    Matches the FFmpeg ``zoompan`` formula:
    ``x = clip(px - iw/(2*zoom), 0, iw - iw/zoom)`` (same for ``y``).
    """
    if zoom <= 1.0:
        return 0.0, 0.0
    crop_w = video_width / zoom
    crop_h = video_height / zoom
    max_x = max(0.0, video_width - crop_w)
    max_y = max(0.0, video_height - crop_h)
    x = max(0.0, min(px - crop_w / 2.0, max_x))
    y = max(0.0, min(py - crop_h / 2.0, max_y))
    return x, y


def build_zoom_segments(
    sidecar: ZoomSidecar,
    video_width: Optional[int] = None,
    video_height: Optional[int] = None,
    fps: Optional[float] = None,
    zoom_factor: Optional[float] = None,
    ramp_sec: Optional[float] = None,
) -> List[ZoomSegment]:
    """Pair ``in``/``out`` events into non-overlapping frame-based :class:`ZoomSegment`s.

    - Each ``in`` is paired with the next ``out``.
    - Stray ``out`` events while not zoomed are ignored.
    - Duplicate ``in`` events while already zoomed are ignored.
    - A trailing ``in`` without a following ``out`` creates an open-ended segment
      (``out_frame=None``, ``end_frame=None``) that holds zoom to the end of the video.
    """
    eff_w = int(video_width if video_width is not None else sidecar.viewport_width)
    eff_h = int(video_height if video_height is not None else sidecar.viewport_height)
    eff_fps = float(fps if fps is not None else sidecar.fps)
    eff_zoom = float(zoom_factor if zoom_factor is not None else sidecar.zoom_factor)
    eff_ramp_sec = float(ramp_sec if ramp_sec is not None else sidecar.ramp_sec)

    if eff_zoom <= 1.0 or not sidecar.events:
        return []

    ramp_frames = compute_ramp_frames(eff_ramp_sec, eff_fps)

    # Pair each 'in' with the next 'out'.
    pairs: List[Tuple[ZoomEvent, Optional[ZoomEvent]]] = []
    pending_in: Optional[ZoomEvent] = None
    for ev in sidecar.events:
        if ev.type == "in":
            if pending_in is None:
                pending_in = ev
        elif ev.type == "out":
            if pending_in is not None:
                pairs.append((pending_in, ev))
                pending_in = None
    if pending_in is not None:
        pairs.append((pending_in, None))

    if not pairs:
        return []

    raw_segments: List[ZoomSegment] = []
    for in_ev, out_ev in pairs:
        f_in = max(0, int(round(in_ev.t * eff_fps)))
        px, py = map_coords_to_video(
            in_ev.x,
            in_ev.y,
            sidecar.viewport_width,
            sidecar.viewport_height,
            eff_w,
            eff_h,
        )

        if out_ev is None:
            raw_segments.append(
                ZoomSegment(
                    in_frame=f_in,
                    hold_start_frame=f_in + ramp_frames,
                    out_frame=None,
                    end_frame=None,
                    px=px,
                    py=py,
                    zoom_factor=eff_zoom,
                )
            )
        else:
            f_out = max(f_in, int(round(out_ev.t * eff_fps)))
            if f_out == f_in:
                # Zero-duration toggle produces no visible zoom.
                continue
            r_in = min(ramp_frames, f_out - f_in)
            raw_segments.append(
                ZoomSegment(
                    in_frame=f_in,
                    hold_start_frame=f_in + r_in,
                    out_frame=f_out,
                    end_frame=f_out + ramp_frames,
                    px=px,
                    py=py,
                    zoom_factor=eff_zoom,
                )
            )

    # Ensure consecutive segments never overlap in frame space.
    segments: List[ZoomSegment] = []
    for i, seg in enumerate(raw_segments):
        if seg.end_frame is not None and i + 1 < len(raw_segments):
            next_in = raw_segments[i + 1].in_frame
            if seg.end_frame > next_in:
                clamped_end = max(seg.out_frame, next_in)  # type: ignore[arg-type]
                seg = ZoomSegment(
                    in_frame=seg.in_frame,
                    hold_start_frame=seg.hold_start_frame,
                    out_frame=seg.out_frame,
                    end_frame=clamped_end,
                    px=seg.px,
                    py=seg.py,
                    zoom_factor=seg.zoom_factor,
                )
        segments.append(seg)

    return segments


def evaluate_zoom_at_frame(
    segments: Sequence[ZoomSegment],
    frame: int,
    video_width: int,
    video_height: int,
) -> Tuple[float, float, float]:
    """Evaluate exact ``(zoom, crop_x, crop_y)`` at output frame ``frame``.

    Uses the exact same cosine ease-in/ease-out and clamping math as
    :func:`build_zoompan_filter`.
    """
    for seg in segments:
        if frame < seg.in_frame:
            continue
        if seg.end_frame is not None and frame >= seg.end_frame:
            continue

        dz = seg.zoom_factor - 1.0
        r_in = seg.hold_start_frame - seg.in_frame

        if r_in > 0 and frame < seg.hold_start_frame:
            progress = (frame - seg.in_frame) / float(r_in)
            zoom = 1.0 + dz * 0.5 * (1.0 - math.cos(math.pi * progress))
        elif seg.out_frame is None or frame < seg.out_frame:
            zoom = seg.zoom_factor
        else:
            r_out = (seg.end_frame - seg.out_frame) if seg.end_frame is not None else 0
            if r_out > 0 and frame < seg.end_frame:  # type: ignore[operator]
                progress = (frame - seg.out_frame) / float(r_out)
                zoom = 1.0 + dz * 0.5 * (1.0 + math.cos(math.pi * progress))
            else:
                zoom = 1.0

        crop_x, crop_y = clamp_crop_origin(seg.px, seg.py, zoom, video_width, video_height)
        return zoom, crop_x, crop_y

    return 1.0, 0.0, 0.0


def build_zoompan_filter(
    segments: Sequence[ZoomSegment],
    video_width: int,
    video_height: int,
    fps: float,
) -> str:
    """Generate the FFmpeg ``fps + zoompan`` video filter expression.

    Uses cosine easing (``0.5*(1-cos(PI*p))`` / ``0.5*(1+cos(PI*p))``) for smooth
    zoom-in and zoom-out transitions, ``clip(...)`` for crop-boundary clamping,
    and ``d=1`` so every input frame maps 1:1 to an output frame.
    """
    if not segments:
        raise ValueError("Cannot build zoompan filter without at least one ZoomSegment")
    if video_width <= 0 or video_height <= 0:
        raise ValueError("Video dimensions must be positive")
    if fps <= 0:
        raise ValueError("fps must be positive")

    z_terms: List[str] = []
    x_terms: List[str] = []
    y_terms: List[str] = []

    for seg in segments:
        dz_str = _fmt_num(seg.zoom_factor - 1.0)
        seg_parts: List[str] = []

        r_in = seg.hold_start_frame - seg.in_frame
        if r_in > 0:
            seg_parts.append(
                f"between(on,{seg.in_frame},{seg.hold_start_frame - 1})"
                f"*0.5*(1-cos(PI*(on-{seg.in_frame})/{r_in}))"
            )

        if seg.out_frame is None:
            seg_parts.append(f"gte(on,{seg.hold_start_frame})")
            active_cond = f"gte(on,{seg.in_frame})"
        else:
            if seg.out_frame > seg.hold_start_frame:
                seg_parts.append(f"between(on,{seg.hold_start_frame},{seg.out_frame - 1})")
            r_out = (seg.end_frame - seg.out_frame) if seg.end_frame is not None else 0
            if r_out > 0 and seg.end_frame is not None:
                seg_parts.append(
                    f"between(on,{seg.out_frame},{seg.end_frame - 1})"
                    f"*0.5*(1+cos(PI*(on-{seg.out_frame})/{r_out}))"
                )
            active_end = (seg.end_frame - 1) if (seg.end_frame is not None and seg.end_frame > seg.in_frame) else seg.out_frame - 1
            active_cond = f"between(on,{seg.in_frame},{active_end})"

        if seg_parts:
            seg_expr = "+".join(seg_parts)
            if dz_str == "1":
                z_terms.append(seg_expr)
            else:
                z_terms.append(f"{dz_str}*({seg_expr})")

        px_str = _fmt_num(seg.px)
        py_str = _fmt_num(seg.py)
        x_terms.append(f"{active_cond}*{px_str}")
        y_terms.append(f"{active_cond}*{py_str}")

    z_expr = "1+" + "+".join(z_terms)

    if len(segments) == 1:
        target_x = _fmt_num(segments[0].px)
        target_y = _fmt_num(segments[0].py)
    else:
        target_x = "(" + "+".join(x_terms) + ")"
        target_y = "(" + "+".join(y_terms) + ")"

    x_expr = f"clip({target_x}-iw/(2*zoom),0,iw-iw/zoom)"
    y_expr = f"clip({target_y}-ih/(2*zoom),0,ih-ih/zoom)"

    fps_str = _fmt_num(fps)
    return (
        f"fps={fps_str},"
        f"zoompan=z='{z_expr}':x='{x_expr}':y='{y_expr}'"
        f":d=1:s={int(video_width)}x{int(video_height)}:fps={fps_str}"
    )


def _infer_ffprobe_bin(ffmpeg_bin: str, ffprobe_bin: Optional[str]) -> str:
    if ffprobe_bin:
        return ffprobe_bin
    ffmpeg_path = Path(ffmpeg_bin)
    if ffmpeg_path.parent != Path("."):
        name = "ffprobe.exe" if ffmpeg_path.suffix.lower() == ".exe" else "ffprobe"
        candidate = ffmpeg_path.with_name(name)
        if candidate.is_file():
            return str(candidate)
    return "ffprobe"


def _parse_frame_rate(rate_str: Optional[str]) -> Optional[float]:
    if not rate_str or rate_str in ("0/0", "N/A"):
        return None
    if "/" in rate_str:
        num_s, den_s = rate_str.split("/", 1)
        num, den = float(num_s), float(den_s)
        if den > 0 and num > 0:
            return num / den
        return None
    val = float(rate_str)
    return val if val > 0 else None


def probe_video_info(
    video_path: Union[str, Path],
    ffprobe_bin: str = "ffprobe",
) -> Optional[dict[str, Any]]:
    """Probe input video width, height, and fps via ``ffprobe``.

    Returns ``None`` if the video file does not exist or ``ffprobe`` is unavailable.
    """
    path = Path(video_path)
    if not path.is_file():
        return None
    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,r_frame_rate,avg_frame_rate",
        "-of",
        "json",
        str(path),
    ]
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout)
        streams = payload.get("streams") or []
        if not streams:
            return None
        stream = streams[0]
        width = int(stream["width"])
        height = int(stream["height"])
        fps = _parse_frame_rate(stream.get("avg_frame_rate")) or _parse_frame_rate(
            stream.get("r_frame_rate")
        )
        return {"width": width, "height": height, "fps": fps}
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        return None


def build_ffmpeg_command(
    video_path: Union[str, Path],
    output_path: Union[str, Path],
    filter_expr: str,
    ffmpeg_bin: str = "ffmpeg",
) -> List[str]:
    """Build the FFmpeg command to render the zoomed WebM video (VP9)."""
    return [
        ffmpeg_bin,
        "-y",
        "-i",
        str(video_path),
        "-vf",
        filter_expr,
        "-c:v",
        "libvpx-vp9",
        "-crf",
        "30",
        "-b:v",
        "0",
        "-deadline",
        "good",
        "-cpu-used",
        "4",
        "-row-mt",
        "1",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "libopus",
        str(output_path),
    ]


def process_video(
    video_path: Union[str, Path],
    output_path: Optional[Union[str, Path]] = None,
    sidecar_path: Optional[Union[str, Path]] = None,
    sidecar_data: Optional[Any] = None,
    *,
    dry_run: bool = False,
    ffmpeg_bin: str = "ffmpeg",
    ffprobe_bin: Optional[str] = None,
    zoom_factor: Optional[float] = None,
    ramp_sec: Optional[float] = None,
    fps: Optional[float] = None,
) -> dict[str, Any]:
    """Post-process a video with Camtasia-style smooth zoom using FFmpeg ``zoompan``.

    If no sidecar exists (when not explicitly specified) or there are no zoom
    events, the input video is copied unchanged to ``output_path``.
    """
    in_path = Path(video_path)
    out_path = Path(output_path) if output_path is not None else default_output_path(in_path)

    if sidecar_data is not None:
        sidecar = parse_sidecar_data(sidecar_data)
        resolved_sidecar: Optional[Path] = Path(sidecar_path) if sidecar_path else None
    elif sidecar_path is not None:
        resolved_sidecar = Path(sidecar_path)
        if not resolved_sidecar.is_file():
            raise FileNotFoundError(f"Sidecar file not found: {resolved_sidecar}")
        sidecar = load_sidecar(resolved_sidecar)
    else:
        resolved_sidecar = find_sidecar_path(in_path)
        if resolved_sidecar is not None:
            sidecar = load_sidecar(resolved_sidecar)
        else:
            sidecar = ZoomSidecar(
                viewport_width=DEFAULT_VIEWPORT_WIDTH,
                viewport_height=DEFAULT_VIEWPORT_HEIGHT,
                fps=DEFAULT_FPS,
                zoom_factor=DEFAULT_ZOOM_FACTOR,
                ramp_sec=DEFAULT_RAMP_SEC,
                events=(),
            )

    probe_bin = _infer_ffprobe_bin(ffmpeg_bin, ffprobe_bin)
    probe_info = probe_video_info(in_path, ffprobe_bin=probe_bin)

    video_w = probe_info["width"] if probe_info else sidecar.viewport_width
    video_h = probe_info["height"] if probe_info else sidecar.viewport_height
    eff_fps = float(
        fps
        if fps is not None
        else (probe_info["fps"] if (probe_info and probe_info.get("fps")) else sidecar.fps)
    )

    segments = build_zoom_segments(
        sidecar,
        video_width=video_w,
        video_height=video_h,
        fps=eff_fps,
        zoom_factor=zoom_factor,
        ramp_sec=ramp_sec,
    )

    if not segments:
        if not dry_run:
            if not in_path.is_file():
                raise FileNotFoundError(f"Input video not found: {in_path}")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            if in_path.resolve() != out_path.resolve():
                shutil.copy2(str(in_path), str(out_path))
        return {
            "action": "copy",
            "command": None,
            "filter": None,
            "input": str(in_path),
            "output": str(out_path),
            "sidecar": str(resolved_sidecar) if resolved_sidecar else None,
            "segments": 0,
            "width": video_w,
            "height": video_h,
            "fps": eff_fps,
        }

    filter_expr = build_zoompan_filter(segments, video_w, video_h, eff_fps)
    display_cmd = build_ffmpeg_command(in_path, out_path, filter_expr, ffmpeg_bin=ffmpeg_bin)

    if dry_run:
        return {
            "action": "dry_run",
            "command": display_cmd,
            "filter": filter_expr,
            "input": str(in_path),
            "output": str(out_path),
            "sidecar": str(resolved_sidecar) if resolved_sidecar else None,
            "segments": len(segments),
            "width": video_w,
            "height": video_h,
            "fps": eff_fps,
        }

    if not in_path.is_file():
        raise FileNotFoundError(f"Input video not found: {in_path}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{out_path.stem}.",
        suffix=out_path.suffix or ".webm",
        dir=str(out_path.parent),
    )
    os.close(fd)
    tmp_path = Path(tmp_name)

    try:
        run_cmd = build_ffmpeg_command(in_path, tmp_path, filter_expr, ffmpeg_bin=ffmpeg_bin)
        try:
            proc = subprocess.run(
                run_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise FileNotFoundError(
                f"ffmpeg binary not found: {ffmpeg_bin!r}. "
                "Install ffmpeg or pass --ffmpeg <path>."
            ) from exc

        if proc.returncode != 0:
            raise RuntimeError(
                f"ffmpeg failed with exit code {proc.returncode}:\n{proc.stderr.strip()}"
            )

        os.replace(str(tmp_path), str(out_path))
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise

    return {
        "action": "encoded",
        "command": display_cmd,
        "filter": filter_expr,
        "input": str(in_path),
        "output": str(out_path),
        "sidecar": str(resolved_sidecar) if resolved_sidecar else None,
        "segments": len(segments),
        "width": video_w,
        "height": video_h,
        "fps": eff_fps,
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Apply Camtasia-style smooth zoom effects to a recorded video using FFmpeg."
    )
    parser.add_argument(
        "--video",
        required=True,
        help="Path to the input video file (e.g. video/ACME_GmbH.webm).",
    )
    parser.add_argument(
        "--sidecar",
        default=None,
        help="Optional path to the .zoom.json sidecar file (auto-discovered by default).",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Path for the output zoomed .webm video (default: <stem>_zoomed.webm).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the FFmpeg command without encoding.",
    )
    parser.add_argument(
        "--ffmpeg",
        default=os.environ.get("FFMPEG_PATH", "ffmpeg"),
        help="Path to the ffmpeg binary when not on PATH.",
    )
    parser.add_argument(
        "--ffprobe",
        default=None,
        help="Optional path to the ffprobe binary.",
    )
    parser.add_argument(
        "--zoom-factor",
        type=float,
        default=None,
        help="Override the sidecar zoom factor (e.g. 2.0).",
    )
    parser.add_argument(
        "--ramp-sec",
        type=float,
        default=None,
        help="Override the sidecar ramp duration in seconds (e.g. 0.5).",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=None,
        help="Override the output frame rate.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    try:
        result = process_video(
            video_path=args.video,
            output_path=args.output,
            sidecar_path=args.sidecar,
            dry_run=args.dry_run,
            ffmpeg_bin=args.ffmpeg,
            ffprobe_bin=args.ffprobe,
            zoom_factor=args.zoom_factor,
            ramp_sec=args.ramp_sec,
            fps=args.fps,
        )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if result["action"] == "dry_run":
        print(shlex.join(result["command"]))
    elif result["action"] == "copy":
        if args.dry_run:
            print(
                f"# No zoom events; would copy {result['input']} -> {result['output']} unchanged"
            )
        else:
            print(f"No zoom events; copied unchanged: {result['output']}")
    else:
        print(
            f"Saved zoomed video ({result['segments']} segment(s), "
            f"{result['width']}x{result['height']}@{_fmt_num(result['fps'])}fps): "
            f"{result['output']}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
