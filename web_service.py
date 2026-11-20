from __future__ import annotations

import json
import os
import shutil
import time
from typing import Any, Optional
from urllib.parse import quote

import psycopg2
from psycopg2.extras import Json, RealDictCursor
from flask import Flask, g, jsonify, request, send_from_directory

import apply_zoom

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER") or os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

FFMPEG_PATH = os.environ.get("FFMPEG_PATH", "ffmpeg")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "db"),
    "port": int(os.environ.get("DB_PORT", 5432)),
    "dbname": os.environ.get("DB_NAME", "videos"),
    "user": os.environ.get("DB_USER", "video_user"),
    "password": os.environ.get("DB_PASSWORD", "video_pass"),
}

_SCHEMA_LOCK_ID = 739182645


def init_db(max_retries: int = 30, delay: float = 2.0) -> None:
    """Initialize PostgreSQL tables and idempotently migrate Task 1 schema."""
    for attempt in range(1, max_retries + 1):
        try:
            conn = psycopg2.connect(**DB_CONFIG)
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_xact_lock(%s)", (_SCHEMA_LOCK_ID,))
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS videos (
                            id SERIAL PRIMARY KEY,
                            company_name TEXT NOT NULL UNIQUE,
                            file_path TEXT NOT NULL,
                            video_url TEXT NOT NULL,
                            sidecar_path TEXT,
                            zoom_events JSONB,
                            processed_file_path TEXT,
                            processed_video_url TEXT,
                            processing_status TEXT NOT NULL DEFAULT 'none',
                            created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                            updated_at TIMESTAMP NOT NULL DEFAULT NOW()
                        )
                        """
                    )
                    # Backwards-compatible column additions for existing Task 1 databases.
                    cur.execute("ALTER TABLE videos ADD COLUMN IF NOT EXISTS sidecar_path TEXT")
                    cur.execute("ALTER TABLE videos ADD COLUMN IF NOT EXISTS zoom_events JSONB")
                    cur.execute("ALTER TABLE videos ADD COLUMN IF NOT EXISTS processed_file_path TEXT")
                    cur.execute("ALTER TABLE videos ADD COLUMN IF NOT EXISTS processed_video_url TEXT")
                    cur.execute(
                        "ALTER TABLE videos ADD COLUMN IF NOT EXISTS processing_status TEXT NOT NULL DEFAULT 'none'"
                    )
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS jobs (
                            id SERIAL PRIMARY KEY,
                            video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                            company_name TEXT NOT NULL,
                            status TEXT NOT NULL DEFAULT 'pending',
                            input_path TEXT NOT NULL,
                            sidecar_path TEXT,
                            zoom_events JSONB,
                            output_path TEXT,
                            processed_video_url TEXT,
                            base_url TEXT NOT NULL DEFAULT '',
                            error TEXT,
                            created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                            updated_at TIMESTAMP NOT NULL DEFAULT NOW()
                        )
                        """
                    )
                    cur.execute(
                        "CREATE INDEX IF NOT EXISTS idx_jobs_status_id ON jobs (status, id)"
                    )
                conn.commit()
            finally:
                conn.close()
            return
        except psycopg2.OperationalError:
            if attempt == max_retries:
                raise
            time.sleep(delay)


if os.environ.get("SKIP_INIT_DB", "").lower() not in ("1", "true", "yes"):
    init_db()


def get_db():
    if "db" not in g:
        try:
            g.db = psycopg2.connect(**DB_CONFIG)
            g.db.autocommit = False
        except psycopg2.OperationalError as e:
            raise psycopg2.OperationalError(f"Database unavailable: {e}") from e
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def safe_filename(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name).strip("_.") or "video"


def _sidecar_to_json_dict(sidecar: apply_zoom.ZoomSidecar) -> dict[str, Any]:
    return {
        "viewport": {
            "width": sidecar.viewport_width,
            "height": sidecar.viewport_height,
        },
        "fps": sidecar.fps,
        "zoom_factor": sidecar.zoom_factor,
        "ramp_sec": sidecar.ramp_sec,
        "events": [
            {"t": ev.t, "x": ev.x, "y": ev.y, "type": ev.type}
            for ev in sidecar.events
        ],
    }


