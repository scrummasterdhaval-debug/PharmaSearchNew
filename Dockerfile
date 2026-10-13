# PharmaSearch, as it runs in production.
#
# One process: SQLite and the register indexes are files on the disk this
# container mounts, and the background refreshes keep their state in memory,
# so the application runs as a single worker rather than several.
FROM python:3.12-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    PHARMASEARCH_DB_PATH=/data/pharmasearch.db \
    PHARMASEARCH_REGISTER_DIR=/data/open_registers \
    PHARMASEARCH_ENV=production \
    PHARMASEARCH_TRUSTED_PROXY=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    PHARMASEARCH_REGISTER_PROCESS=child

WORKDIR /app

# The browser is installed by root and run by the unprivileged user below.
# Left at its default it lands in root's home, where that user cannot see it,
# so PLAYWRIGHT_BROWSERS_PATH puts it somewhere both can read.

COPY Backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
    && python -m playwright install --with-deps chromium

COPY Backend /app

# The application writes nothing inside the image: the database, the register
# indexes and the exports all live on the mounted disk. It runs as a user
# without administrative rights: the entrypoint starts as root only to make a
# newly attached disk that user's, then drops to it before the app starts.
RUN useradd --create-home --uid 10001 pharmasearch \
    && mkdir -p /data/open_registers /data/exports \
    && rm -rf /app/exports \
    && ln -sfn /data/exports /app/exports \
    && chown -R pharmasearch:pharmasearch /data /app \
    && sed -i 's/\r$//' /app/deploy/entrypoint.sh \
    && chmod 755 /app/deploy/entrypoint.sh
ENTRYPOINT ["/app/deploy/entrypoint.sh"]

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ['PORT']+'/healthz').read()"

# --proxy-headers with --forwarded-allow-ips: the reverse proxy in front
# terminates TLS, and its X-Forwarded-Proto header is what tells the
# application the visitor arrived over HTTPS.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*' --timeout-keep-alive 120"]
