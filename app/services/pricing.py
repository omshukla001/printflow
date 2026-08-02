"""Pricing calculation service."""
from app.extensions import db
from app.models import Pricing
from app.services.print_options import count_pages_in_range


def get_price_per_page(paper_size, color_mode, sides='one-sided'):
    """Price of ONE SHEET of paper for the given options.

    Duplex is priced separately: a two-sided sheet uses one piece of paper but
    two sides' worth of toner, so it carries its own rate rather than being
    half-price or the same as simplex.
    """
    pricing = Pricing.query.filter_by(
        paper_size=paper_size, color_mode=color_mode, is_active=True
    ).first()
    if pricing:
        return pricing.price_for_sides(sides)
    return 0.0


def calculate_cost(page_count, copies, paper_size, color_mode, sides='one-sided'):
    """Calculate total cost — simple variant for previews/estimates."""
    price = get_price_per_page(paper_size, color_mode, sides)
    return round(price * page_count * copies, 2)


def calculate_job_cost(job):
    """Calculate cost honoring the full set of print options on a PrintJob.

    Charge model: the rate applies to each *effective sheet of paper* (so N-up
    makes things cheaper, page-range/odd-even reduces the count). Duplex halves
    the sheet count and is then billed at its own higher per-sheet rate, so two
    pages duplexed cost less than two simplex sheets but more than one.
    """
    base_pages = count_pages_in_range(job.page_ranges, job.page_count or 1)
    if (job.page_set or 'all') == 'odd':
        base_pages = (base_pages + 1) // 2
    elif (job.page_set or 'all') == 'even':
        base_pages = base_pages // 2

    pages_to_print = base_pages * (job.copies or 1)
    nup = job.pages_per_sheet or 1
    sheets = (pages_to_print + nup - 1) // nup

    if (job.sides or 'one-sided') == 'two-sided':
        sheets = (sheets + 1) // 2

    price = get_price_per_page(job.paper_size, job.color_mode,
                               job.sides or 'one-sided')
    return round(price * sheets, 2)


def get_all_pricing():
    """Get all active pricing entries."""
    return Pricing.query.filter_by(is_active=True).order_by(
        Pricing.paper_size, Pricing.color_mode
    ).all()


def lock_job_cost(job):
    """Recompute and lock the cost of a job at the moment of submission.

    Goes through the offers layer so a job printed straight from the admin
    queue is charged the same discounted price the customer was quoted.
    """
    if job.cost_locked:
        return job.cost
    from app.services import offers  # imported here: offers depends on pricing
    offers.apply_to_job(job, commit=False)
    job.cost_locked = True
    db.session.commit()
    return job.cost
