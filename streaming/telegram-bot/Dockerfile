FROM python:3.12-slim

# Unbuffered so `docker logs` shows what the bot is doing as it happens,
# rather than in delayed chunks.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py clients.py ranking.py importer.py titles.py following.py ./

# Runs as a non-root user; it only needs outbound HTTP, no file access.
RUN useradd --create-home --uid 1000 bot
USER bot

CMD ["python", "bot.py"]
