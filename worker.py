from __future__ import annotations

import os
import signal
import time

import psycopg2

os.environ.setdefault("SKIP_INIT_DB", "1")
import web_service  # noqa: E402

POLL_INTERVAL = float(os.environ.get("WORKER_POLL_INTERVAL", "1.0"))

_running = True


def _handle_signal(signum, frame):
    global _running
    print(f"[worker] Received signal {signum}, shutting down...", flush=True)
    _running = False


def run_worker() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    print("[worker] Initializing database schema...", flush=True)
    web_service.init_db()
    print(
        f"[worker] Ready (poll_interval={POLL_INTERVAL}s, ffmpeg={web_service.FFMPEG_PATH})",
        flush=True,
    )

    conn = None
    while _running:
        try:
            if conn is None or conn.closed:
                conn = psycopg2.connect(**web_service.DB_CONFIG)
                conn.autocommit = False

            job = web_service.claim_next_pending_job(conn)
            if job is None:
                time.sleep(POLL_INTERVAL)
                continue

            print(
                f"[worker] Processing job #{job['id']} for company={job['company_name']!r} "
                f"(input={job['input_path']})",
                flush=True,
            )
            result = web_service.process_claimed_job(conn, job)
            if result["status"] == "completed":
                print(
                    f"[worker] Completed job #{job['id']}: {result['processed_video_url']}",
                    flush=True,
                )
            else:
                print(
                    f"[worker] Failed job #{job['id']}: {result.get('error')}",
                    flush=True,
                )
        except psycopg2.OperationalError as exc:
            print(f"[worker] Database connection error: {exc}; retrying...", flush=True)
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
                conn = None
            time.sleep(max(POLL_INTERVAL, 2.0))
        except Exception as exc:
            print(f"[worker] Unexpected error: {exc}", flush=True)
            time.sleep(max(POLL_INTERVAL, 2.0))

    if conn is not None and not conn.closed:
        conn.close()
    print("[worker] Stopped cleanly.", flush=True)


if __name__ == "__main__":
    run_worker()