def _extract_zoom_payload_from_request() -> Optional[dict[str, Any]]:
    """Extract and validate optional zoom sidecar JSON from multipart file, form field, or JSON body."""
    raw_obj: Any = None
    if "sidecar" in request.files and request.files["sidecar"].filename:
        raw_bytes = request.files["sidecar"].read()
        try:
            raw_obj = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid sidecar JSON file: {exc}") from exc
    else:
        for field in ("zoom_events", "zoom_config", "sidecar"):
            raw_str = request.form.get(field)
            if raw_str is not None and raw_str.strip():
                try:
                    raw_obj = json.loads(raw_str)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Invalid JSON in '{field}': {exc}") from exc
                break

    if raw_obj is None and request.is_json:
        body = request.get_json(silent=True) or {}
        if isinstance(body, dict):
            if "zoom_events" in body and body["zoom_events"] is not None:
                raw_obj = body["zoom_events"]
            elif "sidecar" in body and body["sidecar"] is not None:
                raw_obj = body["sidecar"]
            elif "events" in body and body["events"] is not None:
                raw_obj = body

    if raw_obj is None:
        return None

    parsed = apply_zoom.parse_sidecar_data(raw_obj)
    return _sidecar_to_json_dict(parsed)


def _is_truthy(val: Optional[str], default: bool = True) -> bool:
    if val is None or val == "":
        return default
    return str(val).strip().lower() not in ("0", "false", "no", "off")


def upsert_video(
    company_name: str,
    file_path: str,
    video_url: str,
    *,
    sidecar_path: Optional[str] = None,
    zoom_events: Optional[dict[str, Any]] = None,
    processing_status: str = "none",
) -> dict[str, Any]:
    db = get_db()
    with db.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO videos (
                company_name, file_path, video_url,
                sidecar_path, zoom_events,
                processed_file_path, processed_video_url,
                processing_status, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, NULL, NULL, %s, NOW())
            ON CONFLICT (company_name) DO UPDATE SET
                file_path = EXCLUDED.file_path,
                video_url = EXCLUDED.video_url,
                sidecar_path = EXCLUDED.sidecar_path,
                zoom_events = EXCLUDED.zoom_events,
                processed_file_path = NULL,
                processed_video_url = NULL,
                processing_status = EXCLUDED.processing_status,
                updated_at = NOW()
            RETURNING *
            """,
            (
                company_name,
                file_path,
                video_url,
                sidecar_path,
                Json(zoom_events) if zoom_events is not None else None,
                processing_status,
            ),
        )
        row = cur.fetchone()
    db.commit()
    return dict(row)


def create_job(
    video_id: int,
    company_name: str,
    input_path: str,
    *,
    sidecar_path: Optional[str] = None,
    zoom_events: Optional[dict[str, Any]] = None,
    base_url: str = "",
    status: str = "pending",
) -> dict[str, Any]:
    db = get_db()
    with db.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO jobs (
                video_id, company_name, status, input_path,
                sidecar_path, zoom_events, base_url, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            RETURNING *
            """,
            (
                video_id,
                company_name,
                status,
                input_path,
                sidecar_path,
                Json(zoom_events) if zoom_events is not None else None,
                base_url.rstrip("/"),
            ),
        )
        row = cur.fetchone()
    db.commit()
    return dict(row)


def get_video(company_name: str) -> Optional[dict[str, Any]]:
    db = get_db()
    with db.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM videos WHERE company_name = %s", (company_name,))
        row = cur.fetchone()
        return dict(row) if row else None


def get_video_by_id(video_id: int) -> Optional[dict[str, Any]]:
    db = get_db()
    with db.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM videos WHERE id = %s", (video_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def get_job(job_id: int) -> Optional[dict[str, Any]]:
    db = get_db()
    with db.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM jobs WHERE id = %s", (job_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def claim_next_pending_job(conn) -> Optional[dict[str, Any]]:
    """Atomically claim the oldest pending job for worker processing."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            WITH next_job AS (
                SELECT id FROM jobs
                WHERE status = 'pending'
                ORDER BY id ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE jobs j
            SET status = 'processing', error = NULL, updated_at = NOW()
            FROM next_job
            WHERE j.id = next_job.id
            RETURNING j.*
            """
        )
        job = cur.fetchone()
        if job is not None:
            cur.execute(
                "UPDATE videos SET processing_status = 'processing', updated_at = NOW() WHERE id = %s",
                (job["video_id"],),
            )
    conn.commit()
    return dict(job) if job else None


