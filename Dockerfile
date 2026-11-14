# Pinned by digest so a build always starts from the same image; Dependabot
# proposes updates (see .github/dependabot.yml).
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CONFIG_DIR=/config \
    HOST=0.0.0.0 \
    PORT=8080 \
    PUID=99 \
    PGID=100 \
    TZ=America/Chicago

WORKDIR /app
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.txt -c requirements.lock

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY app ./app
COPY web ./web

VOLUME ["/config"]
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=120s --retries=3 \
  CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8080'), timeout=4).status == 200 else 1)"

# The entrypoint hands /config to PUID:PGID and drops root before the app starts.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
# HOST and PORT come from the environment (see README "Configuration").
CMD ["sh", "-c", "exec uvicorn app.main:app --host \"${HOST:-0.0.0.0}\" --port \"${PORT:-8080}\" --proxy-headers --no-access-log"]
