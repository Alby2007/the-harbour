FROM python:3.13-slim

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir .

# Non-root user; /data is the sqlite home that the compose volume mounts over.
RUN useradd --system --uid 10001 devinmobile \
    && mkdir -p /data \
    && chown devinmobile /data
USER devinmobile

ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "devinmobile.bot.main"]
