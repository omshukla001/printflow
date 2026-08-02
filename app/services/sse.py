"""Server-Sent Events stream for live job status.

Lightweight polling-based generator — picks up DB state changes without any
external broker. Connections close after a max duration so workers don't pile up.
"""
import json
import time
from flask import Response, current_app, stream_with_context

from app.models import PrintJob


def stream_job_status(user_id, job_ids=None, interval=2.0, max_seconds=120):
    """Yield SSE messages for the user's active jobs until they all settle."""

    def generate():
        start = time.time()
        last_payload = None
        while time.time() - start < max_seconds:
            q = PrintJob.query.filter(PrintJob.user_id == user_id)
            if job_ids:
                q = q.filter(PrintJob.id.in_(job_ids))
            else:
                q = q.filter(PrintJob.status.in_(
                    ('queued', 'prioritized', 'ready_to_print', 'printing')
                ))
            payload = [j.to_dict() for j in q.all()]
            data = json.dumps(payload, default=str)
            if data != last_payload:
                yield f'data: {data}\n\n'
                last_payload = data
            # End the stream when no active jobs remain
            if not payload:
                yield 'event: idle\ndata: {}\n\n'
                break
            time.sleep(interval)
        yield 'event: end\ndata: {}\n\n'

    return Response(stream_with_context(generate()),
                    mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache',
                             'X-Accel-Buffering': 'no'})
