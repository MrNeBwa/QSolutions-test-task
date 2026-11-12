## Архитектура
- **web** — Apache + mod_wsgi, на котором запущено Flask-приложение (`web_service.py`). Внутри контейнера слушает порт `80`, на хост пробрасывается порт `8000`.
- **db** — база данных PostgreSQL 16.
- **docker-compose.yml** — оркестрирует оба контейнера, настраивает сеть, тома и зависимости.

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
| `GET` | `/health` | Проверка работоспособности (пингует БД, возвращает `200 OK` или `503`). |
| `POST` | `/upload` | Загрузка видео. Multipart-форма с полями `file` и `company_name`. |
| `GET` | `/get_video_url` | Получение ссылки на видео. Параметр `?company_name=...` (возвращает `404`, если нет). |
| `GET` | `/media/<filename>` | Прямая отдача загруженного видеофайла (также обрабатывается Apache через `Alias`). |
| `GET` | `/videos` | Список всех записей в БД (для отладки). |

## Логи и отладка

Посмотреть логи веб-сервиса:
```bash
docker compose logs -f web
```

Проверить работу изнутри контейнера:
```bash
docker compose exec web curl -s http://localhost/
docker compose exec web curl -s http://localhost/health
```

## Клиентская часть (Запись видео)

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
   *(Скрипт запишет видео, отправит его на сервер по адресу `http://127.0.0.1:8000` и откроет превью в браузере).*
