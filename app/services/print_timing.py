"""How long a print job will take, and how long is left while it runs.

Two numbers matter to a customer standing at the counter: how long their
document will take before they commit to it, and how much of that is left once
it starts. Both come from the same estimate so they can never disagree.

The estimate is deliberately learned rather than fixed. A rated 30 ppm is a
laboratory number — the real figure depends on the printer, the paper, and how
often it has to wake from sleep. Every completed job records how long it
actually took, so the rate converges on this shop's printer instead of the
datasheet. Until there are enough samples, the configured default is used.

Durations are always presented as a range, not a point. A single number invites
someone to stand and watch the last ten seconds tick past; "about 1-2 min" sets
an expectation that a slow warm-up will not break.
"""
import logging
from datetime import datetime, timezone

from flask import current_app

from app.services import print_options

log = logging.getLogger(__name__)

# Rebuilt from the database at most this often. The rate moves slowly, and the
# kiosk polls this several times a second across every open page.
_CACHE_TTL_SECONDS = 300
_cache = {'at': None, 'rates': None}

# Floor for a believable measurement. A 100 ppm production press is 0.6s per
# side, so anything under half a second is not a printer — it is CUPS closing
# the job when the data reached the buffer while the paper is still moving.
# Measured on a Brother HL-L2400D over USB: CUPS reported a 20-page job
# complete in 7 seconds, and the printer's own state returned to idle after 2.
MIN_PLAUSIBLE_SECONDS_PER_IMPRESSION = 0.5


def _now():
    return datetime.now(timezone.utc)


def _cfg(name, default):
    try:
        return current_app.config.get(name, default)
    except RuntimeError:      # outside an app context
        return default


def _default_rates():
    """Seconds per impression from configured page-per-minute ratings."""
    simplex_ppm = float(_cfg('PRINT_SPEED_SIMPLEX_PPM', 30) or 30)
    duplex_ppm = float(_cfg('PRINT_SPEED_DUPLEX_PPM', 15) or 15)
    return {
        'one-sided': 60.0 / max(simplex_ppm, 1.0),
        'two-sided': 60.0 / max(duplex_ppm, 1.0),
    }


def impressions(job):
    """Sides of paper the printer actually images.

    Not the same as page_count: ranges, odd/even, n-up and copies all change it,
    and it is impressions rather than sheets that consume printer time — a
    duplex sheet is two passes through the engine.
    """
    pages = print_options.effective_pages(job) * (job.copies or 1)
    nup = job.pages_per_sheet or 1
    return max(1, (pages + nup - 1) // nup)


def _learned_rates():
    """Median seconds per impression per sides mode, from completed jobs.

    Median rather than mean: one job that sat in the queue behind a paper jam
    would drag an average up permanently, and the whole point is to reflect the
    normal case.
    """
    from app.models import PrintJob

    sample_size = int(_cfg('PRINT_TIMING_SAMPLE_SIZE', 50))
    min_samples = int(_cfg('PRINT_TIMING_MIN_SAMPLES', 5))
    overhead = float(_cfg('PRINT_JOB_OVERHEAD_SECONDS', 12))

    rows = (PrintJob.query
            .filter(PrintJob.status == 'completed',
                    PrintJob.printing_started_at.isnot(None),
                    PrintJob.printed_at.isnot(None))
            .order_by(PrintJob.printed_at.desc())
            .limit(sample_size)
            .all())

    buckets = {'one-sided': [], 'two-sided': []}
    for job in rows:
        started, finished = job.printing_started_at, job.printed_at
        if started is None or finished is None:
            continue
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=timezone.utc)

        duration = (finished - started).total_seconds()
        count = impressions(job)
        # A duration that cannot be real says the job was completed by hand in
        # the admin UI, or the agent reported late. Either way it is not a
        # measurement of the printer.
        if duration <= 0 or duration > 3600:
            continue
        per_impression = (duration - overhead) / count
        # Discard impossibly fast samples. The agent reports completion from
        # the CUPS job state, and CUPS calls a job done once the data reaches
        # the printer's buffer — on a USB laser that is seconds, while the
        # paper keeps coming for a minute. Such a sample says nothing about the
        # engine, and averaging it in would collapse every estimate to zero.
        # Nothing images a side faster than this, so anything below it is
        # measuring the cable, not the printer.
        if per_impression < MIN_PLAUSIBLE_SECONDS_PER_IMPRESSION:
            continue
        buckets.setdefault(job.sides or 'one-sided', []).append(per_impression)

    rates = dict(_default_rates())
    for sides, samples in buckets.items():
        if len(samples) >= min_samples:
            samples.sort()
            mid = len(samples) // 2
            median = (samples[mid] if len(samples) % 2
                      else (samples[mid - 1] + samples[mid]) / 2)
            rates[sides] = median
    return rates


