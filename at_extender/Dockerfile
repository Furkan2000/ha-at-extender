# Debian statt der Alpine-Basis von Home Assistant: Playwright-Chromium läuft nur auf glibc.
# Das Image ist multi-arch (amd64 + aarch64).
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

RUN pip install --no-cache-dir \
        playwright==1.64.0 \
        paho-mqtt==2.1.0 \
        requests==2.32.3 \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* /root/.cache

WORKDIR /app
COPY run.py /app/run.py

CMD ["python3", "/app/run.py"]