def claim_job_by_id(conn, job_id: int, force: bool = False) -> Optional[dict[str, Any]]:
    """Atomically transition a specific job to 'processing'."""
    allowed = ("pending", "created", "failed", "completed") if force else ("pending", "created", "failed")
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            UPDATE jobs
            SET status = 'processing', error = NULL, updated_at = NOW()
            WHERE id = %s AND status = ANY(%s)
            RETURNING *
            """,
            (job_id, list(allowed)),
        )
        job = cur.fetchone()
        if job is not None:
            cur.execute(
                "UPDATE videos SET processing_status = 'processing', updated_at = NOW() WHERE id = %s",
                (job["video_id"],),
            )
    conn.commit()
    return dict(job) if job else None


def process_claimed_job(
    conn,
    job: dict[str, Any],
    *,
    ffmpeg_bin: Optional[str] = None,
    upload_folder: Optional[str] = None,
) -> dict[str, Any]:
    """Run ``apply_zoom.process_video`` for a claimed job and store results in PostgreSQL."""
    target_dir = upload_folder or app.config["UPLOAD_FOLDER"]
    os.makedirs(target_dir, exist_ok=True)

    output_filename = f"{safe_filename(job['company_name'])}_zoomed.webm"
    output_path = os.path.join(target_dir, output_filename)

    sidecar_data = job.get("zoom_events")
    sidecar_path = job.get("sidecar_path")
    if sidecar_path and not os.path.isfile(sidecar_path):
        sidecar_path = None

    try:
        apply_zoom.process_video(
            video_path=job["input_path"],
            output_path=output_path,
            sidecar_path=sidecar_path if sidecar_data is None else None,
            sidecar_data=sidecar_data,
            ffmpeg_bin=ffmpeg_bin or FFMPEG_PATH,
        )

        base_url = (job.get("base_url") or PUBLIC_BASE_URL).rstrip("/")
        media_rel = "/media/" + quote(output_filename)
        processed_video_url = (base_url + media_rel) if base_url else media_rel

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE jobs
                SET status = 'completed',
                    output_path = %s,
                    processed_video_url = %s,
                    error = NULL,
                    updated_at = NOW()
                WHERE id = %s
                RETURNING *
                """,
                (output_path, processed_video_url, job["id"]),
            )
            updated = cur.fetchone()
            cur.execute(
                """
                UPDATE videos
                SET processed_file_path = %s,
                    processed_video_url = %s,
                    processing_status = 'completed',
                    updated_at = NOW()
                WHERE id = %s
                """,
                (output_path, processed_video_url, job["video_id"]),
            )
        conn.commit()
        return dict(updated)
    except Exception as exc:
        err_msg = str(exc)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE jobs
                SET status = 'failed',
                    error = %s,
                    updated_at = NOW()
                WHERE id = %s
                RETURNING *
                """,
                (err_msg, job["id"]),
            )
            failed = cur.fetchone()
            cur.execute(
                """
                UPDATE videos
                SET processing_status = 'failed',
                    updated_at = NOW()
                WHERE id = %s
                """,
                (job["video_id"],),
            )
        conn.commit()
        return dict(failed)


def _check_ffmpeg_available() -> bool:
    if os.path.isabs(FFMPEG_PATH):
        return os.path.isfile(FFMPEG_PATH) and os.access(FFMPEG_PATH, os.X_OK)
    return shutil.which(FFMPEG_PATH) is not None


@app.route("/", methods=["GET"])
def index():
    return jsonify(
        {
            "service": "video-web-service",
            "status": "ok",
            "endpoints": [
                "POST /upload",
                "GET  /get_video_url?company_name=...",
                "GET  /media/<filename>",
                "GET  /videos",
                "GET  /jobs",
                "POST /jobs",
                "GET  /jobs/<id>",
                "POST /jobs/<id>/process",
                "GET  /health",
            ],
        }
    )


@app.route("/health", methods=["GET"])
def health():
    upload_dir = app.config["UPLOAD_FOLDER"]
    uploads_writable = os.access(upload_dir, os.W_OK)
    ffmpeg_ok = _check_ffmpeg_available()
    try:
        db = get_db()
        with db.cursor() as cur:
            cur.execute("SELECT 1")
    except psycopg2.OperationalError as e:
        return (
            jsonify(
                {
                    "status": "degraded",
                    "db": "unavailable",
                    "uploads": "ok" if uploads_writable else "not_writable",
                    "ffmpeg": "ok" if ffmpeg_ok else "missing",
                    "detail": str(e),
                }
            ),
            503,
        )

    if not uploads_writable or not ffmpeg_ok:
        return (
            jsonify(
                {
                    "status": "degraded",
                    "db": "ok",
                    "uploads": "ok" if uploads_writable else "not_writable",
                    "ffmpeg": "ok" if ffmpeg_ok else "missing",
                }
            ),
            503,
        )

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

    try:
        zoom_payload = _extract_zoom_payload_from_request()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    upload_dir = app.config["UPLOAD_FOLDER"]
    os.makedirs(upload_dir, exist_ok=True)

    raw_name = os.path.basename(file.filename.replace("\\", "/"))
    ext = os.path.splitext(raw_name)[1].lower()
    if not ext or ext == ".":
        ext = ".webm"
    safe_base = safe_filename(company_name)
    filename = safe_base + ext
    file_path = os.path.join(upload_dir, filename)
    file.save(file_path)

    base_url = request.host_url.rstrip("/")
    video_url = base_url + "/media/" + quote(filename)

    sidecar_filename = safe_base + ".zoom.json"
    sidecar_path = os.path.join(upload_dir, sidecar_filename)
    sidecar_url: Optional[str] = None

    auto_process = _is_truthy(request.form.get("auto_process"), default=True)

    if zoom_payload is not None:
        with open(sidecar_path, "w", encoding="utf-8") as sf:
            json.dump(zoom_payload, sf, indent=2)
        sidecar_url = base_url + "/media/" + quote(sidecar_filename)
        initial_status = "pending" if auto_process else "created"
        video_row = upsert_video(
            company_name,
            file_path,
            video_url,
            sidecar_path=sidecar_path,
            zoom_events=zoom_payload,
            processing_status=initial_status,
        )
        job_row = create_job(
            video_row["id"],
            company_name,
            file_path,
            sidecar_path=sidecar_path,
            zoom_events=zoom_payload,
            base_url=base_url,
            status=initial_status,
        )
        return (
            jsonify(
                {
                    "company_name": company_name,
                    "video_id": video_row["id"],
                    "video_url": video_url,
                    "sidecar_url": sidecar_url,
                    "job_id": job_row["id"],
                    "processing_status": job_row["status"],
                }
            ),
            200,
        )

    # No sidecar uploaded: remove any stale sidecar so normal uploads remain unaffected.
    if os.path.exists(sidecar_path):
        try:
            os.remove(sidecar_path)
        except OSError:
            pass

    video_row = upsert_video(
        company_name,
        file_path,
        video_url,
        sidecar_path=None,
        zoom_events=None,
        processing_status="none",
    )
    return (
        jsonify(
            {
                "company_name": company_name,
                "video_id": video_row["id"],
                "video_url": video_url,
                "job_id": None,
                "processing_status": "none",
            }
        ),
        200,
    )


@app.route("/get_video_url", methods=["GET"])
def get_video_url():
    company_name = request.args.get("company_name", "").strip()
    if not company_name:
        return jsonify({"error": "company_name is required"}), 400

    row = get_video(company_name)
    if row is None:
        return jsonify({"error": "not found"}), 404

    return jsonify(
        {
            "company_name": row["company_name"],
            "video_url": row["video_url"],
            "processed_video_url": row.get("processed_video_url"),
            "processing_status": row.get("processing_status", "none"),
        }
    )


@app.route("/media/<path:filename>", methods=["GET"])
def serve_media(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


@app.route("/videos", methods=["GET"])
def list_videos():
    db = get_db()
    with db.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM videos ORDER BY id DESC")
        rows = cur.fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/jobs", methods=["GET"])
def list_jobs():
    status_filter = request.args.get("status", "").strip()
    db = get_db()
    with db.cursor(cursor_factory=RealDictCursor) as cur:
        if status_filter:
            cur.execute(
                "SELECT * FROM jobs WHERE status = %s ORDER BY id DESC",
                (status_filter,),
            )
        else:
            cur.execute("SELECT * FROM jobs ORDER BY id DESC")
        rows = cur.fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/jobs", methods=["POST"])
def create_processing_job():
    body = request.get_json(silent=True) or {}
    company_name = (request.form.get("company_name") or body.get("company_name") or "").strip()
    video_id = request.form.get("video_id") or body.get("video_id")

    video_row = None
    if video_id is not None:
        try:
            video_row = get_video_by_id(int(video_id))
        except (TypeError, ValueError):
            return jsonify({"error": "invalid video_id"}), 400
    elif company_name:
        video_row = get_video(company_name)
    else:
        return jsonify({"error": "company_name or video_id is required"}), 400

    if video_row is None:
        return jsonify({"error": "video not found"}), 404

    try:
        zoom_payload = _extract_zoom_payload_from_request()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    upload_dir = app.config["UPLOAD_FOLDER"]
    sidecar_path = video_row.get("sidecar_path")
    if zoom_payload is not None:
        sidecar_path = os.path.join(upload_dir, f"{safe_filename(video_row['company_name'])}.zoom.json")
        with open(sidecar_path, "w", encoding="utf-8") as sf:
            json.dump(zoom_payload, sf, indent=2)
    else:
        zoom_payload = video_row.get("zoom_events")

    auto_process_raw = request.form.get("auto_process")
    if auto_process_raw is None and isinstance(body, dict) and "auto_process" in body:
        auto_process = bool(body["auto_process"])
    else:
        auto_process = _is_truthy(auto_process_raw, default=True)

    initial_status = "pending" if auto_process else "created"
    base_url = request.host_url.rstrip("/")

    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE videos
            SET sidecar_path = %s,
                zoom_events = %s,
                processing_status = %s,
                updated_at = NOW()
            WHERE id = %s
            """,
            (
                sidecar_path,
                Json(zoom_payload) if zoom_payload is not None else None,
                initial_status,
                video_row["id"],
            ),
        )
    db.commit()

    job_row = create_job(
        video_row["id"],
        video_row["company_name"],
        video_row["file_path"],
        sidecar_path=sidecar_path,
        zoom_events=zoom_payload,
        base_url=base_url,
        status=initial_status,
    )
    return jsonify(job_row), 201


