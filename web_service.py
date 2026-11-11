import os
import sqlite3
from datetime import datetime
from urllib.parse import quote

from flask import Flask, g, jsonify, request, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
DB_PATH = os.path.join(BASE_DIR, "videos.db")

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=10.0)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA busy_timeout=5000")
        g.db.execute(
            """
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_name TEXT NOT NULL UNIQUE,
                file_path TEXT NOT NULL,
                video_url TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        g.db.commit()
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def safe_filename(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name).strip("_") or "video"


def upsert_video(company_name: str, file_path: str, video_url: str):
    db = get_db()
    db.execute(
        """
        INSERT INTO videos (company_name, file_path, video_url, updated_at)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(company_name) DO UPDATE SET
            file_path = excluded.file_path,
            video_url = excluded.video_url,
            updated_at = datetime('now')
        """,
        (company_name, file_path, video_url),
    )
    db.commit()


def get_video(company_name: str):
    return get_db().execute(
        "SELECT * FROM videos WHERE company_name = ?", (company_name,)
    ).fetchone()


@app.route("/upload", methods=["POST"])
def upload_video():
    if "file" not in request.files:
        return jsonify({"error": "no file part"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "empty filename"}), 400

    company_name = request.form.get("company_name", "").strip()
    if not company_name:
        return jsonify({"error": "company_name is required"}), 400

    ext = os.path.splitext(file.filename)[1].lower() or ".webm"
    filename = safe_filename(company_name) + ext
    file_path = os.path.join(UPLOAD_FOLDER, filename)
    file.save(file_path)

    video_url = request.host_url.rstrip("/") + "/media/" + quote(filename)
    upsert_video(company_name, file_path, video_url)

    return jsonify({"company_name": company_name, "video_url": video_url}), 200


@app.route("/get_video_url", methods=["GET"])
def get_video_url():
    company_name = request.args.get("company_name", "").strip()
    if not company_name:
        return jsonify({"error": "company_name is required"}), 400

    row = get_video(company_name)
    if row is None:
        return jsonify({"error": "not found"}), 404

    return jsonify({"company_name": row["company_name"], "video_url": row["video_url"]})


@app.route("/media/<path:filename>", methods=["GET"])
def serve_media(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


@app.route("/videos", methods=["GET"])
def list_videos():
    rows = get_db().execute("SELECT * FROM videos ORDER BY id DESC").fetchall()
    return jsonify([dict(r) for r in rows])


if __name__ == "__main__":
    host = os.environ.get("FLASK_HOST", "0.0.0.0")
    port = int(os.environ.get("FLASK_PORT", "8000"))
    app.run(host=host, port=port, threaded=True)