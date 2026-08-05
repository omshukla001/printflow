"""Shared helpers for validating + translating per-job print options.

A single source of truth for:
  - validating user form input
  - building the CUPS options dict
  - serializing options for the agent API
"""
import re


ALLOWED_COPIES = (1, 100)
ALLOWED_COLOR = ('bw', 'color')
ALLOWED_PAPER = ('A4', 'A3', 'Letter')
ALLOWED_SIDES = ('one-sided', 'two-sided')
ALLOWED_PAGES_PER_SHEET = (1, 2, 4, 6, 9)
ALLOWED_PAGE_SET = ('all', 'odd', 'even')
ALLOWED_OUTPUT_ORDER = ('normal', 'reverse')
ALLOWED_ORIENTATION = ('auto', 'portrait', 'landscape')
ALLOWED_QUALITY = ('draft', 'normal', 'high')

# Colour prints on one side only — the colour path has no duplex, so a colour
# job is always forced to simplex no matter what the form asked for.
COLOR_SIMPLEX_ONLY = True

# CUPS orientation-requested IPP values
_ORIENTATION_IPP = {
    'portrait': '3',
    'landscape': '4',
}
_QUALITY_IPP = {
    'draft': '3',
    'normal': '4',
    'high': '5',
}

_PAGE_RANGE_RE = re.compile(r'^\d+(-\d+)?(,\d+(-\d+)?)*$')


def color_enabled():
    """Whether colour printing is currently offered.

    Reads app config when inside a request/app context; defaults to False
    outside one so non-Flask callers (scripts, the agent) stay mono.
    """
    try:
        from flask import current_app
        return bool(current_app.config.get('COLOR_PRINTING_ENABLED', False))
    except Exception:
        return False


def allowed_paper():
    """Paper sizes the shop currently offers, first entry being the default.

    Falls back to the full built-in list outside an app context so the pure
    helpers stay usable from scripts.
    """
    try:
        from flask import current_app
        sizes = current_app.config.get('PAPER_SIZES')
        if sizes:
            return tuple(s for s in sizes if s in ALLOWED_PAPER) or (ALLOWED_PAPER[0],)
    except Exception:
        pass
    return ALLOWED_PAPER


def resolve_sides(color_mode, sides):
    """The sides setting a job will actually print with.

    Colour is simplex-only, so asking for duplex in colour quietly resolves to
    one-sided. Used by validation, the live quote and the CUPS options builder
    so all three agree on what is charged and what is printed.
    """
    if COLOR_SIMPLEX_ONLY and color_mode == 'color':
        return 'one-sided'
    return sides


def normalize_page_ranges(text, max_page=None):
    """Validate and normalize a page-range string.

    Returns the normalized string (no spaces, sorted segments deduplicated) or
    None for "print all pages". Raises ValueError on invalid input.
    """
    if text is None:
        return None
    s = text.replace(' ', '')
    if not s:
        return None
    if not _PAGE_RANGE_RE.match(s):
        raise ValueError('Page range must look like "1-3,5,7-9".')

    segments = []
    for part in s.split(','):
        if '-' in part:
            a, b = part.split('-', 1)
            a, b = int(a), int(b)
            if a < 1 or b < a:
                raise ValueError(f'Invalid range "{part}".')
            if max_page is not None and (a > max_page or b > max_page):
                raise ValueError(f'Range "{part}" exceeds document length ({max_page} pages).')
            segments.append((a, b))
        else:
            n = int(part)
            if n < 1:
                raise ValueError(f'Invalid page "{part}".')
            if max_page is not None and n > max_page:
                raise ValueError(f'Page {n} exceeds document length ({max_page} pages).')
            segments.append((n, n))

    # Sort + merge overlapping/adjacent ranges
    segments.sort()
    merged = []
    for a, b in segments:
        if merged and a <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))

    return ','.join(f'{a}-{b}' if a != b else f'{a}' for a, b in merged)


def count_pages_in_range(page_ranges, total_pages):
    """How many physical pages are actually printed for a given range string."""
    if not page_ranges:
        return total_pages
    count = 0
    for part in page_ranges.split(','):
        if '-' in part:
            a, b = part.split('-', 1)
            count += max(0, int(b) - int(a) + 1)
        else:
            count += 1
    return count


def effective_pages(job):
    """Effective pages printed = filtered by range, then odd/even filter."""
    base = count_pages_in_range(job.page_ranges, job.page_count or 1)
    if job.page_set == 'odd':
        return (base + 1) // 2
    if job.page_set == 'even':
        return base // 2
    return base


def effective_sheets(job):
    """Sheets of paper consumed (after pages-per-sheet and duplex)."""
    pages = effective_pages(job) * (job.copies or 1)
    nup = job.pages_per_sheet or 1
    sheets = (pages + nup - 1) // nup
    if (job.sides or 'one-sided') == 'two-sided':
        sheets = (sheets + 1) // 2
    return sheets


