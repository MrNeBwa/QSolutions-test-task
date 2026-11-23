## Архитектура
- **web** — Apache + mod_wsgi, на котором запущено Flask-приложение (`web_service.py`) с установленным `ffmpeg`. Внутри контейнера слушает порт `80`, на хост пробрасывается порт `8000`.
- **worker** — фоновый процесс (`worker.py`), который забирает задачи из таблицы `jobs` в PostgreSQL, применяет `apply_zoom.py` и сохраняет `processed_video_url`.
- **db** — база данных PostgreSQL 16.
- **docker-compose.yml** — оркестрирует контейнеры, настраивает сеть, тома и зависимости.

## Быстрый запуск

1. Убедитесь, что у вас установлены Docker и Docker Compose.
2. Скопируйте файл с переменными окружения (если его нет):
   ```bash
   cp .env.example .env
   ```
3. Запустите docker образ
```bash
docker compose up --build -d
```

API будет доступно по адресу: **http://localhost:8000**

## API Эндпоинты

| Метод | URL | Описание |
|---|---|---|
| `GET` | `/` | Список доступных эндпоинтов и статус сервиса. |
| `GET` | `/health` | Проверка работоспособности (пингует БД, проверяет `uploads` и `ffmpeg`, возвращает `200 OK` или `503`). |
| `POST` | `/upload` | Загрузка видео. Multipart-форма с полями `file`, `company_name` и опционально `sidecar` (`.zoom.json`) или `zoom_events`. |
| `GET` | `/get_video_url` | Получение ссылки на видео (`video_url` и `processed_video_url`). Параметр `?company_name=...` (возвращает `404`, если нет). |
| `GET` | `/media/<filename>` | Прямая отдача загруженного и обработанного видеофайла (также обрабатывается Apache через `Alias`). |
| `GET` | `/videos` | Список всех записей в БД (для отладки). |
| `GET` | `/jobs` | Список задач пост-обработки зума. |
| `GET` | `/jobs/<id>` | Статус конкретной задачи и `processed_video_url`. |
| `POST` | `/jobs/<id>/process` | Явный запуск пост-обработки задачи `<id>`. |
| `GET` | `/upload_companies` | Минимальная HTML-форма для загрузки CSV с компаниями. |
| `POST` | `/upload_companies` | Загрузка CSV (`multipart/form-data`, поле `file`, разделитель `;`) и идемпотентный upsert в таблицу `companies` (не перезаписывает `status` и извлечённые поля). |
| `GET` | `/companies` | Список всех компаний в JSON (опционально `?status=N` или `?status=null`). |
| `GET` | `/companies/<company_name>` | Одна компания по имени (JSON) или `404`. |

## Логи и отладка

Посмотреть логи веб-сервиса и воркера:
```bash
docker compose logs -f web worker
```

Проверить работу изнутри контейнера:
```bash
docker compose exec web curl -s http://localhost/
docker compose exec web curl -s http://localhost/health
```

## Клиентская часть (Запись видео и зум)

Скрипты для записи видео (`qscript.py`, `capture_video.py`, `video_client.py`) **не входят в Docker-образ** и запускаются на хост-машине.

1. Установите зависимости для клиента:
   ```bash
   pip install -r requirements-client.txt
   playwright install chromium
   ```
2. Запустите запись с отправкой на сервер:
   ```bash
   python capture_video.py --server http://127.0.0.1:8000
   ```
   *(Во время записи можно нажимать **F5**, чтобы переключать zoom-in / zoom-out вокруг курсора; для автоматического зума через `window.__zoomAt(x, y)` и ожидания результата добавьте `--auto-zoom --wait-processing`).*

При наличии хотя бы одного события рядом с видео сохраняется sidecar `<video>.zoom.json`. Если событий нет — файл не создаётся.

## Локальная пост-обработка (`apply_zoom.py`)

```bash
python apply_zoom.py --video "video/Acme Corp.webm" --dry-run
python apply_zoom.py --video "video/Acme Corp.webm"
python apply_zoom.py --video "video/Acme Corp.webm" --ffmpeg /path/to/ffmpeg
```

## Импорт компаний и обогащение Impressum (`enrich_companies.py`)

