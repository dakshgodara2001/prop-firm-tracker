# Prop-Firm Deal Intelligence — dashboard + pipeline in one image.
# Data (SQLite, raw snapshots, reports) lives in the /data volume.
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PFT_DATA_DIR=/data \
    TZ=Asia/Kolkata

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /data
VOLUME /data
EXPOSE 8050

HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8050/healthz', timeout=4).status==200 else 1)"

CMD ["waitress-serve", "--host=0.0.0.0", "--port=8050", "wsgi:app"]
