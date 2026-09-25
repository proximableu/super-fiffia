# syntax=docker/dockerfile:1
FROM python:3.12-slim

# Don't write .pyc files and keep stdout/stderr unbuffered for clean logs.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first so the dependency layer is cached independently.
# ``psycopg[binary]`` ships a vendored libpq, so no system libpq is required.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application, configuration, migrations and Docker bootstrap files.
COPY . ./

# Make the container entrypoint executable.
RUN chmod +x docker/entrypoint.sh

# The FastAPI app listens here. ``FS_DSN`` / ``FS_OLLAMA_URL`` are injected at
# run time by docker-compose, not baked into the image.
EXPOSE 8000

ENTRYPOINT ["bash", "docker/entrypoint.sh"]
