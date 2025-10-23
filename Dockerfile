FROM python:3.11-slim

# Metadata
LABEL maintainer="CRCON External Sync"
LABEL description="Standalone CRCON Data Export to External Database"

# Arbeitsverzeichnis
WORKDIR /app

# System-Dependencies für psycopg2
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libpq-dev \
        gcc \
    && rm -rf /var/lib/apt/lists/*

# Python-Dependencies
COPY requirements_standalone.txt .
RUN pip install --no-cache-dir -r requirements_standalone.txt

# Script kopieren
COPY external_sync_standalone.py .

# State-Directory erstellen
RUN mkdir -p /data

# Volume für State-Datei
VOLUME /data

# Healthcheck
HEALTHCHECK --interval=5m --timeout=10s --start-period=30s \
    CMD python external_sync_standalone.py status || exit 1

# Standard: Loop-Modus
CMD ["python", "-u", "external_sync_standalone.py", "loop"]