def validate_and_apply(form, job):
    """Read print options from a form-like dict and apply to a job.

    Returns a list of (field, error) tuples. Empty list means success.
    """
    errors = []

    def _get(name, default=''):
        v = form.get(name, default)
        return v.strip() if isinstance(v, str) else v

    # Copies
    try:
        copies = int(_get('copies', '1'))
    except ValueError:
        copies = 1
    if not (ALLOWED_COPIES[0] <= copies <= ALLOWED_COPIES[1]):
        errors.append(('copies', f'Copies must be between {ALLOWED_COPIES[0]} and {ALLOWED_COPIES[1]}.'))
        copies = max(ALLOWED_COPIES[0], min(copies, ALLOWED_COPIES[1]))
    job.copies = copies

    job.color_mode = _get('color_mode', 'bw')
    if job.color_mode not in ALLOWED_COLOR:
        errors.append(('color_mode', 'Invalid color mode.'))
        job.color_mode = 'bw'
    # Colour can be switched off shop-wide. Enforced here rather than only in
    # the template, so a hand-crafted POST can't sneak a colour job through.
    if job.color_mode == 'color' and not color_enabled():
        job.color_mode = 'bw'

    offered = allowed_paper()
    job.paper_size = _get('paper_size', offered[0])
    if job.paper_size not in offered:
        # Not an error the customer can act on when the size simply isn't
        # stocked — quietly clamp to the default rather than rejecting.
        if job.paper_size not in ALLOWED_PAPER:
            errors.append(('paper_size', 'Invalid paper size.'))
        job.paper_size = offered[0]

    job.sides = _get('sides', 'one-sided')
    if job.sides not in ALLOWED_SIDES:
        errors.append(('sides', 'Invalid sides setting.'))
        job.sides = 'one-sided'
    # Colour is simplex-only. Enforced here as well as in the template so a
    # hand-crafted POST can't book a duplex colour job at the duplex rate.
    job.sides = resolve_sides(job.color_mode, job.sides)

    # Page ranges
    try:
        job.page_ranges = normalize_page_ranges(_get('page_ranges', '') or None,
                                                max_page=job.page_count)
    except ValueError as e:
        errors.append(('page_ranges', str(e)))
        job.page_ranges = None

    # N-up
    try:
        nup = int(_get('pages_per_sheet', '1'))
    except ValueError:
        nup = 1
    if nup not in ALLOWED_PAGES_PER_SHEET:
        errors.append(('pages_per_sheet', 'Pages-per-sheet must be 1, 2, 4, 6, or 9.'))
        nup = 1
    job.pages_per_sheet = nup

    page_set = _get('page_set', 'all')
    if page_set not in ALLOWED_PAGE_SET:
        errors.append(('page_set', 'Invalid page set.'))
        page_set = 'all'
    job.page_set = page_set

    out_order = _get('output_order', 'normal')
    if out_order not in ALLOWED_OUTPUT_ORDER:
        errors.append(('output_order', 'Invalid output order.'))
        out_order = 'normal'
    job.output_order = out_order

    orientation = _get('orientation', 'auto')
    if orientation not in ALLOWED_ORIENTATION:
        errors.append(('orientation', 'Invalid orientation.'))
        orientation = 'auto'
    job.orientation = orientation

    job.fit_to_page = bool(_get('fit_to_page', '')) and _get('fit_to_page', '') != 'false'

    quality = _get('print_quality', 'normal')
    if quality not in ALLOWED_QUALITY:
        errors.append(('print_quality', 'Invalid quality.'))
        quality = 'normal'
    job.print_quality = quality

    # Checkboxes — present in form = True; absent = False. Default checked = True.
    job.collate = (_get('collate', 'on') in ('on', 'true', '1', 'yes'))

    return errors


def to_dict(job):
    """Serialize a job's print options for the agent API."""
    return {
        'copies': job.copies,
        'color_mode': job.color_mode,
        'paper_size': job.paper_size,
        'sides': job.sides,
        'page_ranges': job.page_ranges,
        'pages_per_sheet': job.pages_per_sheet,
        'page_set': job.page_set,
        'output_order': job.output_order,
        'orientation': job.orientation,
        'fit_to_page': bool(job.fit_to_page),
        'print_quality': job.print_quality,
        'collate': bool(job.collate),
    }


def to_cups_options(opts):
    """Build a CUPS options dict from a job-or-dict.

    Accepts either a PrintJob row or the dict produced by to_dict()/the agent
    payload — both have the same field names.
    """
    g = (lambda k, d=None: opts.get(k, d)) if isinstance(opts, dict) else (lambda k, d=None: getattr(opts, k, d))

    cups_opts = {}

    copies = g('copies') or 1
    if copies and copies > 1:
        cups_opts['copies'] = str(copies)

    color_mode = g('color_mode', 'bw')
    cups_opts['print-color-mode'] = 'monochrome' if color_mode == 'bw' else 'color'

    paper = g('paper_size', 'A4')
    cups_opts['media'] = paper if paper in ('A4', 'A3', 'Letter') else 'A4'

    sides = resolve_sides(color_mode, g('sides', 'one-sided'))
    cups_opts['sides'] = 'two-sided-long-edge' if sides == 'two-sided' else 'one-sided'

    page_ranges = g('page_ranges')
    if page_ranges:
        cups_opts['page-ranges'] = page_ranges

    nup = g('pages_per_sheet') or 1
    if nup and nup > 1:
        cups_opts['number-up'] = str(nup)

    page_set = g('page_set', 'all')
    if page_set in ('odd', 'even'):
        cups_opts['page-set'] = page_set

    out_order = g('output_order', 'normal')
    if out_order == 'reverse':
        cups_opts['outputorder'] = 'reverse'

    orientation = g('orientation', 'auto')
    if orientation in _ORIENTATION_IPP:
        cups_opts['orientation-requested'] = _ORIENTATION_IPP[orientation]

    if g('fit_to_page'):
        cups_opts['fit-to-page'] = ''  # CUPS treats presence as enable

    quality = g('print_quality', 'normal')
    if quality in _QUALITY_IPP and quality != 'normal':
        cups_opts['print-quality'] = _QUALITY_IPP[quality]

    collate = g('collate', True)
    if not collate:
        cups_opts['Collate'] = 'False'

    return cups_opts
