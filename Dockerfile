FROM python:3.12-slim

# Logs straight to `docker compose logs`, no .pyc files in the image
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Asset paths are relative (./assets/...), so the bot must run from /app
CMD ["python", "bot.py"]
