import json
import logging
import os
import re
import time
from enum import IntEnum
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HTTP_TIMEOUT = 15
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_TIMEOUT = 60
MAX_TEXT_CHARS = 4000  # ~1000 tokens (rough heuristic: chars // 4)
HEAD_MARGIN = 100
TAIL_MARGIN = 400

try:
    import lxml  # noqa: F401
    BS_PARSER = "lxml"
except ImportError:
    BS_PARSER = "html.parser"


class CompanyStatus(IntEnum):
    OK = 0
    NO_IMPRESSUM_LINK_FOUND = 1
    PARSING_FAILED = 2
    ADDRESS_INCOMPLETE = 3
    ERROR = 4
    NO_WEBSITE = 5


SYSTEM_PROMPT = """\
You extract legally required company information ("Impressum" / "Imprint") from
German-language website text.

Return ONLY a valid JSON object (no markdown fences, no commentary) that parses
with json.loads. The JSON must have exactly these keys:

{
  "company_name": "<string or null>",
  "postal_address": {
    "street": "<string or null>",
    "house_no": "<string or null>",
    "postcode": "<string or null>",
    "city": "<string or null>",
    "country": "<string or null>"
  },
  "handelsregister": "<string or null>",
  "vat_number": "<string or null>",
  "phone": "<string or null>",
  "email": "<string or null>",
  "responsible_person": "<string or null>"
}

Rules:
- Extract ONLY information actually present in the text; use null when absent.
- Do not invent or guess values.
- phone: keep the international format when possible (e.g. +49 ...).
- vat_number: German VAT ids look like DE followed by 9 digits.
- email: lowercase.
- responsible_person: the named person or role legally responsible for the
  content (e.g. Geschaeftsfuehrer, Vorstand, "inhaltlich verantwortlich").
- Output valid JSON only.
"""

STRIP_TAGS = ("head", "script", "style", "noscript", "svg", "nav", "header", "footer", "form")

BOILERPLATE_LINE_RE = re.compile(
    r"(?i)^(?:"
    r"datenschutz(?:erklärung)?|agb(?:\s*/.*)?|widerrufsrecht|versand.*|"
    r"zahlungsbedingungen|barrierefreiheit.*|allg\.\s*einkaufsbedingungen|"
    r"menü(?:\s+schließen)?|zurück|suchen|start|deutsch|english|français|"
    r"©.*|.*alle rechte vorbehalten\.?|"
    r".*\bcookies?\b.*(?:akzeptieren|konfigurieren|notwendige|disabled|verwendet).*"
    r")$"
)

HEAD_ANCHOR_RE = re.compile(
    r"(?i)\b(?:"
    r"angaben\s+gemäß\s+§\s*5\s*(?:tmg|ddg)|"
    r"gesetzliche\s+anbieterkennung|"
    r"anbieterkennzeichnung|"
    r"diensteanbieter|anbieter|"
    r"impressum|imprint|legal\s+notice"
    r")\b"
)

TAIL_ANCHOR_RE = re.compile(
    r"(?i)(?:"
    r"(?:\+|00)49[\s()./-]*\d[\d\s()./-]{5,}|"
    r"\bDE(?:\s*\d){9}\b|"
    r"[\w.+-]+@[\w-]+\.[\w.-]+|"
    r"\b(?:HRB|HRA)\s*\d+\b|"
    r"\b(?:Registergericht|Amtsgericht|Verantwortlich|Vertreten\s+durch|"
    r"Geschäftsführer|Geschäftsführung|Gesellschafter|Inhaltlich\s+verantwortlich)\b"
    r")"
)

DISCLAIMER_HEADING_RE = re.compile(
    r"(?im)^(?:"
    r"disclaimer|haftungsausschluss|haftung\s+für\s+inhalte|"
    r"urheberrecht(?:\s*/.*)?|rechtliche\s+hinweise.*|keine\s+werbung"
    r")\b"
)