@app.route("/jobs/<int:job_id>", methods=["GET"])
def get_job_status(job_id: int):
    job = get_job(job_id)
    if job is None:
        return jsonify({"error": "job not found"}), 404
    return jsonify(job), 200


@app.route("/jobs/<int:job_id>/process", methods=["POST"])
def trigger_job_process(job_id: int):
    existing = get_job(job_id)
    if existing is None:
        return jsonify({"error": "job not found"}), 404

    force = _is_truthy(request.args.get("force"), default=False)

    try:
        zoom_payload = _extract_zoom_payload_from_request()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    db = get_db()
    if zoom_payload is not None:
        upload_dir = app.config["UPLOAD_FOLDER"]
        sidecar_path = os.path.join(upload_dir, f"{safe_filename(existing['company_name'])}.zoom.json")
        with open(sidecar_path, "w", encoding="utf-8") as sf:
            json.dump(zoom_payload, sf, indent=2)
        with db.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                SET sidecar_path = %s, zoom_events = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (sidecar_path, Json(zoom_payload), job_id),
            )
            cur.execute(
                """
                UPDATE videos
                SET sidecar_path = %s, zoom_events = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (sidecar_path, Json(zoom_payload), existing["video_id"]),
            )
        db.commit()
        force = True

    if existing["status"] == "completed" and not force:
        return jsonify(existing), 200

    claimed = claim_job_by_id(db, job_id, force=force)
    if claimed is None:
        refreshed = get_job(job_id)
        if refreshed and refreshed["status"] == "completed" and not force:
            return jsonify(refreshed), 200
        return jsonify({"error": "job is already being processed", "job": refreshed}), 409

    if not claimed.get("base_url"):
        claimed["base_url"] = request.host_url.rstrip("/")

    result = process_claimed_job(db, claimed)
    status_code = 200 if result["status"] == "completed" else 500
    return jsonify(result), status_code


@app.errorhandler(psycopg2.OperationalError)
def handle_db_error(e):
    return jsonify({"error": "Database unavailable", "detail": str(e)}), 503
