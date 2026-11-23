import io
import json
import os

import pytest
import requests

os.environ["SKIP_INIT_DB"] = "1"

import impressum  # noqa: E402
from impressum import CompanyStatus  # noqa: E402
import web_service  # noqa: E402


def test_normalize_url():
    assert impressum.normalize_url("nutrimmun.de") == "https://nutrimmun.de"
    assert impressum.normalize_url("https://example.de/path") == "https://example.de/path"
    assert impressum.normalize_url("http://example.de") == "http://example.de"
    assert impressum.normalize_url("   ") == ""


def test_find_impressum_url():
    html_text_match = '<a href="/about">About</a><a href="/legal/info"> Impressum </a>'
    assert impressum.find_impressum_url(html_text_match, "https://example.de/") == "https://example.de/legal/info"

    html_href_fallback = '<a href="../imprint.html">Legal notice</a>'
    assert impressum.find_impressum_url(html_href_fallback, "https://example.de/de/home") == "https://example.de/imprint.html"

    assert impressum.find_impressum_url('<a href="/contact">Contact</a>', "https://example.de/") is None


def test_extract_visible_text_strips_noise_and_decodes_cfemail():
    # "info@nutrimmun.de" encoded with Cloudflare XOR key 0xa9
    cf_hex = "a9c0c7cfc6e9c7dcdddbc0c4c4dcc787cdcc"
    html = f"""
    <html>
      <head><title>Impressum - Site</title><style>body{{color:red}}</style></head>
      <body>
        <header>Header Nav</header>
        <nav>Menu</nav>
        <main>
          <h1>Impressum</h1>
          <p>E-Mail: <span data-cfemail="{cf_hex}">[email protected]</span></p>
        </main>
        <script>console.log("ignore");</script>
        <footer>Footer Links</footer>
      </body>
    </html>
    """
    text = impressum.extract_visible_text(html)
    assert "Impressum" in text
    assert "info@nutrimmun.de" in text
    for noise in ("Header Nav", "Menu", "Footer Links", "console.log", "color:red", "Impressum - Site"):
        assert noise not in text


def test_optimize_text_under_and_over_threshold():
    short = "Impressum\nACME GmbH\nMain St 12\n10115 Berlin"
    assert impressum.optimize_text(short, max_chars=500) == short

    head_noise = "\n".join(f"Category {i}" for i in range(150))
    legal = (
        "Impressum\nAngaben gemäß § 5 DDG\nACME GmbH\n"
        "Vertreten durch: Max Mustermann\n"
        "Hauptstr. 12\n10115 Berlin\n"
        "Tel: +49 30 123456\nE-Mail: info@acme.de\n"
        "USt-IdNr.: DE123456789\nHRB 99887"
    )
    tail_noise = "Haftungsausschluss\n" + ("Dies ist ein langer Rechtstext. " * 200) + "\ninfo@acme.de"
    full = f"{head_noise}\n{legal}\n{tail_noise}"

    opt = impressum.optimize_text(full, max_chars=1000)
    assert len(opt) < len(full) // 3
    assert "ACME GmbH" in opt
    assert "DE123456789" in opt
    assert "HRB 99887" in opt
    assert "Category 0" not in opt
    assert "langer Rechtstext" not in opt

    # Last-resort head+tail window when text has no anchors and exceeds budget
    blob = "A" * 500 + "M" * 2000 + "Z" * 500
    trimmed = impressum.optimize_text(blob, max_chars=400)
    assert trimmed.startswith("A" * 100)
    assert trimmed.endswith("Z" * 100)
    assert len(trimmed) <= 410


def test_parse_extraction_statuses():
    complete = {
        "company_name": None,
        "postal_address": {
            "street": "Am Mittelhafen",
            "house_no": "56",
            "postcode": "48155",
            "city": "Münster",
            "country": None,  # country is not required for OK
        },
        "handelsregister": "HRB 6160",
        "vat_number": "DE217733235",
        "phone": "+49 251 135660",
        "email": "INFO@NUTRIMMUN.DE",
        "responsible_person": "Sabine Harders",
    }
    status, fields = impressum.parse_extraction(complete, "nutrimmun GmbH")
    assert status == CompanyStatus.OK
    assert fields["legal_name"] == "nutrimmun GmbH"
    assert fields["postal_address"] == "Am Mittelhafen 56, 48155 Münster"
    assert fields["email"] == "info@nutrimmun.de"

    incomplete = {"company_name": "ACME GmbH", "postal_address": {"city": "Berlin"}}
    status, _ = impressum.parse_extraction(incomplete, "ACME GmbH")
    assert status == CompanyStatus.ADDRESS_INCOMPLETE

    assert impressum.parse_extraction({}, "ACME GmbH")[0] == CompanyStatus.PARSING_FAILED
    assert impressum.parse_extraction(None, "ACME GmbH")[0] == CompanyStatus.PARSING_FAILED
    assert impressum.parse_extraction({"company_name": None, "email": ""}, "ACME GmbH")[0] == CompanyStatus.PARSING_FAILED


