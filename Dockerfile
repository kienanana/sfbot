FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SFBOT_DB_PATH=/data/sfbot.db

WORKDIR /app
COPY pyproject.toml README.md ./
COPY sfbot ./sfbot
RUN pip install --no-cache-dir .

VOLUME ["/data"]
CMD ["sfbot"]

