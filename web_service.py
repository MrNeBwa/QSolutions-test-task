import json
import os
import shutil
import time
from urllib.parse import quote

import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, g, jsonify, request, send_from_directory

import apply_zoom

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


def init_db(max_retries=30, delay=2):
    for attempt in range(1, max_retries + 1):
        try:
            conn = psycopg2.connect(**DB_CONFIG)
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS videos (
                            id SERIAL PRIMARY KEY,
                            company_name TEXT NOT NULL UNIQUE,
                            file_path TEXT NOT NULL,
                            video_url TEXT NOT NULL,
                            processed_video_url TEXT,
                            created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                            updated_at TIMESTAMP NOT NULL DEFAULT NOW()
                        )
                    """)
                    cur.execute(
                        "ALTER TABLE videos ADD COLUMN IF NOT EXISTS processed_video_url TEXT"
                    )
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS jobs (
                            id SERIAL PRIMARY KEY,
                            video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                            company_name TEXT NOT NULL,
                            status TEXT NOT NULL DEFAULT 'pending',
                            input_path TEXT NOT NULL,
                            sidecar_path TEXT,
                            output_path TEXT,
                            processed_video_url TEXT,
                            base_url TEXT NOT NULL DEFAULT '',
                            error TEXT,
                            created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                            updated_at TIMESTAMP NOT NULL DEFAULT NOW()
                        )
                    """)
                conn.commit()
            finally:
                conn.close()
            return
        except (psycopg2.OperationalError, psycopg2.IntegrityError):
            if attempt == max_retries:
                raise
            time.sleep(delay)


init_db()


def get_db():
    if "db" not in g:
        try:
            g.db = psycopg2.connect(**DB_CONFIG)
            g.db.autocommit = False
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
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name).strip("_.") or "video"


def upsert_video(company_name: str, file_path: str, video_url: str):
    db = get_db()
    with db.cursor() as cur:
        cur.execute("""
            INSERT INTO videos (company_name, file_path, video_url, processed_video_url, updated_at)
            VALUES (%s, %s, %s, NULL, NOW())
            ON CONFLICT (company_name) DO UPDATE SET
                file_path = EXCLUDED.file_path,
                video_url = EXCLUDED.video_url,
                processed_video_url = NULL,
                updated_at = NOW()
            RETURNING id
        """, (company_name, file_path, video_url))
        video_id = cur.fetchone()[0]
    db.commit()
    return video_id


def get_video(company_name: str):
    db = get_db()
    with db.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM videos WHERE company_name = %s", (company_name,))
        return cur.fetchone()


def create_job(video_id: int, company_name: str, input_path: str, sidecar_path: str, base_url: str):
    db = get_db()
    with db.cursor() as cur:
        cur.execute("""
            INSERT INTO jobs (video_id, company_name, status, input_path, sidecar_path, base_url)
            VALUES (%s, %s, 'pending', %s, %s, %s)
            RETURNING id
        """, (video_id, company_name, input_path, sidecar_path, base_url))
        job_id = cur.fetchone()[0]
    db.commit()
    return job_id


def run_job(conn, job: dict):
    out_filename = safe_filename(job["company_name"]) + "_zoomed.webm"
    out_path = os.path.join(UPLOAD_FOLDER, out_filename)
    try:
        apply_zoom.process_video(
            job["input_path"],
            output_path=out_path,
            sidecar_path=job.get("sidecar_path"),
        )
        base = (job.get("base_url") or "").rstrip("/")
        processed_url = base + "/media/" + quote(out_filename)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                UPDATE jobs
                SET status = 'completed', output_path = %s, processed_video_url = %s,
                    error = NULL, updated_at = NOW()
                WHERE id = %s
                RETURNING *
            """, (out_path, processed_url, job["id"]))
            updated = dict(cur.fetchone())
            cur.execute("""
                UPDATE videos
                SET processed_video_url = %s, updated_at = NOW()
                WHERE id = %s
            """, (processed_url, job["video_id"]))
        conn.commit()
        return updated
    except Exception as e:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                UPDATE jobs
                SET status = 'failed', error = %s, updated_at = NOW()
                WHERE id = %s
                RETURNING *
            """, (str(e), job["id"]))
            failed = dict(cur.fetchone())
        conn.commit()
        return failed


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
            "GET  /jobs",
            "GET  /jobs/<id>",
            "POST /jobs/<id>/process",
            "GET  /health",
        ],
    })


