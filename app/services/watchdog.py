"""Background watchdog jobs.

- Auto-fails jobs stuck in ready_to_print/printing past configured timeouts.
- Deletes stored documents once their job is finished (retention sweep).
- Periodically deletes orphaned upload/preview files (no DB row).
- Marks agents as offline when their heartbeat is stale.
"""
import logging
import os
from datetime import datetime, timezone, timedelta

from app.extensions import db
from app.models import PrintJob, AgentStatus
from app.services.queue_manager import fail_job, cancel_job
from app.services.retention import purge_many

log = logging.getLogger(__name__)


def _now():
    return datetime.now(timezone.utc)


def sweep_stuck_jobs(app):
    """Auto-fail jobs that have been stuck too long."""
    with app.app_context():
        try:
            ready_minutes = app.config.get('STUCK_READY_MINUTES', 10)
            print_minutes = app.config.get('STUCK_PRINTING_MINUTES', 15)
            now = _now()

            ready_cutoff = now - timedelta(minutes=ready_minutes)
            print_cutoff = now - timedelta(minutes=print_minutes)

            stuck_ready = PrintJob.query.filter(
                PrintJob.status == 'ready_to_print',
                PrintJob.last_status_at < ready_cutoff,
            ).all()
            for job in stuck_ready:
                log.warning('Auto-failing stuck ready job #%s (%s)', job.id, job.filename)
                fail_job(job, f'Stuck in ready_to_print > {ready_minutes}min — agent unresponsive')

            stuck_printing = PrintJob.query.filter(
                PrintJob.status == 'printing',
                PrintJob.last_status_at < print_cutoff,
            ).all()
            for job in stuck_printing:
                log.warning('Auto-failing stuck printing job #%s (%s)', job.id, job.filename)
                fail_job(job, f'Stuck in printing > {print_minutes}min — assumed lost')
        except Exception as e:
            log.exception('Watchdog sweep_stuck_jobs failed: %s', e)


def sweep_offline_agents(app):
    """Mark agents offline when their heartbeat is stale, and alert on it.

    The alert is raised on the transition, not on every sweep, so the admin gets
    one entry per outage with a start and an end rather than a wall of rows.
    """
    with app.app_context():
        try:
            from app.services import stock

            cutoff = _now() - timedelta(seconds=app.config.get('AGENT_OFFLINE_SECONDS', 60))
            stale = AgentStatus.query.filter(
                AgentStatus.is_online.is_(True),
                AgentStatus.last_heartbeat < cutoff,
            ).all()
            for agent in stale:
                agent.is_online = False
                name = agent.printer_name or agent.printer_id or f'kiosk {agent.id}'
                where = ' / '.join(x for x in (agent.hostname, agent.ip_address) if x)
                stock.raise_alert(
                    'kiosk_offline', f'kiosk:{agent.printer_id or agent.id}',
                    f'Kiosk offline — {name}',
                    f'No heartbeat for over {app.config.get("AGENT_OFFLINE_SECONDS", 60)}s.'
                    + (f' Last seen at {where}.' if where else ''),
                    severity='critical')
            if stale:
                db.session.commit()

            # Anything heartbeating again closes its outage.
            for agent in AgentStatus.query.filter(AgentStatus.is_online.is_(True)).all():
                stock.resolve_alert(f'kiosk:{agent.printer_id or agent.id}',
                                    note='Kiosk came back online')
        except Exception as e:
            log.exception('Watchdog sweep_offline_agents failed: %s', e)


def sweep_stock(app):
    """Re-check consumable levels so a threshold change shows up promptly."""
    with app.app_context():
        try:
            from app.services import stock
            stock.check_all()
        except Exception as e:
            log.exception('Watchdog sweep_stock failed: %s', e)


def sweep_password_resets(app):
    """Retire reset codes past their expiry.

    They already fail verification once expired — this is so the admin's list of
    outstanding codes reflects reality, and so a dead hash is not left sitting
    in the table.
    """
    with app.app_context():
        try:
            from app.services import password_reset
            gone = password_reset.expire_stale()
            if gone:
                log.info('Watchdog expired %s password reset code(s)', gone)
        except Exception as e:
            log.exception('Watchdog sweep_password_resets failed: %s', e)


