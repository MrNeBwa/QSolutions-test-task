from typing import Optional
from urllib.parse import quote

import requests


def UploadVideo(server_url: str, video_path: str, company_name: str) -> str:
    """Uploads a video file to the server and returns its URL."""
    url = server_url.rstrip("/") + "/upload"
    with open(video_path, "rb") as f:
        files = {"file": (video_path.split("/")[-1], f)}
        data = {"company_name": company_name}
        resp = requests.post(url, files=files, data=data, timeout=60)
    resp.raise_for_status()
    return resp.json()["video_url"]


def GetVideoURL(server_url: str, company_name: str) -> str:
    """Returns the video URL for the given company name."""
    url = server_url.rstrip("/") + "/get_video_url"
    resp = requests.get(url, params={"company_name": company_name}, timeout=30)
    if resp.status_code == 404:
        raise FileNotFoundError(f"No video found for company: {company_name}")
    resp.raise_for_status()
    return resp.json()["video_url"]