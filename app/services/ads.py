"""Kiosk advertisement media — upload, validation and serving.

Five kinds of slide can go on the kiosk screen:

* **image** — png/jpg/gif/webp, shown as-is
* **video** — mp4/webm/ogg, autoplayed muted on a loop
* **pdf** — rendered to PNG pages at upload time and cycled through, because a
  kiosk browser cannot be relied on to display an embedded PDF
* **html** — arbitrary markup, rendered inside a sandboxed iframe so a bad
  snippet cannot break the display or read the kiosk's QR token
* **url** — an external page in the same sandboxed iframe
* **text** — title and body only, no media

Files live alongside the print uploads in `<uploads>/ads/`.
"""
import logging
import os
import uuid

from flask import current_app
from werkzeug.utils import secure_filename

log = logging.getLogger(__name__)

EXTENSIONS = {
    'image': {'png', 'jpg', 'jpeg', 'gif', 'webp'},
    'video': {'mp4', 'webm', 'ogg', 'ogv', 'mov'},
    'pdf': {'pdf'},
}

MIME_TYPES = {
    'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
    'gif': 'image/gif', 'webp': 'image/webp',
    'mp4': 'video/mp4', 'webm': 'video/webm', 'ogg': 'video/ogg',
    'ogv': 'video/ogg', 'mov': 'video/quicktime',
    'pdf': 'application/pdf',
}

PDF_PAGE_LIMIT = 20
UPLOAD_TYPES = ('image', 'video', 'pdf')


def ads_dir():
    """Where ad media is kept — a sibling of the print upload folder."""
    return os.path.join(os.path.dirname(current_app.config['UPLOAD_FOLDER']), 'ads')


def _ext(filename):
    return filename.rsplit('.', 1)[1].lower() if '.' in filename else ''


def detect_type(filename):
    """Guess the media kind from the filename, or None if unsupported."""
    ext = _ext(filename)
    for kind, allowed in EXTENSIONS.items():
        if ext in allowed:
            return kind
    return None


def save_media(file_storage, media_type=None):
    """Store an uploaded ad file.

    Returns (stored_name, media_type, mime, page_count). Raises ValueError when
    the file type does not match the chosen slide type.
    """
    original = secure_filename(file_storage.filename or '')
    if not original:
        raise ValueError('That file has no usable name.')

    ext = _ext(original)
    detected = detect_type(original)
    if detected is None:
        raise ValueError('Unsupported file type. Use an image, a video, or a PDF.')
    if media_type in UPLOAD_TYPES and media_type != detected:
        raise ValueError(f'That looks like {detected} content, not {media_type}.')

    directory = ads_dir()
    os.makedirs(directory, exist_ok=True)
    stored_name = f'{uuid.uuid4().hex}.{ext}'
    file_storage.save(os.path.join(directory, stored_name))

    pages = 0
    if detected == 'pdf':
        pages = render_pdf_pages(stored_name)
        if pages == 0:
            delete_media(stored_name)
            raise ValueError('That PDF could not be read.')

    return stored_name, detected, MIME_TYPES.get(ext, 'application/octet-stream'), pages


def page_name(stored_name, page):
    """Filename of one rendered PDF page (1-indexed)."""
    stem = os.path.splitext(stored_name)[0]
    return f'{stem}_p{page}.png'


def render_pdf_pages(stored_name, limit=PDF_PAGE_LIMIT):
    """Rasterise a stored PDF to PNG pages. Returns the page count."""
    try:
        import fitz  # PyMuPDF, already required for print previews
    except ImportError:
        log.warning('PyMuPDF unavailable — cannot render PDF ad')
        return 0

    directory = ads_dir()
    path = os.path.join(directory, stored_name)
    count = 0
    try:
        doc = fitz.open(path)
        matrix = fitz.Matrix(150 / 72, 150 / 72)
        for index, page in enumerate(doc):
            if index >= limit:
                break
            pixmap = page.get_pixmap(matrix=matrix)
            pixmap.save(os.path.join(directory, page_name(stored_name, index + 1)))
            count += 1
        doc.close()
    except Exception as e:
        log.warning('Could not render PDF ad %s: %s', stored_name, e)
        return 0
    return count


def media_path(stored_name):
    """Absolute path of a stored file, or None if it escapes the ads folder."""
    if not stored_name:
        return None
    directory = os.path.realpath(ads_dir())
    candidate = os.path.realpath(os.path.join(directory, stored_name))
    if candidate != directory and not candidate.startswith(directory + os.sep):
        log.warning('Refusing ad media path outside the ads folder: %s', stored_name)
        return None
    return candidate if os.path.exists(candidate) else None


def delete_media(stored_name, pages=0):
    """Remove a stored file and any PNG pages rendered from it."""
    removed = 0
    for name in [stored_name] + [page_name(stored_name, i + 1) for i in range(pages or 0)]:
        path = media_path(name)
        if path:
            try:
                os.remove(path)
                removed += 1
            except OSError as e:
                log.warning('Could not delete ad media %s: %s', name, e)
    return removed