REGISTRY_OR_VAT_RE = re.compile(r"(?i)\b(?:HRB|HRA)\s*\d+\b|\bDE(?:\s*\d){9}\b")


def load_dotenv(path=".env"):
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, val)


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    return url


def fetch_page(url: str, timeout: int = HTTP_TIMEOUT) -> tuple[str, str]:
    resp = requests.get(
        url,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "de,en;q=0.8"},
        timeout=timeout,
    )
    resp.raise_for_status()
    if resp.encoding and resp.encoding.lower() == "iso-8859-1" and resp.apparent_encoding:
        resp.encoding = resp.apparent_encoding
    return resp.url, resp.text


def find_impressum_url(html: str, base_url: str) -> str | None:
    soup = BeautifulSoup(html, BS_PARSER)
    anchors = soup.find_all("a", href=True)

    # 1. Exact visible text match (case-insensitive)
    for a in anchors:
        text = a.get_text(" ", strip=True).lower()
        href = a["href"].strip()
        if text in ("impressum", "imprint") and href and not href.startswith(("javascript:", "mailto:")):
            return urljoin(base_url, href)

    # 2. Fallback: href contains impressum or imprint
    for a in anchors:
        href = a["href"].strip()
        low = href.lower()
        if ("impressum" in low or "imprint" in low) and not low.startswith(("javascript:", "mailto:")):
            return urljoin(base_url, href)

    return None


def _decode_cfemail(encoded: str) -> str:
    try:
        key = int(encoded[:2], 16)
        return "".join(chr(int(encoded[i:i + 2], 16) ^ key) for i in range(2, len(encoded), 2))
    except (ValueError, IndexError):
        return ""


def extract_visible_text(html: str) -> str:
    soup = BeautifulSoup(html, BS_PARSER)
    for tag in soup.find_all(attrs={"data-cfemail": True}):
        decoded = _decode_cfemail(tag["data-cfemail"])
        if decoded:
            tag.replace_with(decoded)
    for tag in soup.find_all(STRIP_TAGS):
        tag.decompose()

    raw = soup.get_text(separator="\n")
    lines = [re.sub(r"\s+", " ", line).strip() for line in raw.splitlines()]
    return "\n".join(line for line in lines if line)


def estimate_tokens(text: str) -> int:
    return len(text) // 4


def optimize_text(text: str, max_chars: int = MAX_TEXT_CHARS) -> str:
    """Trim boilerplate head/tail from long Impressum text while keeping legal signals."""
    if len(text) <= max_chars:
        return text

    # 1. Strip obvious boilerplate lines
    kept = [
        line for line in text.splitlines()
        if not (len(line) <= 120 and BOILERPLATE_LINE_RE.match(line.strip()))
    ]
    cleaned = "\n".join(kept) or text

    # 2. Head anchor: pick the last legal heading right before the first strong signal
    #    (avoids picking duplicate "Impressum" menu/breadcrumb items far above the content).
    head_matches = list(HEAD_ANCHOR_RE.finditer(cleaned))
    tail_matches = list(TAIL_ANCHOR_RE.finditer(cleaned))

    head_start = 0
    if head_matches:
        first_signal = next((m for m in tail_matches if m.start() >= head_matches[0].start()), None)
        if first_signal:
            preceding = [m for m in head_matches if m.start() <= first_signal.start()]
            near = [m for m in preceding if first_signal.start() - m.start() <= 1500]
            head_start = (near[-1] if near else preceding[0]).start()
        else:
            head_start = head_matches[0].start()

    # 3. Tail anchor: find the last strong legal signal after head_start, stopping before
    #    long disclaimer/copyright sections when no registry/VAT appears after them.
    after_head = cleaned[head_start:]
    after_signals = list(TAIL_ANCHOR_RE.finditer(after_head))

    cut_limit = len(after_head)
    if after_signals:
        first_sig_end = after_signals[0].end()
        for dm in DISCLAIMER_HEADING_RE.finditer(after_head):
            if dm.start() > first_sig_end and not REGISTRY_OR_VAT_RE.search(after_head[dm.start():]):
                cut_limit = dm.start()
                break

    scoped_signals = [m for m in after_signals if m.end() <= cut_limit]
    if scoped_signals:
        tail_end = min(scoped_signals[-1].end() + TAIL_MARGIN, cut_limit)
    else:
        tail_end = cut_limit

    # 4. Sandwich = head + tail cut together (snap start to line boundary)
    raw_start = max(0, head_start - HEAD_MARGIN)
    start = cleaned.rfind("\n", 0, raw_start) + 1 if raw_start > 0 else 0
    candidate = cleaned[start:head_start + tail_end].strip()

    # 5. Verification / fallback
    if not (HEAD_ANCHOR_RE.search(candidate) and TAIL_ANCHOR_RE.search(candidate)):
        candidate = cleaned

    if len(candidate) > max_chars:
        half = max_chars // 2
        candidate = candidate[:half].rstrip() + "\n...\n" + candidate[-half:].lstrip()

    return candidate


