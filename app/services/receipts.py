"""Receipt PDF generation."""
import logging
import os
from datetime import datetime, timezone
from flask import current_app

log = logging.getLogger(__name__)


def generate_receipt(job):
    """Generate a PDF receipt for a completed job. Returns the receipt filename or None."""
    if job.status != 'completed':
        return None
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        from reportlab.lib.units import mm
    except ImportError:
        log.warning('reportlab not installed; skipping receipt generation')
        return None

    folder = current_app.config['RECEIPT_FOLDER']
    os.makedirs(folder, exist_ok=True)
    fname = f'receipt_{job.id}.pdf'
    path = os.path.join(folder, fname)

    c = canvas.Canvas(path, pagesize=A4)
    width, height = A4
    y = height - 30 * mm

    c.setFont('Helvetica-Bold', 18)
    c.drawString(20 * mm, y, 'PrintFlow Receipt')
    y -= 12 * mm

    c.setFont('Helvetica', 11)
    when = job.printed_at or datetime.now(timezone.utc)
    user_name = job.user.full_name if job.user else 'Unknown'
    lines = [
        ('Receipt #', str(job.id)),
        ('Date', when.strftime('%Y-%m-%d %H:%M UTC')),
        ('User', user_name),
        ('File', job.filename),
        ('Pages', f'{job.page_count} x {job.copies} copies'),
        ('Paper / Mode', f'{job.paper_size} / {job.color_mode.upper()}'),
        ('Sides', job.sides),
    ]
    for label, value in lines:
        c.drawString(20 * mm, y, f'{label}:')
        c.drawString(70 * mm, y, str(value)[:60])
        y -= 7 * mm

    y -= 5 * mm
    c.line(20 * mm, y, width - 20 * mm, y)
    y -= 10 * mm

    # Show the saving explicitly — a discounted total with no explanation on
    # the receipt reads like a billing error.
    if (job.discount_amount or 0) > 0:
        c.setFont('Helvetica', 11)
        c.drawString(20 * mm, y, 'Subtotal')
        c.drawRightString(width - 20 * mm, y, f'INR {(job.base_cost or job.cost):.2f}')
        y -= 7 * mm
        c.setFont('Helvetica', 9)
        c.drawString(20 * mm, y, f'Discount: {(job.discount_label or "Offer")[:70]}')
        c.drawRightString(width - 20 * mm, y, f'- INR {job.discount_amount:.2f}')
        y -= 9 * mm

    c.setFont('Helvetica-Bold', 14)
    c.drawString(20 * mm, y, 'Total')
    c.drawRightString(width - 20 * mm, y, f'INR {job.cost:.2f}')

    y -= 20 * mm
    c.setFont('Helvetica-Oblique', 9)
    c.drawString(20 * mm, y, 'Thank you for using PrintFlow.')

    c.save()
    return fname
