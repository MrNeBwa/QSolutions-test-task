import time

import psycopg2
from psycopg2.extras import RealDictCursor

import web_service

POLL_INTERVAL = 1.0


def pick_pending_job(conn):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT * FROM jobs
            WHERE status = 'pending'
            ORDER BY id ASC
            LIMIT 1
        """)
        job = cur.fetchone()
        if job is None:
            return None
        cur.execute(
            "UPDATE jobs SET status = 'processing', updated_at = NOW() WHERE id = %s",
            (job["id"],),
        )
    conn.commit()
    return dict(job)


def main():
    print("Zoom worker started", flush=True)
    while True:
        conn = None
        try:
            conn = psycopg2.connect(**web_service.DB_CONFIG)
            conn.autocommit = False
            job = pick_pending_job(conn)
            if job is None:
                time.sleep(POLL_INTERVAL)
                continue

            print(f"Processing job #{job['id']} ({job['company_name']})", flush=True)
            res = web_service.run_job(conn, job)
            print(f"Job #{job['id']} -> {res['status']}: {res.get('processed_video_url') or res.get('error')}", flush=True)
        except Exception as e:
            print("Worker error:", e, flush=True)
            time.sleep(2)
        finally:
            if conn is not None:
                conn.close()


if __name__ == "__main__":
    main()