def extract_with_deepseek(text: str, retries: int = 3) -> dict | None:
    """Call DeepSeek Chat Completions in JSON mode. Returns None if output stays unparseable."""
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY environment variable is not set")

    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"Extract the Impressum data from the text below and return JSON:\n\n{text}",
            },
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 1024,
        "temperature": 0,
    }

    for attempt in range(1, retries + 1):
        resp = requests.post(
            DEEPSEEK_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
            timeout=DEEPSEEK_TIMEOUT,
        )
        resp.raise_for_status()

        choices = resp.json().get("choices") or []
        content = ((choices[0].get("message") or {}).get("content") or "").strip() if choices else ""
        if content:
            try:
                parsed = json.loads(content)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass

        logger.warning("DeepSeek returned blank/invalid JSON (attempt %d/%d)", attempt, retries)
        if attempt < retries:
            time.sleep(attempt)

    return None


def extract_stub(text: str) -> dict:
    """Local regex stub returning the exact DeepSeek JSON schema (for testing without an API key)."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    company_name = None
    for line in lines:
        if re.search(r"\b(?:GmbH|GbR|AG|UG|OHG|KG|e\.K\.|mbH|Ltd\.?|Inc\.?)\b", line) and len(line) <= 100:
            company_name = line
            break

    email_m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
    vat_m = re.search(r"\bDE(?:\s*\d){9}\b", text)
    reg_m = re.search(r"\b(?:HRB|HRA)\s*\d+\b", text)
    phone_m = re.search(
        r"(?i)(?:(?:Tel(?:efon)?|Mobil|Phone|T)\s*[:.]?\s*)((?:\+|00)?\d[\d\s()./-]{6,}\d)",
        text,
    ) or re.search(r"((?:\+|00)49[\s()./-]*\d[\d\s()./-]{5,}\d)", text)

    responsible = None
    for idx, line in enumerate(lines):
        m = re.match(
            r"(?i)^(?:Geschäftsführer|Geschäftsführung|Gesellschafter|Vertreten durch|"
            r"Inhaltlich\s+Verantwortliche[r]?[^:]*)\s*[:.]?\s*(.*)$",
            line,
        )
        if m:
            rest = m.group(1).strip()
            responsible = rest or (lines[idx + 1] if idx + 1 < len(lines) else None)
            break
        if re.search(r"(?i)vertreten durch die Geschäftsführung", line) and idx + 1 < len(lines):
            responsible = lines[idx + 1]
            break

    street = house_no = postcode = city = country = None
    for idx, line in enumerate(lines):
        # Single-line pipe/comma format, e.g. "Diegelsbergerstr. 24 | 73061 Ebersbach | Deutschland"
        parts = [p.strip() for p in re.split(r"[|]", line)]
        if len(parts) >= 2:
            pc_m = re.match(r"^(\d{5})\s+(.+)$", parts[1])
            st_m = re.match(r"^(.+?)\s+(\d+[a-zA-Z]?(?:[-/]\d+)?)$", parts[0])
            if pc_m and st_m:
                street, house_no = st_m.group(1).strip(), st_m.group(2).strip()
                postcode, city = pc_m.group(1), pc_m.group(2).strip()
                if len(parts) >= 3:
                    country = parts[2]
                break

        # Multi-line format: street+house_no on previous line, "12345 City" on current line
        pc_m = re.match(r"^(\d{5})\s+([A-ZÄÖÜ][A-Za-zÄÖÜäöüß .'-]+)$", line)
        if pc_m:
            postcode, city = pc_m.group(1), pc_m.group(2).strip()
            if idx > 0:
                st_m = re.match(r"^(.+?)\s+(\d+[a-zA-Z]?(?:[-/]\d+)?)$", lines[idx - 1])
                if st_m:
                    street, house_no = st_m.group(1).strip(), st_m.group(2).strip()
            if idx + 1 < len(lines) and lines[idx + 1] in ("Deutschland", "Germany", "Österreich", "Schweiz"):
                country = lines[idx + 1]
            break

    return {
        "company_name": company_name,
        "postal_address": {
            "street": street,
            "house_no": house_no,
            "postcode": postcode,
            "city": city,
            "country": country,
        },
        "handelsregister": reg_m.group(0) if reg_m else None,
        "vat_number": re.sub(r"\s+", "", vat_m.group(0)) if vat_m else None,
        "phone": phone_m.group(1).strip() if phone_m else None,
        "email": email_m.group(0).lower() if email_m else None,
        "responsible_person": responsible,
    }


def _clean_str(val) -> str | None:
    if isinstance(val, str):
        val = val.strip()
        return val or None
    return None


def parse_extraction(data: dict | None, csv_company_name: str) -> tuple[CompanyStatus, dict]:
    """Map DeepSeek JSON to DB columns and decide status per §4.6."""
    empty_fields = {
        "legal_name": None,
        "postal_address": None,
        "handelsregister": None,
        "vat_number": None,
        "phone": None,
        "email": None,
        "responsible_person": None,
        "raw_json": data,
    }
    if not isinstance(data, dict) or not data:
        return CompanyStatus.PARSING_FAILED, empty_fields

    addr = data.get("postal_address") if isinstance(data.get("postal_address"), dict) else {}
    street = _clean_str(addr.get("street"))
    house_no = _clean_str(addr.get("house_no"))
    postcode = _clean_str(addr.get("postcode"))
    city = _clean_str(addr.get("city"))
    country = _clean_str(addr.get("country"))

    extracted_name = _clean_str(data.get("company_name"))
    handelsregister = _clean_str(data.get("handelsregister"))
    vat_number = _clean_str(data.get("vat_number"))
    phone = _clean_str(data.get("phone"))
    email = _clean_str(data.get("email"))
    if email:
        email = email.lower()
    responsible_person = _clean_str(data.get("responsible_person"))

    detected = any([
        extracted_name, street, house_no, postcode, city, country,
        handelsregister, vat_number, phone, email, responsible_person,
    ])
    if not detected:
        return CompanyStatus.PARSING_FAILED, empty_fields

    address_complete = all([postcode, city, street, house_no])
    status = CompanyStatus.OK if address_complete else CompanyStatus.ADDRESS_INCOMPLETE

    street_line = " ".join(p for p in (street, house_no) if p)
    city_line = " ".join(p for p in (postcode, city) if p)
    formatted_address = ", ".join(p for p in (street_line, city_line, country) if p) or None

    return status, {
        "legal_name": extracted_name or csv_company_name,
        "postal_address": formatted_address,
        "handelsregister": handelsregister,
        "vat_number": vat_number,
        "phone": phone,
        "email": email,
        "responsible_person": responsible_person,
        "raw_json": data,
    }
