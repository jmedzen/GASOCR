# ==========================================
# GASOCR - Google AI Studio Gemini PDF OCR
# Docker Container Definition (Build 033)
# ==========================================

FROM python:3.11-slim

# Prevent Python from writing .pyc files and enable unbuffered logging
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8610 \
    DATA_DIR=/app/data

WORKDIR /app

# Install system dependencies and lightweight CJK font (方案 A: 文泉驛微米黑 ~15MB)
# fonts-wqy-microhei 替代 fonts-noto-cjk (~380MB)，切圖與文字回退完全夠用
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    fonts-wqy-microhei \
    tzdata \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/*

# Copy requirements first to leverage Docker layer caching
COPY requirements.txt .

# Install Python packages
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . .

# Ensure data directory structures exist
RUN mkdir -p /app/data/uploads /app/data/renders /app/data/exports

# Declare volume for data persistence (SQLite DB, uploads, renders, exports)
VOLUME ["/app/data"]

# Expose service port
EXPOSE 8610

# Container health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8610/api/version || exit 1

# Launch GASOCR via Uvicorn
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8610"]
