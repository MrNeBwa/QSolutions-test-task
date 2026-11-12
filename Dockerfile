FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APACHE_RUN_USER=www-data \
    APACHE_RUN_GROUP=www-data \
    APACHE_LOG_DIR=/var/log/apache2

RUN apt-get update && apt-get install -y --no-install-recommends \
    apache2 \
    apache2-dev \
    build-essential \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN echo "ServerName localhost" > /etc/apache2/conf-available/servername.conf \
    && a2enconf servername

RUN mod_wsgi-express module-config > /etc/apache2/mods-available/wsgi.load \
    && a2enmod wsgi

COPY web_service.py wsgi.py ./
COPY apache.conf /etc/apache2/sites-available/000-default.conf

RUN mkdir -p /app/uploads

RUN a2dissite 000-default 2>/dev/null || true \
    && a2ensite 000-default || true

EXPOSE 80

CMD ["apache2ctl", "-D", "FOREGROUND"]