# Shared application image for sync, web, and worker services
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first to preserve the build cache
COPY requirements.txt .
RUN pip install -r requirements.txt

# Application code
COPY app ./app
COPY config ./config
COPY scripts ./scripts
COPY manage.py ./manage.py

# Runtime data and secrets are mounted as volumes and never copied into the image.
# The default process is the sync service; Compose overrides it for other roles.
CMD ["python", "-m", "app.service"]
