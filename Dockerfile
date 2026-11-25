FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    GAMES_DIR=/games \
    PUID=99 \
    PGID=100 \
    UMASK=002

WORKDIR /app
# Dependencies first: this layer stays cached until requirements.txt changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Version label after the pip layer, so a version bump doesn't reinstall deps.
ARG VERSION=dev
LABEL org.opencontainers.image.title="GameLib" \
      org.opencontainers.image.description="Self-hosted game library for a games share" \
      org.opencontainers.image.version="${VERSION}"

COPY app ./app
COPY gunicorn.conf.py docker/entrypoint.sh ./
# Strip CRLF in case the source was copied from Windows, then make executable.
RUN sed -i 's/\r$//' /app/entrypoint.sh /app/gunicorn.conf.py \
 && chmod 755 /app/entrypoint.sh \
 && python -m compileall -q app

# The app always listens on 8080 inside the container; pick the host port
# in the Unraid template / compose file. /data and /games must be mapped
# by the template (no VOLUME here, so a missing mapping is detected
# instead of silently landing in an anonymous volume).
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4); sys.exit(0)" || exit 1

# Starts as root only to fix /data ownership, then drops to PUID:PGID.
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["gunicorn", "-c", "/app/gunicorn.conf.py", "app:create_app()"]
