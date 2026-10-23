FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/app/data/cat_game.db

WORKDIR /app

# Dependencies first, so code-only changes reuse this layer
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Runs as root: Fly mounts the /app/data volume root-owned, and each machine is its own VM
CMD ["python", "bot.py"]
