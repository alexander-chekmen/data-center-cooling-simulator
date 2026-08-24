FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependencies first so source edits do not invalidate the layer cache.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sim/ ./sim/
COPY collector/ ./collector/
COPY api/ ./api/
COPY scripts/ ./scripts/

# One image serves both roles; docker-compose picks the command.
CMD ["python", "-m", "api.main"]
