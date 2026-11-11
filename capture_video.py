import argparse
import csv
import os
import sys
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
    parser.add_argument("--view-seconds", type=int, default=5,
                        help="Preview duration in seconds (0 = wait for Enter)")
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
            )

            if not video_path:
                print("Failed to record for:", company_name)
                continue

            print("Saved video:", video_path)

            if args.server:
                try:
                    from video_client import UploadVideo
                    video_url = UploadVideo(args.server, video_path, company_name)
                    print("Uploaded:", video_url)
                    preview_video(video_url, args.view_seconds)
                except Exception as e:
                    print(f"Upload/preview failed for {company_name}: {e}")


if __name__ == "__main__":
    main()