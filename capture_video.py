import csv
import os
import sys
import qscript

PATH_TO_VIDEO_FOLDER = "./video"
INPUT_CSV = "input.csv"

def main():
    csv_path = sys.argv[1] if len(sys.argv) > 1 else INPUT_CSV
    if not os.path.isfile(csv_path):
        print("Input CSV not found:", csv_path)
        return

    qscript.PATH_TO_VIDEO_FOLDER = PATH_TO_VIDEO_FOLDER

    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f, delimiter=";", quotechar='"')
        for row in reader:
            url = row.get("URL", "").strip()
            company_name = row.get("Company Name", "").strip()
            language = row.get("Language", "").strip()
            country = row.get("Country", "").strip()
            postcode = row.get("Postcode", "").strip()
            city = row.get("City", "").strip()
            street = row.get("Street", "").strip()
            house_no = row.get("HouseNo", "").strip()

            print("Processing:", company_name, "->", city)
            video_path = qscript.CaptureVideo_Magento(
                url,
                company_name,
                language,
                country,
                postcode,
                city,
                street,
                house_no,
            )
            if video_path:
                print("Saved video:", video_path)
            else:
                print("Failed for:", company_name)

if __name__ == "__main__":
    main()