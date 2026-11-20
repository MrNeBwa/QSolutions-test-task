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

## Тесты

```bash
pytest -v
```

Тесты автоматически запускаются в GitHub Actions на каждый Pull Request (`.github/workflows/ci.yml`).
