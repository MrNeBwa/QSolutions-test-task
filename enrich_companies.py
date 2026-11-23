from __future__ import annotations

import argparse
import logging
import os
import time

import psycopg2
import requests
from psycopg2.extras import Json, RealDictCursor

import impressum
from impressum import CompanyStatus

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(BASE_DIR, "schema.sql")
REQUEST_DELAY = 0.5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def get_db_config() -> dict:
    return {
        "host": os.environ.get("DB_HOST", "localhost"),
        "port": int(os.environ.get("DB_PORT", 5432)),
        "dbname": os.environ.get("DB_NAME", "videos"),
        "user": os.environ.get("DB_USER", "video_user"),
        "password": os.environ.get("DB_PASSWORD", "video_pass"),
    }


def connect_db():
    conn = psycopg2.connect(**get_db_config())
    conn.autocommit = False
    return conn


def ensure_schema():
    conn = connect_db()
    try:
        with conn.cursor() as cur, open(SCHEMA_PATH, encoding="utf-8") as f:
            cur.execute(f.read())
        conn.commit()
    finally:
        conn.close()


def select_companies(company=None, status=None, limit=None) -> list[dict]:
    where = []
    params = []

    if company is not None:
        where.append("company_name = %s")
        params.append(company)
        if status is not None:
            where.append("status = %s")
            params.append(status)
    elif status is not None:
        where.append("status = %s")
        params.append(status)
    else:
        where.append("(status IS NULL OR status <> %s)")
        params.append(int(CompanyStatus.OK))

    sql = "SELECT id, company_name, web, status FROM companies"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id ASC"
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)

    conn = connect_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def update_status_only(company_id: int, status: CompanyStatus, impressum_url: str | None = None):
    conn = connect_db()
    try:
        with conn.cursor() as cur:
            if impressum_url is not None:
                cur.execute(
                    """
                    UPDATE companies
                    SET status = %s, impressum_url = %s, updated_at = NOW()
                    WHERE id = %s
                    """,
                    (int(status), impressum_url, company_id),
                )
            else:
                cur.execute(
                    "UPDATE companies SET status = %s, updated_at = NOW() WHERE id = %s",
                    (int(status), company_id),
                )
        conn.commit()
    finally:
        conn.close()


def save_extraction(company_id: int, status: CompanyStatus, impressum_url: str, fields: dict):
    conn = connect_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE companies
                SET status = %s,
                    impressum_url = %s,
                    legal_name = %s,
                    postal_address = %s,
                    handelsregister = %s,
                    vat_number = %s,
                    phone = %s,
                    email = %s,
                    responsible_person = %s,
                    raw_json = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    int(status),
                    impressum_url,
                    fields["legal_name"],
                    fields["postal_address"],
                    fields["handelsregister"],
                    fields["vat_number"],
                    fields["phone"],
                    fields["email"],
                    fields["responsible_person"],
                    Json(fields["raw_json"]) if fields["raw_json"] is not None else None,
                    company_id,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def is_stub_mode(cli_stub: bool) -> bool:
    if cli_stub:
        return True
    if os.environ.get("DEEPSEEK_STUB", "").strip().lower() in ("1", "true", "yes"):
        return True
    if os.environ.get("DEEPSEEK_API_KEY", "").strip().lower() in ("stub", "mock"):
        return True
    return False


def process_company(row: dict, use_stub: bool = False) -> CompanyStatus:
    company_id = row["id"]
    name = row["company_name"]
    home_url = impressum.normalize_url(row.get("web") or "")

    # 4.1 Normalize URL
    if not home_url:
        logger.info("[%s] Empty web URL -> %s", name, CompanyStatus.NO_WEBSITE.name)
        update_status_only(company_id, CompanyStatus.NO_WEBSITE)
        return CompanyStatus.NO_WEBSITE

    impressum_url = None
    try:
        # 4.2 Fetch home page and find Impressum/Imprint link
        final_home_url, home_html = impressum.fetch_page(home_url)
        impressum_url = impressum.find_impressum_url(home_html, final_home_url)
        if not impressum_url:
            logger.info(
                "[%s] No Impressum/Imprint link on %s -> %s",
                name, final_home_url, CompanyStatus.NO_IMPRESSUM_LINK_FOUND.name,
            )
            update_status_only(company_id, CompanyStatus.NO_IMPRESSUM_LINK_FOUND)
            return CompanyStatus.NO_IMPRESSUM_LINK_FOUND

        time.sleep(REQUEST_DELAY)

        # 4.3 Fetch Impressum page and extract visible text
        impressum_url, imp_html = impressum.fetch_page(impressum_url)
        visible_text = impressum.extract_visible_text(imp_html)

        # 4.4 Optimize text (token budget)
        optimized_text = impressum.optimize_text(visible_text)
        before_chars = len(visible_text)
        after_chars = len(optimized_text)
        saved_pct = ((before_chars - after_chars) / before_chars * 100.0) if before_chars else 0.0
        logger.info(
            "[%s] %s | chars: %d -> %d | tokens: ~%d -> ~%d (-%.1f%%)",
            name,
            impressum_url,
            before_chars,
            after_chars,
            impressum.estimate_tokens(visible_text),
            impressum.estimate_tokens(optimized_text),
            saved_pct,
        )

        # 4.5 Extract structured JSON via DeepSeek (or local stub)
        if use_stub:
            logger.info("[%s] Running in STUB mode (no live DeepSeek call)", name)
            extracted = impressum.extract_stub(optimized_text)
        else:
            extracted = impressum.extract_with_deepseek(optimized_text)

        # 4.6 Decide status and write back to PostgreSQL
        status, fields = impressum.parse_extraction(extracted, name)
        save_extraction(company_id, status, impressum_url, fields)
        logger.info(
            "[%s] Saved -> %s (%d) | address=%s",
            name, status.name, int(status), fields["postal_address"],
        )
        return status

    except (requests.RequestException, RuntimeError, ValueError) as exc:
        logger.error("[%s] Transport/API error: %s -> %s", name, exc, CompanyStatus.ERROR.name)
        update_status_only(company_id, CompanyStatus.ERROR, impressum_url=impressum_url)
        return CompanyStatus.ERROR


def main(argv=None) -> int:
    impressum.load_dotenv(os.path.join(BASE_DIR, ".env"))

    parser = argparse.ArgumentParser(description="Enrich companies in PostgreSQL from their Impressum pages.")
    parser.add_argument("--company", help="Process only this company_name")
    parser.add_argument("--limit", type=int, help="Process at most N companies")
    parser.add_argument("--status", type=int, help="Process only rows with this integer status")
    parser.add_argument("--stub", action="store_true", help="Use local stub extractor instead of DeepSeek API")
    args = parser.parse_args(argv)

    use_stub = is_stub_mode(args.stub)
    ensure_schema()

    rows = select_companies(company=args.company, status=args.status, limit=args.limit)
    logger.info("Selected %d company row(s) to enrich%s", len(rows), " [STUB MODE]" if use_stub else "")

    for idx, row in enumerate(rows):
        if idx > 0:
            time.sleep(REQUEST_DELAY)
        process_company(row, use_stub=use_stub)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