def test_extract_with_deepseek_retries_and_errors(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        impressum.extract_with_deepseek("Impressum")

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(impressum.time, "sleep", lambda _: None)

    responses = iter([
        {"choices": [{"message": {"content": "   "}}]},
        {"choices": [{"message": {"content": json.dumps({"company_name": "ACME GmbH"})}}]},
    ])

    class FakeResp:
        def __init__(self, payload, status_code=200):
            self._payload = payload
            self.status_code = status_code

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(f"HTTP {self.status_code}")

        def json(self):
            return self._payload

    monkeypatch.setattr(impressum.requests, "post", lambda *a, **kw: FakeResp(next(responses)))
    assert impressum.extract_with_deepseek("Impressum", retries=2) == {"company_name": "ACME GmbH"}

    monkeypatch.setattr(
        impressum.requests,
        "post",
        lambda *a, **kw: FakeResp({"choices": [{"message": {"content": "not json"}}]}),
    )
    assert impressum.extract_with_deepseek("Impressum", retries=2) is None

    monkeypatch.setattr(impressum.requests, "post", lambda *a, **kw: FakeResp({}, status_code=500))
    with pytest.raises(requests.HTTPError):
        impressum.extract_with_deepseek("Impressum", retries=1)


def test_extract_stub():
    sample = (
        "Impressum\nBarolo & Champagne GbR\n"
        "Gesellschafter: Claus Esslinger, Fabian Kehle\n"
        "Diegelsbergerstr. 24 | 73061 Ebersbach | Deutschland\n"
        "Mobil: 0151 - 5270 0170\nKontakt: ce@baroloandchampagne.com\n"
        "USt-IDNr.: DE316472128"
    )
    data = impressum.extract_stub(sample)
    status, fields = impressum.parse_extraction(data, "Barolo & Champagne GbR")
    assert status == CompanyStatus.OK
    assert fields["vat_number"] == "DE316472128"
    assert fields["email"] == "ce@baroloandchampagne.com"
    assert fields["postal_address"] == "Diegelsbergerstr. 24, 73061 Ebersbach, Deutschland"


def test_companies_endpoints(monkeypatch):
    store = {}
    seq = [0]

    class FakeCursor:
        def __init__(self):
            self._one = None
            self._all = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql, params=None):
            q = " ".join(sql.split())
            if q.startswith("INSERT INTO companies"):
                name, lang, country, pc, city, street, hno, web = params
                inserted = name not in store
                if inserted:
                    seq[0] += 1
                    store[name] = {
                        "id": seq[0],
                        "company_name": name,
                        "status": None,
                        "legal_name": None,
                    }
                store[name].update(
                    language=lang, country=country, postcode=pc, city=city,
                    street=street, house_no=hno, web=web,
                )
                self._one = (inserted,)
            elif "WHERE company_name = %s" in q:
                self._one = store.get(params[0])
            elif "WHERE status = %s" in q:
                self._all = [r for r in store.values() if r["status"] == params[0]]
            elif "WHERE status IS NULL" in q:
                self._all = [r for r in store.values() if r["status"] is None]
            elif "FROM companies" in q:
                self._all = list(store.values())

        def fetchone(self):
            return self._one

        def fetchall(self):
            return self._all

    class FakeDB:
        def cursor(self, cursor_factory=None):
            return FakeCursor()

        def commit(self):
            pass

    monkeypatch.setattr(web_service, "get_db", lambda: FakeDB())
    client = web_service.app.test_client()

    # GET upload form
    form = client.get("/upload_companies")
    assert form.status_code == 200
    assert b'type="file"' in form.data
    assert b"/companies" in form.data

    # First CSV upload
    csv_bytes = (
        "URL;Company Name;Language;Country;Postcode;City;Street;HouseNo;Web\n"
        ";nutrimmun GmbH;German;Germany;;;;;nutrimmun.de\n"
        ";Barolo & Champagne GbR;German;Germany;;;;;baroloandchampagne.com\n"
    ).encode("utf-8")
    r1 = client.post("/upload_companies", data={"file": (io.BytesIO(csv_bytes), "companies.csv")})
    assert r1.status_code == 200
    assert r1.get_json() == {"imported": 2, "updated": 0, "total_rows": 2}

    # Simulate enrichment result, then re-import same CSV -> enrichment must NOT be overwritten
    store["nutrimmun GmbH"]["status"] = int(CompanyStatus.OK)
    store["nutrimmun GmbH"]["legal_name"] = "nutrimmun GmbH"
    r2 = client.post("/upload_companies", data={"file": (io.BytesIO(csv_bytes), "companies.csv")})
    assert r2.get_json() == {"imported": 0, "updated": 2, "total_rows": 2}
    assert store["nutrimmun GmbH"]["status"] == 0
    assert store["nutrimmun GmbH"]["legal_name"] == "nutrimmun GmbH"

    # List / filter / single company / 404
    assert len(client.get("/companies").get_json()) == 2
    assert len(client.get("/companies?status=0").get_json()) == 1
    assert client.get("/companies/nutrimmun GmbH").get_json()["web"] == "nutrimmun.de"
    assert client.get("/companies/Unknown Corp").status_code == 404
