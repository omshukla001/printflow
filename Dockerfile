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
# fonts-dejavu-core  -> real fonts for LibreOffice output and reportlab receipts.
#
# libmagic1 is deliberately absent: upload sniffing is done by
# file_handler._MAGIC_SIGNATURES, which reads the leading bytes directly and
# needs no library.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-writer \
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

# Run as an unprivileged user. Every uploaded file is parsed in-process by
# PyMuPDF and Pillow, and handed to LibreOffice for DOC/DOCX — all of it
# attacker-controlled input from anonymous customers. A parser bug should not
# land on uid 0.
#
# HOME must be writable: `libreoffice --headless` creates a user profile under
# $HOME on first run and exits non-zero if it cannot, which convert_to_pdf()
# swallows — DOCX uploads would then silently print unconverted.
#
# The UID is pinned so it stays stable across rebuilds; the /data volume is
# chowned to it and that ownership has to keep matching.
RUN groupadd --gid 10001 printflow \
    && useradd --uid 10001 --gid 10001 --home-dir /home/printflow --create-home printflow \
    && mkdir -p /data \
    && chown -R printflow:printflow /data /app /home/printflow
ENV HOME=/home/printflow
USER printflow

EXPOSE 5000
CMD ["gunicorn", "-c", "gunicorn.server.conf.py", "run:app"]