Таблица `companies` описана в [`schema.sql`](schema.sql) и создаётся автоматически (идемпотентно) при старте веб-сервиса и скрипта обогащения. Пример входного CSV — [`companies.csv`](companies.csv).

1. Загрузите CSV через форму `http://localhost:8000/upload_companies` или `curl`:
   ```bash
   curl -F "file=@companies.csv" http://localhost:8000/upload_companies
   ```
2. Укажите `DEEPSEEK_API_KEY` в `.env` (файл `.env` в `.gitignore` и не коммитится). Для прогона без реального ключа используйте флаг `--stub` или `DEEPSEEK_STUB=1`.
3. Запустите обогащение (внутри контейнера или локально с хоста):
   ```bash
   # С реальным DeepSeek API:
   docker compose exec web python enrich_companies.py
   # Или локально с заглушкой (без API-ключа):
   python enrich_companies.py --stub
   # Фильтры:
   python enrich_companies.py --company "nutrimmun GmbH" --stub
   python enrich_companies.py --limit 10 --status 4
   ```
4. Проверьте результат:
   ```bash
   curl -s http://localhost:8000/companies | python -m json.tool
   ```

### Коды статусов (`CompanyStatus`)
- `NULL` — импортирована, ещё не обработана
- `0` (`OK`) — Impressum разобран и адрес полный (`postcode`, `city`, `street`, `house_no`)
- `1` (`NO_IMPRESSUM_LINK_FOUND`) — на главной странице нет ссылки Impressum / Imprint
- `2` (`PARSING_FAILED`) — LLM не извлёк ни одного поля (или вернул невалидный JSON после ретраев)
- `3` (`ADDRESS_INCOMPLETE`) — поля извлечены, но адрес неполный
- `4` (`ERROR`) — сетевая ошибка / HTTP 4xx-5xx / ошибка DeepSeek API
- `5` (`NO_WEBSITE`) — пустое поле `web`

### Оптимизация токенов (`impressum.optimize_text`)
Порог — `MAX_TEXT_CHARS = 4000` (≈ 1000 токенов по эвристике `len(text) // 4`). Страницы короче порога отправляются без изменений. Если текст длиннее:
1. Удаляются служебные HTML-теги (`head`, `script`, `style`, `noscript`, `svg`, `nav`, `header`, `footer`, `form`) и декодируются Cloudflare `data-cfemail` адреса; отбрасываются очевидные строки-шаблоны (cookie-баннеры, меню, `Datenschutz`, `AGB`, `©`).
2. **Head anchor**: находится последний юридический заголовок (`Impressum`, `Angaben gemäß § 5 TMG/DDG`, `Anbieter`, `Gesetzliche Anbieterkennung` и т.п.) непосредственно перед первым сильным сигналом (это отсекает дубликаты пункта меню «Impressum» выше по странице).
3. **Tail anchor**: ищется последнее вхождение сильного сигнала (`+49`/`0049`, `DE` + 9 цифр, e-mail, `HRB`/`HRA`/`Amtsgericht`, `Geschäftsführer`/`Gesellschafter`/`Verantwortlich`) до начала секций длинных дисклеймеров (`Disclaimer`, `Haftungsausschluss`, `Urheberrecht`), с запасом `+400` символов.
4. **Проверка и fallback**: проверяется наличие и head-якоря, и сильного сигнала; при провале используется очищенный текст, а если он всё ещё превышает бюджет — окно `head + tail` (`max_chars // 2` с каждой стороны).
5. До/после количество символов и оценка токенов логируются для каждой компании.

### Демо-результаты прогона (DeepSeek API, `deepseek-chat`)

Снимок последнего прогона (`GET /companies`) сохранён в [`demo_companies.json`](demo_companies.json):

| Компания | Impressum URL | Символы (до → после) | Токены (≈) | Статус |
|---|---|---|---|---|
| `nutrimmun GmbH` | `https://nutrimmun.de/impressum/` | `4944 → 668` | `~1236 → ~167` (**-86.5%**) | `0 (OK)` |
| `Barolo & Champagne GbR` | `https://www.baroloandchampagne.com/impressum` | `8937 → 762` | `~2234 → ~190` (**-91.5%**) | `0 (OK)` |

## Тесты

```bash
pytest -v
```

Тесты автоматически запускаются в GitHub Actions на каждый Pull Request (`.github/workflows/ci.yml`).
