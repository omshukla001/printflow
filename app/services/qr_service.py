"""QR code generation and validation."""
import io
import qrcode
from flask import url_for


def generate_qr_png(data, box_size=10, border=2):
    """Generate a QR code PNG as bytes."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    qr.add_data(data)
    qr.make(fit=True)

    img = qr.make_image(fill_color='black', back_color='white')
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf.getvalue()


def get_qr_data_for_user(user):
    """Get the QR data string for a user (their qr_token)."""
    return user.qr_token
