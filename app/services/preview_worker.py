"""Async preview generation — run heavy PyMuPDF work in a worker thread."""
import logging
import threading
from queue import Queue, Empty

from app.extensions import db
from app.models import PrintJob

log = logging.getLogger(__name__)


_queue = Queue()
_started = False
_started_lock = threading.Lock()
_app = None


def enqueue(job_id, file_path, ext):
    """Queue a preview generation task."""
    _queue.put((job_id, file_path, ext))


def _worker():
    from app.services.file_handler import generate_previews
    while True:
        try:
            job_id, file_path, ext = _queue.get()
        except Exception:
            continue
        try:
            with _app.app_context():
                previews = generate_previews(job_id, file_path, ext)
                job = db.session.get(PrintJob, job_id)
                if job is not None:
                    job.preview_status = 'ready' if previews else 'failed'
                    job.preview_pages = len(previews) if previews else 0
                    db.session.commit()
        except Exception as e:
            log.exception('Preview generation failed for job %s: %s', job_id, e)
            try:
                with _app.app_context():
                    job = db.session.get(PrintJob, job_id)
                    if job is not None:
                        job.preview_status = 'failed'
                        db.session.commit()
            except Exception:
                pass


def start(app, workers=2):
    global _started, _app
    with _started_lock:
        if _started:
            return
        _app = app
        for _ in range(workers):
            t = threading.Thread(target=_worker, daemon=True)
            t.start()
        _started = True
        log.info('Preview worker started with %d threads', workers)
