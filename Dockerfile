# PrintFlow server image — for container hosts (Render / Railway / Fly.io).
# The RPi agent is NOT built from this; it keeps running natively with pycups.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

# libreoffice-writer -> DOC/DOCX/TXT -> PDF, shelled out to by
#                       file_handler.convert_to_pdf(). Without it that function
#                       swallows FileNotFoundError and silently returns the
#                       unconverted file, so 3 of the 7 allowed extensions
#                       would appear to upload fine and then print wrong.
# libmagic1          -> python-magic, used for MIME sniffing on upload.
# fonts-dejavu-core  -> real fonts for LibreOffice output and reportlab receipts.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-writer \
        libmagic1 \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements-server.txt .
RUN pip install --no-cache-dir -r requirements-server.txt

COPY . .

# DATA_DIR is where uploads/previews/receipts live. Mount a persistent volume
# here — on an ephemeral container filesystem, a job's file would vanish before
# the agent polls for it.
ENV DATA_DIR=/data \
    CLOUD_MODE=true \
    FLASK_ENV=production
RUN mkdir -p /data

EXPOSE 5000
CMD ["gunicorn", "-c", "gunicorn.server.conf.py", "run:app"]
