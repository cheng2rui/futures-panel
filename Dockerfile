FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive TZ=Asia/Shanghai

# 系统依赖（akshare 所需）
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    chromium-sandbox \
    fonts-wqy-microhei \
    fonts-wqy-zenhei \
    xdg-utils \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    libpangocairo-1.0-0 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

ENV CHROME_BIN=chromium \
    PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium

# Python 依赖
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# 复制应用代码
WORKDIR /app
COPY app.py .
COPY account.json account.json
COPY positions.json positions.json
COPY watchlist.json watchlist.json
COPY trades.json trades.json
COPY alert_cron.py .
COPY templates/ templates/
COPY static/ static/

# Flask 运行
EXPOSE 8318
CMD ["python", "app.py", "--port", "8318", "--host", "0.0.0.0"]