@app.route("/health", methods=["GET"])
def health():
    uploads_writable = os.access(UPLOAD_FOLDER, os.W_OK)
    ffmpeg_ok = shutil.which("ffmpeg") is not None
    try:
        db = get_db()
        with db.cursor() as cur:
            cur.execute("SELECT 1")
    except psycopg2.OperationalError as e:
        return jsonify({
            "status": "degraded",
            "db": "unavailable",
            "uploads": "ok" if uploads_writable else "not_writable",
            "ffmpeg": "ok" if ffmpeg_ok else "missing",
            "detail": str(e),
        }), 503

    if not uploads_writable or not ffmpeg_ok:
        return jsonify({
            "status": "degraded",
            "db": "ok",
            "uploads": "ok" if uploads_writable else "not_writable",
            "ffmpeg": "ok" if ffmpeg_ok else "missing",
        }), 503

    return jsonify({"status": "ok", "db": "ok", "uploads": "ok", "ffmpeg": "ok"}), 200


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

    raw_name = os.path.basename(file.filename.replace("\\", "/"))
    ext = os.path.splitext(raw_name)[1].lower()
    if not ext or ext == ".":
        ext = ".webm"
    safe_base = safe_filename(company_name)
    filename = safe_base + ext
    file_path = os.path.join(UPLOAD_FOLDER, filename)
    file.save(file_path)

    base_url = request.host_url.rstrip("/")
    video_url = base_url + "/media/" + quote(filename)
    video_id = upsert_video(company_name, file_path, video_url)

    sidecar_path = os.path.join(UPLOAD_FOLDER, safe_base + ".zoom.json")
    has_sidecar = False

    if "sidecar" in request.files and request.files["sidecar"].filename:
        raw_sidecar = request.files["sidecar"].read()
        try:
            apply_zoom.parse_sidecar(json.loads(raw_sidecar.decode("utf-8")))
        except Exception as e:
            return jsonify({"error": f"invalid sidecar json: {e}"}), 400
        with open(sidecar_path, "wb") as sf:
            sf.write(raw_sidecar)
        has_sidecar = True
    elif request.form.get("zoom_events"):
        try:
            parsed = json.loads(request.form["zoom_events"])
            apply_zoom.parse_sidecar(parsed)
        except Exception as e:
            return jsonify({"error": f"invalid zoom_events json: {e}"}), 400
        with open(sidecar_path, "w", encoding="utf-8") as sf:
            json.dump(parsed, sf)
        has_sidecar = True
    elif os.path.exists(sidecar_path):
        os.remove(sidecar_path)

    resp = {"company_name": company_name, "video_url": video_url}
    if has_sidecar:
        resp["job_id"] = create_job(video_id, company_name, file_path, sidecar_path, base_url)
    return jsonify(resp), 200


@app.route("/get_video_url", methods=["GET"])
def get_video_url():
    company_name = request.args.get("company_name", "").strip()
    if not company_name:
        return jsonify({"error": "company_name is required"}), 400

    row = get_video(company_name)
    if row is None:
        return jsonify({"error": "not found"}), 404

    return jsonify({
        "company_name": row["company_name"],
        "video_url": row["video_url"],
        "processed_video_url": row.get("processed_video_url"),
    })


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


@app.route("/jobs", methods=["GET"])
def list_jobs():
    db = get_db()
    with db.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM jobs ORDER BY id DESC")
        rows = cur.fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/jobs/<int:job_id>", methods=["GET"])
def get_job_status(job_id: int):
    db = get_db()
    with db.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM jobs WHERE id = %s", (job_id,))
        row = cur.fetchone()
    if row is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(dict(row))


@app.route("/jobs/<int:job_id>/process", methods=["POST"])
def process_job(job_id: int):
    db = get_db()
    with db.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM jobs WHERE id = %s", (job_id,))
        job = cur.fetchone()
        if job is None:
            return jsonify({"error": "not found"}), 404
        if job["status"] == "processing":
            return jsonify({"error": "already processing"}), 409
        cur.execute(
            "UPDATE jobs SET status = 'processing', updated_at = NOW() WHERE id = %s",
            (job_id,),
        )
    db.commit()

    result = run_job(db, dict(job))
    code = 200 if result["status"] == "completed" else 500
    return jsonify(result), code


@app.errorhandler(psycopg2.OperationalError)
def handle_db_error(e):
    return jsonify({"error": "Database unavailable", "detail": str(e)}), 503
