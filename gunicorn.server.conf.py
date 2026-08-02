"""Gunicorn config for container hosts (Render / Railway / Fly).

Differs from gunicorn.conf.py (the Pi's local config) in three ways that matter:

1. workers = 1. create_app() starts an APScheduler watchdog (app/__init__.py:107)
   and a pool of preview threads (preview_worker.start, default 2) *per process*.
   Two workers means two schedulers both running sweep_stuck_jobs on the same
   rows. Concurrency comes from threads here instead.

2. worker_class = 'gthread'. The live-status endpoint holds an SSE connection
   open for up to 120s (app/services/sse.py). Under the sync worker class each
   open stream occupies an entire worker, so a couple of users watching their
   job would block the whole site.

3. No max_requests. Recycling the only worker would also restart the watchdog
   and the preview threads.
"""
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '5000')}"

workers = 1
worker_class = 'gthread'
threads = int(os.environ.get('GUNICORN_THREADS', '16'))

# Must exceed the SSE stream lifetime (120s) or streams get killed mid-flight.
timeout = 180
graceful_timeout = 30
keepalive = 5

accesslog = '-'
errorlog = '-'
loglevel = os.environ.get('LOG_LEVEL', 'info')
