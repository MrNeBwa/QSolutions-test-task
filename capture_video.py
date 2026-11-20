import argparse
import csv
import os
import time
import webbrowser

import qscript

PATH_TO_VIDEO_FOLDER = "./video"
INPUT_CSV = "input.csv"


def preview_video(url: str, view_seconds: int = 5):
    webbrowser.open(url)
    if view_seconds > 0:
        time.sleep(view_seconds)
    else:
        input("Press Enter to close the preview and continue...")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", help="Web service URL, e.g. http://127.0.0.1:8000")
    parser.add_argument(
        "--view-seconds",
        type=int,
        default=5,
        help="Preview duration in seconds (0 = wait for Enter)",
    )
    parser.add_argument(
        "--auto-zoom",
        action="store_true",
        help="Trigger programmatic window.__zoomAt(x, y) zoom events during recording",
    )
    parser.add_argument(
        "--zoom-factor",
        type=float,
        default=qscript.ZOOM_FACTOR,
        help="Zoom multiplier stored in the sidecar (default: 2.0)",
    )
    parser.add_argument(
        "--ramp-sec",
        type=float,
        default=qscript.ZOOM_RAMP_SEC,
        help="Zoom-in/zoom-out ramp duration in seconds (default: 0.5)",
    )
    parser.add_argument(
        "--wait-processing",
        action="store_true",
        help="Wait for server-side Docker zoom post-processing and preview the zoomed video",
    )
    parser.add_argument(
        "--apply-zoom-local",
        action="store_true",
        help="Run apply_zoom.py locally after recording when a sidecar exists",
    )
    parser.add_argument(
        "--ffmpeg",
        default=os.environ.get("FFMPEG_PATH", "ffmpeg"),
        help="Path to local ffmpeg binary (used with --apply-zoom-local)",
    )
    args = parser.parse_args()

    csv_path = INPUT_CSV
    if not os.path.isfile(csv_path):
        print("Input CSV not found:", csv_path)
        return

    qscript.PATH_TO_VIDEO_FOLDER = PATH_TO_VIDEO_FOLDER

    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f, delimiter=";", quotechar='"')
        for row in reader:
            company_name = row.get("Company Name", "").strip()
            city = row.get("City", "").strip()
            url = row.get("URL", "").strip()

            print("Processing:", company_name, "->", city)
            video_path = qscript.CaptureVideo_Magento(
                url,
                company_name,
                row.get("Language", "").strip(),
                row.get("Country", "").strip(),
                row.get("Postcode", "").strip(),
                city,
                row.get("Street", "").strip(),
                row.get("HouseNo", "").strip(),
                auto_zoom=args.auto_zoom,
                zoom_factor=args.zoom_factor,
                ramp_sec=args.ramp_sec,
            )

            if not video_path:
                print("Failed to record for:", company_name)
                continue

            print("Saved video:", video_path)

            if args.apply_zoom_local:
                import apply_zoom

                sidecar = apply_zoom.find_sidecar_path(video_path)
                if sidecar:
                    res = apply_zoom.process_video(
                        video_path,
                        sidecar_path=sidecar,
                        ffmpeg_bin=args.ffmpeg,
                    )
                    print("Locally zoomed video:", res["output"])

            if args.server:
                try:
                    from video_client import UploadVideo

                    video_url = UploadVideo(
                        args.server,
                        video_path,
                        company_name,
                        wait_for_processing=args.wait_processing,
                    )
                    print("Uploaded:", video_url)
                    preview_video(video_url, args.view_seconds)
                except Exception as e:
                    print(f"Upload/preview failed for {company_name}: {e}")


if __name__ == "__main__":
    main()
