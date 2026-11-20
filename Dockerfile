FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=America/Bogota

# Dependencias del sistema
RUN apt-get update && apt-get install -y \
    curl \
    build-essential \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Dependencias de Python
COPY requirements.txt .

RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Código fuente
COPY . .

# Permisos
RUN chmod +x /app/docker/api-entrypoint.sh && \
    chmod +x /app/docker/worker-entrypoint.sh

# Usuario sin privilegios
RUN useradd --create-home --shell /bin/bash appuser && \
    chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

ENTRYPOINT ["/app/docker/api-entrypoint.sh"]