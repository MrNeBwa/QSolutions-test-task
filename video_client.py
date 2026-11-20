from __future__ import annotations

import os
import time
from typing import Any, Optional

import requests


def find_sidecar_for_video(video_path: str) -> Optional[str]:
    """Find a zoom sidecar file next to ``video_path`` if one exists."""
    stem_sidecar = os.path.splitext(video_path)[0] + ".zoom.json"
    if os.path.isfile(stem_sidecar):
        return stem_sidecar
    full_sidecar = video_path + ".zoom.json"
    if os.path.isfile(full_sidecar):
        return full_sidecar
    return None


def UploadVideo(
    server_url: str,
    video_path: str,
    company_name: str,
    sidecar_path: Optional[str] = None,
    wait_for_processing: bool = False,
    poll_timeout: float = 180.0,
    poll_interval: float = 1.0,
) -> str:
    """Upload a video (and its ``.zoom.json`` sidecar if present) and return its URL.

    When ``wait_for_processing=True`` and a zoom processing job is created on
    the server, waits for completion and returns ``processed_video_url``.
    """
    url = server_url.rstrip("/") + "/upload"
    resolved_sidecar = sidecar_path if sidecar_path is not None else find_sidecar_for_video(video_path)

    with open(video_path, "rb") as vf:
        files: dict[str, Any] = {"file": (os.path.basename(video_path), vf)}
        data = {"company_name": company_name}
        if resolved_sidecar and os.path.isfile(resolved_sidecar):
            with open(resolved_sidecar, "rb") as sf:
                files["sidecar"] = (os.path.basename(resolved_sidecar), sf, "application/json")
                resp = requests.post(url, files=files, data=data, timeout=120)
        else:
            resp = requests.post(url, files=files, data=data, timeout=120)

    resp.raise_for_status()
    payload = resp.json()

    if wait_for_processing and payload.get("job_id") is not None:
        job = WaitForJob(
            server_url,
            int(payload["job_id"]),
            timeout=poll_timeout,
            poll_interval=poll_interval,
        )
        if job.get("processed_video_url"):
            return job["processed_video_url"]

    return payload["video_url"]


def GetVideoURL(server_url: str, company_name: str, prefer_processed: bool = False) -> str:
    """Return the video URL for the given company name."""
    url = server_url.rstrip("/") + "/get_video_url"
    resp = requests.get(url, params={"company_name": company_name}, timeout=30)
    if resp.status_code == 404:
        raise FileNotFoundError(f"No video found for company: {company_name}")
    resp.raise_for_status()
    payload = resp.json()
    if prefer_processed and payload.get("processed_video_url"):
        return payload["processed_video_url"]
    return payload["video_url"]


def GetJobStatus(server_url: str, job_id: int) -> dict[str, Any]:
    """Fetch the status of a zoom processing job from the server."""
    url = f"{server_url.rstrip('/')}/jobs/{int(job_id)}"
    resp = requests.get(url, timeout=30)
    if resp.status_code == 404:
        raise FileNotFoundError(f"No job found with id: {job_id}")
    resp.raise_for_status()
    return resp.json()


def TriggerJobProcess(server_url: str, job_id: int, force: bool = False) -> dict[str, Any]:
    """Explicitly trigger zoom post-processing via ``POST /jobs/<id>/process``."""
    url = f"{server_url.rstrip('/')}/jobs/{int(job_id)}/process"
    params = {"force": "true"} if force else None
    resp = requests.post(url, params=params, timeout=300)
    resp.raise_for_status()
    return resp.json()


def WaitForJob(
    server_url: str,
    job_id: int,
    timeout: float = 180.0,
    poll_interval: float = 1.0,
) -> dict[str, Any]:
    """Poll ``GET /jobs/<id>`` until the job completes or fails."""
    deadline = time.monotonic() + timeout
    while True:
        job = GetJobStatus(server_url, job_id)
        status = job.get("status")
        if status == "completed":
            return job
        if status == "failed":
            raise RuntimeError(f"Zoom processing job {job_id} failed: {job.get('error')}")
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Timed out waiting for zoom job {job_id} (last status: {status})")
        time.sleep(poll_interval)
