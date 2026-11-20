# Video Recorder & Camtasia-Style Zoom Service

Сервис записи видео в браузере (Playwright) с поддержкой плавного Camtasia-style зума (пост-обработка через FFmpeg `zoompan`) и Docker-стеком (Apache + mod_wsgi + Flask + фоновый worker + PostgreSQL 16).

## Архитектура

- **web** — Apache + mod_wsgi с Flask-приложением (`web_service.py`) и установленным `ffmpeg`. Внутри контейнера слушает порт `80`, на хост пробрасывается порт `8000`. Принимает видео и `.zoom.json` sidecar, управляет задачами пост-обработки и отдаёт файлы из `/media/`.
- **worker** — фоновый процесс (`worker.py`, тот же Docker-образ с `ffmpeg`), который опрашивает таблицу `jobs` в PostgreSQL, автоматически применяет `apply_zoom.py` к новым видео и сохраняет `processed_video_url` обратно в PostgreSQL.
- **db** — база данных PostgreSQL 16 (таблицы `videos` и `jobs`).
- **qscript.py** — клиентский Playwright-рекордер с горячей клавишей `F5`, программным хуком `window.__zoomAt(x, y)`, визуальным кольцом-индикатором и записью sidecar-файла `<video>.zoom.json`.
- **apply_zoom.py** — модуль и CLI для пост-обработки видео через FFmpeg `zoompan` с плавным cosine ease-in / hold / ease-out, поддержкой нескольких пар `in`/`out` и открытого зума до конца ролика.

## Быстрый запуск (Docker)

1. Убедитесь, что установлены Docker и Docker Compose.
2. Скопируйте файл с переменными окружения (если его нет):
   ```bash
   cp .env.example .env
   ```
3. Соберите и запустите контейнеры (`web`, `worker`, `db`):
   ```bash
   docker compose up --build -d
   ```

API будет доступно по адресу: **http://localhost:8000**

## API Эндпоинты

| Метод | URL | Описание |
|---|---|---|
| `GET` | `/` | Список доступных эндпоинтов и статус сервиса. |
| `GET` | `/health` | Проверка работоспособности (PostgreSQL, права на `/app/uploads`, наличие `ffmpeg`). |
| `POST` | `/upload` | Загрузка видео (`file`, `company_name`). Опционально принимает файл `sidecar` (`.zoom.json`) или JSON-поле `zoom_events` и флаг `auto_process=true\|false`. При наличии sidecar создаёт задачу в `jobs` (`pending` или `created`). |
| `GET` | `/get_video_url` | Получение ссылок на исходное (`video_url`) и обработанное (`processed_video_url`) видео по `?company_name=...`. |
| `GET` | `/media/<filename>` | Прямая отдача исходного и обработанного (`*_zoomed.webm`) видео (также обрабатывается Apache через `Alias`). |
| `GET` | `/videos` | Список всех записей в таблице `videos`. |
| `GET` | `/jobs` | Список всех задач пост-обработки (поддерживает фильтр `?status=pending\|processing\|completed\|failed`). |
| `POST` | `/jobs` | Создать задачу пост-обработки для уже загруженного видео (`company_name` или `video_id`, опционально `sidecar`/`zoom_events`). |
| `GET` | `/jobs/<id>` | Статус задачи пост-обработки и `processed_video_url`. |
| `POST` | `/jobs/<id>/process` | Явно запустить пост-обработку задачи `<id>` (опционально `?force=true` или новый `sidecar`). |

## Запись видео и захват событий зума (клиент)

Скрипты записи (`qscript.py`, `capture_video.py`, `video_client.py`) запускаются на хост-машине:

1. Установите зависимости клиента:
   ```bash
   pip install -r requirements-client.txt
   playwright install chromium
   ```
2. Запустите запись:
   ```bash
   # Обычная запись (можно нажимать F5 вручную для zoom-in / zoom-out)
   python capture_video.py --server http://127.0.0.1:8000

   # Запись с автоматическими вызовами window.__zoomAt(x, y) и ожиданием обработки в Docker
   python capture_video.py --server http://127.0.0.1:8000 --auto-zoom --wait-processing
   ```

### Как работает захват зума во время записи

- **Горячая клавиша `F5`** (перезагрузка страницы подавлена):
  - первое нажатие фиксирует начало приближения (`"type": "in"`) вокруг текущей позиции курсора и показывает краткое **зелёное кольцо**;
  - второе нажатие фиксирует начало отдаления (`"type": "out"`) и показывает краткое **красно-оранжевое кольцо**.
- **Программный хук `window.__zoomAt(x, y)`** — выполняет тот же toggle из сценария автоматизации без нажатия `F5` (состояние сохраняется даже при переходах между страницами).
- **Sidecar `<video>.zoom.json`** создаётся **только если записано хотя бы одно событие** (обычные записи без зума не затрагиваются):
  ```json
  {
    "viewport": {"width": 1280, "height": 720},
    "fps": 25,
    "zoom_factor": 2.0,
    "ramp_sec": 0.5,
    "events": [
      {"t": 3.0, "x": 640, "y": 360, "type": "in"},
      {"t": 8.0, "x": 640, "y": 360, "type": "out"}
    ]
  }
  ```

## Локальная пост-обработка (`apply_zoom.py`)

```bash
# Предпросмотр команды ffmpeg без кодирования
python apply_zoom.py --video video/ACME_GmbH.webm --dry-run

# Рендер нового видео с зумом (сохраняет исходное разрешение, кодек libvpx-vp9)
python apply_zoom.py --video video/ACME_GmbH.webm

# Указать путь к ffmpeg, если он не в PATH, и свой путь вывода
python apply_zoom.py --video video/ACME_GmbH.webm --ffmpeg /path/to/ffmpeg -o video/out.webm
```

Особенности `apply_zoom.py`:
- Координаты мыши отображаются 1:1 на пиксели видео (`record_video_size == viewport`), с автоматическим масштабированием при отличии разрешения и clamping (`clip(...)`) к границам кадра.
- Плавный cosine ease-in / hold / ease-out для каждой пары `in`/`out`, поддержка нескольких неперекрывающихся пар и открытого зума (`in` без последующего `out` удерживается до конца видео).
- Обратная совместимость со старыми sidecar-файлами без поля `type`.
- Если sidecar отсутствует или событий нет — исходное видео копируется без изменений.

## Тестирование (`pytest`) и CI

Юнит-тесты ([tests/test_zoom.py](tests/test_zoom.py)) проверяют математику координат/clamping, расчёт ramp-кадров, генерацию фильтра `zoompan`, несколько пар `in`/`out`, открытый зум, парсинг старых sidecar без поля `type`, путь «нет событий → копия без изменений», `--dry-run` и запись sidecar в `ZoomRecorder` без зависимости от `ffmpeg`:

```bash
pytest -v
```

CI-workflow (`.github/workflows/ci.yml`) автоматически запускает `pytest -v` на каждый Pull Request.

## Логи и отладка

```bash
docker compose logs -f web worker
docker compose exec web curl -s http://localhost/health
docker compose exec web ffmpeg -version
```
