import os
import time
from urllib.parse import quote

import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, g, jsonify, request, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "db"),
    "port": int(os.environ.get("DB_PORT", 5432)),
    "dbname": os.environ.get("DB_NAME", "videos"),
    "user": os.environ.get("DB_USER", "video_user"),
    "password": os.environ.get("DB_PASSWORD", "video_pass"),
}


def wait_for_db(max_retries=30, delay=2):
    for attempt in range(1, max_retries + 1):
        try:
            conn = psycopg2.connect(**DB_CONFIG)
            conn.close()
            return
        except psycopg2.OperationalError:
            if attempt == max_retries:
                raise
            time.sleep(delay)


wait_for_db()


def get_db():
    if "db" not in g:
        try:
            g.db = psycopg2.connect(**DB_CONFIG)
            g.db.autocommit = False
            with g.db.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS videos (
                        id SERIAL PRIMARY KEY,
                        company_name TEXT NOT NULL UNIQUE,
                        file_path TEXT NOT NULL,
                        video_url TEXT NOT NULL,
                        created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMP NOT NULL DEFAULT NOW()
                    )
                """)
            g.db.commit()
        except psycopg2.OperationalError as e:
            raise psycopg2.OperationalError(
                f"Database unavailable: {e}"
            ) from e
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
    with db.cursor() as cur:
        cur.execute("""
            INSERT INTO videos (company_name, file_path, video_url, updated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (company_name) DO UPDATE SET
                file_path = EXCLUDED.file_path,
                video_url = EXCLUDED.video_url,
                updated_at = NOW()
        """, (company_name, file_path, video_url))
    db.commit()


def get_video(company_name: str):
    db = get_db()
    with db.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM videos WHERE company_name = %s", (company_name,))
        return cur.fetchone()


@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "service": "video-web-service",
        "status": "ok",
        "endpoints": [
            "POST /upload",
            "GET  /get_video_url?company_name=...",
            "GET  /media/<filename>",
            "GET  /videos",
            "GET  /health",
        ],
    })


@app.route("/health", methods=["GET"])
def health():
    try:
        db = get_db()
        with db.cursor() as cur:
            cur.execute("SELECT 1")
        return jsonify({"status": "ok", "db": "ok"}), 200
    except psycopg2.OperationalError as e:
        return jsonify({"status": "degraded", "db": "unavailable", "detail": str(e)}), 503

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
    db = get_db()
    with db.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM videos ORDER BY id DESC")
        rows = cur.fetchall()
    return jsonify([dict(r) for r in rows])


@app.errorhandler(psycopg2.OperationalError)
def handle_db_error(e):
    return jsonify({"error": "Database unavailable", "detail": str(e)}), 503