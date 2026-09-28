FROM python:3.11-slim-bookworm

WORKDIR /app

# System deps required by Playwright's Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnspr4 libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    libatspi2.0-0 libwayland-client0 fonts-liberation wget ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps + Playwright browser
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install chromium

# Copy app
COPY . .

CMD gunicorn app:app --bind 0.0.0.0:${PORT:-8000} --workers 2 --timeout 120
