"""File retention — the server stages documents, it does not archive them.

A document lives on the server only long enough for the print agent to fetch
it. Once the job reaches a terminal state the stored original, any converted
PDF and the rendered previews are deleted. The `print_jobs` row survives with
`files_purged_at` set, so history, receipts and billing are unaffected.

Deletion is deliberately conservative: a blob is only unlinked when no other
job still needs it (a reprint clones `file_path`, so two rows can point at the
same bytes) and only when the resolved path really is inside UPLOAD_FOLDER.
"""
import logging
import os
from datetime import datetime, timezone

from flask import current_app

from app.extensions import db
from app.models import PrintJob

log = logging.getLogger(__name__)

# Kept local rather than imported from queue_manager to avoid a circular import.
_TERMINAL = ('completed', 'failed', 'cancelled')


def _within(directory, path):
    """True if `path` resolves to something inside `directory`."""
    try:
        root = os.path.realpath(directory)
        target = os.path.realpath(path)
        return target == root or target.startswith(root + os.sep)
    except OSError:
        return False


def _unlink(path, upload_dir):
    if not path:
        return False
    if not _within(upload_dir, path):
        log.warning('Refusing to delete path outside UPLOAD_FOLDER: %s', path)
        return False
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        log.warning('Could not delete %s: %s', path, e)
        return False


def _blob_paths(job, upload_dir):
    """Every on-disk artefact of the uploaded document, deduped.

    `file_path` is whatever gets printed (the converted PDF for DOC/DOCX/TXT),
    `stored_filename` is the original upload. For office formats both exist.
    """
    paths = []
    if job.file_path:
        paths.append(job.file_path)
    if job.stored_filename:
        paths.append(os.path.join(upload_dir, job.stored_filename))
        # LibreOffice writes <uuid>.pdf next to <uuid>.docx
        stem, ext = os.path.splitext(job.stored_filename)
        if ext.lower() != '.pdf':
            paths.append(os.path.join(upload_dir, stem + '.pdf'))

    seen, unique = set(), []
    for p in paths:
        key = os.path.realpath(p)
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


def _blob_still_needed(job):
    """True if another live job points at the same bytes (e.g. a reprint)."""
    q = PrintJob.query.filter(
        PrintJob.id != job.id,
        PrintJob.files_purged_at.is_(None),
        PrintJob.status.notin_(_TERMINAL),
    )
    if job.stored_filename:
        q = q.filter(PrintJob.stored_filename == job.stored_filename)
    elif job.file_path:
        q = q.filter(PrintJob.file_path == job.file_path)
    else:
        return False
    return db.session.query(q.exists()).scalar()


def purge_previews(job_id):
    """Delete the rendered preview PNGs for a job. Safe to call repeatedly."""
    preview_root = current_app.config.get('PREVIEW_FOLDER')
    if not preview_root:
        return 0
    job_dir = os.path.join(preview_root, str(job_id))
    if not _within(preview_root, job_dir) or not os.path.isdir(job_dir):
        return 0

    removed = 0
    try:
        for name in os.listdir(job_dir):
            try:
                os.remove(os.path.join(job_dir, name))
                removed += 1
            except OSError:
                pass
        os.rmdir(job_dir)
    except OSError as e:
        log.debug('Preview cleanup for job %s incomplete: %s', job_id, e)
    return removed


def purge_job_files(job, reason='completed', commit=True):
    """Delete a finished job's document and previews from the server.

    Returns True when the job is now purged. Returns False (leaving the job
    untouched) when the bytes are still needed by another live job — the
    watchdog sweep will come back for it.
    """
    if job.files_purged_at is not None:
        return True
    if not current_app.config.get('PURGE_AFTER_PRINT', True):
        return False
    if _blob_still_needed(job):
        log.debug('Job %s file shared with a live job — deferring purge', job.id)
        return False

    upload_dir = current_app.config['UPLOAD_FOLDER']
    deleted = sum(1 for p in _blob_paths(job, upload_dir) if _unlink(p, upload_dir))
    purge_previews(job.id)

    job.files_purged_at = datetime.now(timezone.utc)
    job.preview_status = 'purged'
    job.preview_pages = 0
    if commit:
        db.session.commit()

    log.info('Purged files for job #%s (%s) — %d file(s) removed', job.id, reason, deleted)
    return True


def purge_many(jobs, reason):
    """Purge a batch, committing once. Returns the number purged."""
    count = 0
    for job in jobs:
        try:
            if purge_job_files(job, reason=reason, commit=False):
                count += 1
        except Exception as e:  # one bad row must not stop the sweep
            log.exception('Purge failed for job #%s: %s', job.id, e)
    if count:
        db.session.commit()
    return count
