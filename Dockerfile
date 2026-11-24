# Record main's recent history so the bot can post a changelog when it starts.
FROM python:3.12-slim AS changelog
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY .git /src/.git
RUN git -C /src log --first-parent -n 30 --format='%H%x1f%s%x1f%b%x1e' > /changelog

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SFBOT_DB_PATH=/data/sfbot.db \
    SFBOT_CHANGELOG_PATH=/app/changelog

WORKDIR /app
COPY pyproject.toml README.md ./
COPY sfbot ./sfbot
RUN pip install --no-cache-dir .
COPY --from=changelog /changelog ./changelog

VOLUME ["/data"]
CMD ["sfbot"]