def rates(force_refresh=False):
    """Current seconds-per-impression, learned where possible."""
    now = _now()
    if (not force_refresh and _cache['rates'] is not None
            and _cache['at'] is not None
            and (now - _cache['at']).total_seconds() < _CACHE_TTL_SECONDS):
        return _cache['rates']
    try:
        value = _learned_rates()
    except Exception:
        # Never let a timing estimate take the kiosk display down with it.
        log.exception('Falling back to configured print rates')
        value = _default_rates()
    _cache['at'] = now
    _cache['rates'] = value
    return value


def estimate_for(impression_count, sides='one-sided'):
    """Estimate from raw numbers, for quoting a job that does not exist yet."""
    overhead = float(_cfg('PRINT_JOB_OVERHEAD_SECONDS', 12))
    per_impression = rates().get(sides or 'one-sided',
                                 _default_rates()['one-sided'])
    return overhead + max(1, impression_count) * per_impression


def estimate_seconds(job):
    """Best estimate of total print time for one job, in seconds."""
    return estimate_for(impressions(job), job.sides or 'one-sided')


def remaining_seconds(job):
    """Seconds left on a job that is currently printing.

    Never returns a negative number: an estimate that has already run out still
    means "any moment now", not "finished". The agent reporting completion is
    what ends the countdown, not the clock.
    """
    total = estimate_seconds(job)
    started = job.printing_started_at
    if started is None:
        return total
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return max(0.0, total - (_now() - started).total_seconds())


def queue_seconds(jobs):
    """Total estimated time for a list of jobs, in order."""
    return sum(estimate_seconds(job) for job in jobs)


def format_duration(seconds):
    """Human phrasing for a duration. Deliberately coarse."""
    seconds = max(0, int(round(seconds)))
    if seconds < 10:
        return 'a few seconds'
    if seconds < 60:
        return f'{int(round(seconds / 5.0) * 5)} sec'
    minutes = seconds / 60.0
    if minutes < 10:
        low = int(minutes)
        return f'about {low}-{low + 1} min' if low else 'under a minute'
    return f'about {int(round(minutes))} min'


def per_page_summary():
    """Reference timings to show on the kiosk and the upload page.

    Quoted as whole jobs rather than a per-page rate. A one-page job is mostly
    fixed overhead, so "2 seconds a page" would read as a promise the first
    page cannot keep.

    The duplex example is a two-page document, not a one-page one: a single
    page has nothing on its back, so printing it double-sided takes the same
    time as simplex and comparing the two would look like a bug.
    """
    current = rates()
    overhead = float(_cfg('PRINT_JOB_OVERHEAD_SECONDS', 12))
    learned = current != _default_rates()
    return {
        'one_sided_seconds': round(current['one-sided'], 1),
        'two_sided_seconds': round(current['two-sided'], 1),
        'single_page_one_sided': format_duration(estimate_for(1, 'one-sided')),
        'two_page_two_sided': format_duration(estimate_for(2, 'two-sided')),
        'ten_page_one_sided': format_duration(estimate_for(10, 'one-sided')),
        'overhead_seconds': round(overhead, 1),
        # True once real jobs have displaced the configured defaults, so the
        # kiosk can say "measured" rather than implying a guess is a fact.
        'is_learned': learned,
    }