def sweep_retention(app):
    """Delete stored documents whose job is finished.

    Complements the immediate purge in complete_job/cancel_job: it catches
    grace-period expiries, failed jobs held back for a retry window, purges
    that were deferred because a reprint still pointed at the same file, and
    uploads that were queued and then abandoned.
    """
    with app.app_context():
        try:
            if not app.config.get('PURGE_AFTER_PRINT', True):
                return
            now = _now()

            done_cutoff = now - timedelta(minutes=app.config.get('FILE_RETENTION_MINUTES', 0))
            done = PrintJob.query.filter(
                PrintJob.status == 'completed',
                PrintJob.files_purged_at.is_(None),
                PrintJob.last_status_at < done_cutoff,
            ).limit(500).all()
            n = purge_many(done, 'completed')

            fail_cutoff = now - timedelta(
                hours=app.config.get('FAILED_FILE_RETENTION_HOURS', 24))
            stale = PrintJob.query.filter(
                PrintJob.status.in_(('failed', 'cancelled')),
                PrintJob.files_purged_at.is_(None),
                PrintJob.last_status_at < fail_cutoff,
            ).limit(500).all()
            n += purge_many(stale, 'terminal')

            # Uploads that were never printed and never cancelled. Cancel them
            # (which refunds any charge) so the file can go.
            abandoned_hours = app.config.get('ABANDONED_FILE_HOURS', 72)
            if abandoned_hours > 0:
                abandoned_cutoff = now - timedelta(hours=abandoned_hours)
                abandoned = PrintJob.query.filter(
                    PrintJob.status.in_(('queued', 'prioritized')),
                    PrintJob.files_purged_at.is_(None),
                    PrintJob.submitted_at < abandoned_cutoff,
                ).limit(200).all()
                for job in abandoned:
                    log.info('Cancelling abandoned job #%s (%s) — queued > %dh',
                             job.id, job.filename, abandoned_hours)
                    cancel_job(job)
                    n += 1

            if n:
                log.info('Retention sweep purged %d job file(s)', n)
        except Exception as e:
            log.exception('Watchdog sweep_retention failed: %s', e)


def sweep_orphan_files(app):
    """Delete upload/preview files older than ORPHAN_FILE_DAYS with no matching DB row."""
    with app.app_context():
        try:
            days = app.config.get('ORPHAN_FILE_DAYS', 7)
            cutoff = _now() - timedelta(days=days)
            upload_dir = app.config['UPLOAD_FOLDER']
            preview_dir = app.config['PREVIEW_FOLDER']

            # Set of stored_filenames referenced by jobs
            referenced = {
                row[0] for row in db.session.query(PrintJob.stored_filename).all()
                if row[0]
            }
            # Also collect base names of file_path entries
            for row in db.session.query(PrintJob.file_path).all():
                if row[0]:
                    referenced.add(os.path.basename(row[0]))

            # Clean originals
            if os.path.isdir(upload_dir):
                for name in os.listdir(upload_dir):
                    if name in referenced:
                        continue
                    path = os.path.join(upload_dir, name)
                    try:
                        st = os.stat(path)
                        if datetime.fromtimestamp(st.st_mtime, tz=timezone.utc) < cutoff:
                            os.remove(path)
                            log.info('Deleted orphan upload: %s', name)
                    except OSError:
                        pass

            # Clean previews — keyed by job_id directory
            existing_job_ids = {row[0] for row in db.session.query(PrintJob.id).all()}
            if os.path.isdir(preview_dir):
                for entry in os.listdir(preview_dir):
                    full = os.path.join(preview_dir, entry)
                    if not os.path.isdir(full):
                        continue
                    try:
                        job_id = int(entry)
                    except ValueError:
                        continue
                    if job_id in existing_job_ids:
                        continue
                    try:
                        for f in os.listdir(full):
                            try:
                                os.remove(os.path.join(full, f))
                            except OSError:
                                pass
                        os.rmdir(full)
                        log.info('Deleted orphan preview dir: %s', entry)
                    except OSError:
                        pass
        except Exception as e:
            log.exception('Watchdog sweep_orphan_files failed: %s', e)


def start_scheduler(app):
    """Start APScheduler with watchdog jobs."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except ImportError:
        log.warning('APScheduler not installed; watchdog disabled.')
        return None

    sched = BackgroundScheduler(daemon=True, timezone='UTC')
    sched.add_job(lambda: sweep_stuck_jobs(app), 'interval', minutes=1, id='stuck_jobs',
                  max_instances=1, coalesce=True)
    sched.add_job(lambda: sweep_offline_agents(app), 'interval', seconds=30, id='offline_agents',
                  max_instances=1, coalesce=True)
    sched.add_job(lambda: sweep_retention(app), 'interval', minutes=15, id='retention',
                  max_instances=1, coalesce=True)
    sched.add_job(lambda: sweep_stock(app), 'interval', minutes=5, id='stock',
                  max_instances=1, coalesce=True)
    sched.add_job(lambda: sweep_orphan_files(app), 'interval', hours=6, id='orphan_files',
                  max_instances=1, coalesce=True)
    sched.add_job(lambda: sweep_password_resets(app), 'interval', minutes=5,
                  id='password_resets', max_instances=1, coalesce=True)
    sched.start()
    log.info('Watchdog scheduler started')
    return sched
