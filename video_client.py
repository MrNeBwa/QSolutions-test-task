import os
import time

import requests


def _find_sidecar(video_path: str):
    for p in (os.path.splitext(video_path)[0] + ".zoom.json", video_path + ".zoom.json"):
        if os.path.isfile(p):
            return p
    return None


def UploadVideo(server_url: str, video_path: str, company_name: str, wait_for_processing: bool = False) -> str:
    """Uploads a video file (and its .zoom.json sidecar if present) to the server and returns its URL."""
    url = server_url.rstrip("/") + "/upload"
    sidecar = _find_sidecar(video_path)

    with open(video_path, "rb") as f:
        files = {"file": (os.path.basename(video_path), f)}
        data = {"company_name": company_name}
        if sidecar:
            with open(sidecar, "rb") as sf:
                files["sidecar"] = (os.path.basename(sidecar), sf, "application/json")
                resp = requests.post(url, files=files, data=data, timeout=60)
        else:
            resp = requests.post(url, files=files, data=data, timeout=60)

    resp.raise_for_status()
    payload = resp.json()

    if wait_for_processing and payload.get("job_id"):
        job_url = f"{server_url.rstrip('/')}/jobs/{payload['job_id']}"
        for _ in range(180):
            job = requests.get(job_url, timeout=30).json()
            if job.get("status") == "completed" and job.get("processed_video_url"):
                return job["processed_video_url"]
            if job.get("status") == "failed":
                raise RuntimeError(f"Zoom processing failed: {job.get('error')}")
            time.sleep(1)

    return payload["video_url"]


def GetVideoURL(server_url: str, company_name: str, prefer_processed: bool = False) -> str:
    """Returns the video URL for the given company name."""
    url = server_url.rstrip("/") + "/get_video_url"
    resp = requests.get(url, params={"company_name": company_name}, timeout=30)
    if resp.status_code == 404:
        raise FileNotFoundError(f"No video found for company: {company_name}")
    resp.raise_for_status()
    payload = resp.json()
    if prefer_processed and payload.get("processed_video_url"):
        return payload["processed_video_url"]
    return payload["video_url"]
