import os
import uuid
import subprocess
import fitz  # PyMuPDF
from PIL import Image
from flask import current_app
from werkzeug.utils import secure_filename

ALLOWED_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg', 'doc', 'docx', 'txt'}
CONVERTIBLE_EXTENSIONS = {'doc', 'docx', 'txt'}

# Magic bytes for the formats we accept — defense-in-depth on top of extension.
_MAGIC_SIGNATURES = {
    'pdf':  [b'%PDF-'],
    'png':  [b'\x89PNG\r\n\x1a\n'],
    'jpg':  [b'\xff\xd8\xff'],
    'jpeg': [b'\xff\xd8\xff'],
    'doc':  [b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'],  # OLE2
    'docx': [b'PK\x03\x04'],  # zip
    'txt':  [],  # no magic — accept as-is
}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def get_extension(filename):
    return filename.rsplit('.', 1)[1].lower() if '.' in filename else ''


def sniff_matches_extension(path, ext):
    """Return True if the file's leading bytes match the claimed extension."""
    if ext == 'txt':
        return True
    sigs = _MAGIC_SIGNATURES.get(ext, [])
    if not sigs:
        return True  # unknown, allow
    try:
        with open(path, 'rb') as f:
            head = f.read(16)
        return any(head.startswith(s) for s in sigs)
    except OSError:
        return False


def _safe_name(original_name):
    cleaned = secure_filename(original_name)
    if not cleaned:
        cleaned = 'document'
    return cleaned[:200]


def save_upload(file_storage, max_bytes=None):
    """Save uploaded file with UUID name. Returns dict with file info.

    Enforces max_bytes (defaults to app config MAX_CONTENT_LENGTH) and
    validates the file signature matches its extension. Raises ValueError
    on validation failure.
    """
    original_name = _safe_name(file_storage.filename or '')
    ext = get_extension(original_name)
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError('File type not allowed.')

    if max_bytes is None:
        max_bytes = current_app.config.get('MAX_CONTENT_LENGTH', 50 * 1024 * 1024)

    stored_name = f"{uuid.uuid4().hex}.{ext}"
    upload_dir = current_app.config['UPLOAD_FOLDER']
    os.makedirs(upload_dir, exist_ok=True)
    file_path = os.path.join(upload_dir, stored_name)

    # Stream to disk with a hard byte cap so a malicious chunked upload can't blow up disk
    total = 0
    with open(file_path, 'wb') as out:
        while True:
            chunk = file_storage.stream.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                out.close()
                try:
                    os.remove(file_path)
                except OSError:
                    pass
                raise ValueError(f'File exceeds maximum size of {max_bytes} bytes.')
            out.write(chunk)

    if total == 0:
        try:
            os.remove(file_path)
        except OSError:
            pass
        raise ValueError('Empty file.')

    # Sniff content
    if not sniff_matches_extension(file_path, ext):
        try:
            os.remove(file_path)
        except OSError:
            pass
        raise ValueError('File content does not match its extension.')

    file_size = os.path.getsize(file_path)

    pdf_path = file_path
    if ext in CONVERTIBLE_EXTENSIONS:
        pdf_path = convert_to_pdf(file_path)

    page_count = count_pages(pdf_path, ext)

    return {
        'filename': original_name,
        'stored_filename': stored_name,
        'file_path': file_path,
        'pdf_path': pdf_path,
        'file_size': file_size,
        'page_count': page_count,
        'extension': ext,
    }


def convert_to_pdf(file_path):
    """Convert DOCX/DOC/TXT to PDF using LibreOffice headless."""
    output_dir = os.path.dirname(file_path)
    try:
        subprocess.run([
            'libreoffice', '--headless', '--convert-to', 'pdf',
            '--outdir', output_dir, file_path
        ], capture_output=True, timeout=60, check=True)

        pdf_path = os.path.splitext(file_path)[0] + '.pdf'
        if os.path.exists(pdf_path):
            return pdf_path
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return file_path


def count_pages(file_path, ext=None):
    """Count pages in a document."""
    if ext is None:
        ext = get_extension(file_path)

    if ext in ('png', 'jpg', 'jpeg'):
        return 1

    try:
        doc = fitz.open(file_path)
        count = len(doc)
        doc.close()
        return max(count, 1)
    except Exception:
        return 1


def generate_previews(job_id, file_path, ext=None):
    """Generate preview PNGs for a document. Returns list of preview filenames."""
    if ext is None:
        ext = get_extension(file_path)

    preview_dir = current_app.config['PREVIEW_FOLDER']
    job_preview_dir = os.path.join(preview_dir, str(job_id))
    os.makedirs(job_preview_dir, exist_ok=True)

    previews = []
    page_limit = current_app.config.get('PREVIEW_PAGE_LIMIT', 50)

    if ext in ('png', 'jpg', 'jpeg'):
        try:
            img = Image.open(file_path)
            img.thumbnail((current_app.config['PREVIEW_MAX_WIDTH'], 1200))
            preview_name = 'page_1.png'
            preview_path = os.path.join(job_preview_dir, preview_name)
            img.save(preview_path, 'PNG')
            previews.append(preview_name)
        except Exception:
            pass
    else:
        pdf_path = file_path
        if ext in CONVERTIBLE_EXTENSIONS:
            candidate = os.path.splitext(file_path)[0] + '.pdf'
            if os.path.exists(candidate):
                pdf_path = candidate

        try:
            doc = fitz.open(pdf_path)
            dpi = current_app.config['PREVIEW_DPI']
            zoom = dpi / 72
            mat = fitz.Matrix(zoom, zoom)

            for i, page in enumerate(doc):
                if i >= page_limit:
                    break
                pix = page.get_pixmap(matrix=mat)
                preview_name = f'page_{i + 1}.png'
                preview_path = os.path.join(job_preview_dir, preview_name)
                pix.save(preview_path)
                previews.append(preview_name)

            doc.close()
        except Exception:
            pass

    return previews
