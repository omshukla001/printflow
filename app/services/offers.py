"""Promotional offers — bulk discounts and the referral programme.

Two mechanisms, both admin-managed from /admin/offers:

* **Bulk tiers** (`BulkDiscountTier`) fire automatically on page count. The
  highest matching tier wins; tiers never stack with each other.
* **Referral rewards** arrive as `DiscountVoucher` rows. Granting a reward and
  spending it are deliberately separate steps, so a reward survives in the
  user's account until they actually print.

`PrintJob.cost` always holds the final, discounted figure — the ledger and the
balance never need to know an offer was involved. `base_cost`, `discount_amount`
and `discount_label` exist only so the customer can see what they saved.
"""
import logging
import secrets
import string
from datetime import timedelta

from app.extensions import db
from app.models import (
    BulkDiscountTier, DiscountVoucher, PrintJob, Referral, User,
    get_setting, set_setting, utcnow,
)

log = logging.getLogger(__name__)

# Admin-editable settings, with the launch campaign as the default.
DEFAULTS = {
    'referral_enabled': '1',
    'referral_friend_percent': '20',    # invited friend, first order
    'referral_referrer_percent': '30',  # inviter, after the friend's first print
    'referral_max_discount': '50',      # ₹ cap on either side
    'referral_limit': '50',             # first N successful referrals
    'referral_reward_days': '0',        # voucher lifetime; 0 = never expires
    'signup_enabled': '1',              # every new account, not just invited ones
    'signup_percent': '20',             # off the first print
    'signup_max_discount': '50',        # ₹ cap
    'bulk_enabled': '1',
    'offers_stack': '1',                # allow bulk + voucher on one order
    # Plain text — the page draws its own icon. Emoji here would render as an
    # empty square on any device whose font lacks the glyph, the kiosk included.
    'offers_headline': 'Grand Opening Offers',
}

_CODE_ALPHABET = string.ascii_uppercase + string.digits
# No I/O/0/1 — these get read aloud and typed in by hand at the counter.
_CODE_ALPHABET = ''.join(c for c in _CODE_ALPHABET if c not in 'IO01')


# --- settings -------------------------------------------------------------

def get_settings():
    """All offer settings, typed."""
    def _int(key):
        try:
            return int(float(get_setting(key, DEFAULTS[key])))
        except (TypeError, ValueError):
            return int(float(DEFAULTS[key]))

    def _flag(key):
        return get_setting(key, DEFAULTS[key]) in ('1', 'true', 'True', 'yes', 'on')

    return {
        'referral_enabled': _flag('referral_enabled'),
        'referral_friend_percent': _int('referral_friend_percent'),
        'referral_referrer_percent': _int('referral_referrer_percent'),
        'referral_max_discount': _int('referral_max_discount'),
        'referral_limit': _int('referral_limit'),
        'referral_reward_days': _int('referral_reward_days'),
        'signup_enabled': _flag('signup_enabled'),
        'signup_percent': _int('signup_percent'),
        'signup_max_discount': _int('signup_max_discount'),
        'bulk_enabled': _flag('bulk_enabled'),
        'offers_stack': _flag('offers_stack'),
        'offers_headline': get_setting('offers_headline', DEFAULTS['offers_headline']),
    }


def save_settings(values):
    """Persist a dict of raw setting values. Unknown keys are ignored."""
    for key in DEFAULTS:
        if key in values:
            set_setting(key, values[key])


# --- referral codes -------------------------------------------------------

def _new_code(length=6):
    for _ in range(20):
        code = ''.join(secrets.choice(_CODE_ALPHABET) for _ in range(length))
        if not User.query.filter_by(referral_code=code).first():
            return code
    return ''.join(secrets.choice(_CODE_ALPHABET) for _ in range(length + 2))


def ensure_referral_code(user, commit=True):
    """Return the user's share code, generating it on first use."""
    if not user.referral_code:
        user.referral_code = _new_code()
        if commit:
            db.session.commit()
    return user.referral_code


def find_by_code(code):
    if not code:
        return None
    return User.query.filter_by(referral_code=code.strip().upper()).first()


# --- referral campaign ----------------------------------------------------

def rewarded_referral_count():
    """Successful referrals that actually paid out — this is what the cap counts."""
    return Referral.query.filter(
        Referral.status == 'qualified',
        Referral.reward_voucher_id.isnot(None),
    ).count()


def slots_left(settings=None):
    """Rewards still available under the campaign cap. None means unlimited.

    Counted against *successful* referrals only, matching the promise made to
    customers ("the first N successful referrals"). Pending invites are not
    reserved — several may be in flight when the last slot is taken, and those
    that arrive late qualify without a reward.
    """
    s = settings or get_settings()
    if s['referral_limit'] <= 0:
        return None
    return max(0, s['referral_limit'] - rewarded_referral_count())


def grant_voucher(user, source, percent, max_discount, description,
                  expires_days=0, commit=True):
    """Put a single-use discount into a user's account.

    Returns None for a guest — a temporary walk-in account has nowhere to keep
    a reward, so it is never granted one in the first place.
    """
    if not is_eligible(user):
        return None
    voucher = DiscountVoucher(
        user_id=user.id,
        source=source,
        discount_percent=float(percent),
        max_discount=float(max_discount) if max_discount else None,
        description=description,
        status='available',
        expires_at=(utcnow() + timedelta(days=expires_days)) if expires_days else None,
    )
    db.session.add(voucher)
    if commit:
        db.session.commit()
    return voucher


def signup_percent():
    """Headline discount for registering, or 0 when the campaign is off."""
    settings = get_settings()
    if not settings.get('signup_enabled'):
        return 0
    return float(settings.get('signup_percent') or 0)


def grant_signup_voucher(user, commit=True):
    """The welcome discount every new account gets.

    Skipped for anyone who arrived on a referral code: that already granted a
    welcome voucher, and handing out two would let one order take both. The
    caller decides, since it knows whether the referral was applied.
    """
    percent = signup_percent()
    if not percent:
        return None
    settings = get_settings()
    return grant_voucher(
        user, 'signup', percent, settings.get('signup_max_discount'),
        f'Welcome — {percent:g}% off your first print',
        expires_days=int(settings.get('referral_reward_days') or 0),
        commit=commit,
    )


def register_referral(new_user, code):
    """Link a new signup to the inviter and hand the newcomer their welcome bonus.

    Returns (referral, voucher, message). Any of the first two may be None —
    a bad code or a closed campaign is not an error, the signup still succeeds.
    """
    settings = get_settings()
    if not settings['referral_enabled']:
        return None, None, 'The referral programme is not running right now.'

    referrer = find_by_code(code)
    if referrer is None:
        return None, None, 'That referral code was not recognised.'
    if referrer.id == new_user.id:
        return None, None, 'You cannot refer yourself.'
    if Referral.query.filter_by(referred_id=new_user.id).first():
        return None, None, 'This account has already used a referral code.'

    left = slots_left(settings)
    if left is not None and left <= 0:
        return None, None, 'The referral offer has reached its limit.'

    referral = Referral(referrer_id=referrer.id, referred_id=new_user.id,
                        code_used=referrer.referral_code, status='pending')
    new_user.referred_by_id = referrer.id
    db.session.add(referral)

    voucher = grant_voucher(
        new_user, 'referral_friend',
        settings['referral_friend_percent'], settings['referral_max_discount'],
        f"Welcome bonus — {settings['referral_friend_percent']}% off your first print",
        expires_days=settings['referral_reward_days'], commit=False,
    )
    db.session.commit()
    log.info('Referral registered: %s invited %s', referrer.username, new_user.username)
    return referral, voucher, None


def qualify_referral(job):
    """Called when a print completes: pay the inviter for a successful referral.

    A referral qualifies on the invited user's *first* completed print. Once the
    campaign limit is reached the referral is still marked qualified, but no
    reward is issued — so the cap is enforced without losing the history.
    """
    if job is None or not job.user_id:
        return None
    referral = Referral.query.filter_by(referred_id=job.user_id, status='pending').first()
    if referral is None:
        return None

    settings = get_settings()
    referral.status = 'qualified'
    referral.qualified_at = utcnow()

    limit = settings['referral_limit']
    if limit > 0 and rewarded_referral_count() >= limit:
        db.session.commit()
        log.info('Referral %s qualified but the %s-referral cap is reached', referral.id, limit)
        return referral

    referrer = db.session.get(User, referral.referrer_id)
    if referrer is None:
        db.session.commit()
        return referral

    voucher = grant_voucher(
        referrer, 'referral_referrer',
        settings['referral_referrer_percent'], settings['referral_max_discount'],
        f"Referral reward — {settings['referral_referrer_percent']}% off your next print",
        expires_days=settings['referral_reward_days'], commit=False,
    )
    if voucher is None:
        # Inviter is a guest account — qualified, but there is nobody to pay.
        db.session.commit()
        log.info('Referral %s qualified but %s is a guest — no reward issued',
                 referral.id, referrer.username)
        return referral

    db.session.flush()
    referral.reward_voucher_id = voucher.id
    db.session.commit()
    log.info('Referral %s qualified — rewarded %s', referral.id, referrer.username)
    return referral


# --- bulk tiers -----------------------------------------------------------

def active_tiers():
    return BulkDiscountTier.query.filter_by(is_active=True).order_by(
        BulkDiscountTier.min_pages.asc()).all()


def best_tier(pages):
    """Highest tier the page count reaches, or None."""
    best = None
    for tier in active_tiers():
        if pages >= tier.min_pages and (best is None or tier.min_pages > best.min_pages):
            best = tier
    return best


def seed_default_tiers():
    """Create the launch tiers if none exist. Idempotent."""
    if BulkDiscountTier.query.count():
        return 0
    for min_pages, percent in ((25, 10), (50, 15), (100, 20)):
        db.session.add(BulkDiscountTier(min_pages=min_pages, discount_percent=percent))
    db.session.commit()
    return 3


# --- vouchers -------------------------------------------------------------

def available_vouchers(user):
    if user is None or not getattr(user, 'id', None):
        return []
    now = utcnow()
    rows = DiscountVoucher.query.filter_by(user_id=user.id, status='available').all()
    return [v for v in rows if v.is_usable(now)]


def best_voucher(user):
    """The voucher worth the most to this user — highest percent, then highest cap."""
    vouchers = available_vouchers(user)
    if not vouchers:
        return None
    return max(vouchers, key=lambda v: (v.discount_percent, v.max_discount or 1e9))


def expire_stale_vouchers():
    """Flip past-dated vouchers to 'expired'. Returns how many."""
    now = utcnow()
    stale = DiscountVoucher.query.filter(
        DiscountVoucher.status == 'available',
        DiscountVoucher.expires_at.isnot(None),
        DiscountVoucher.expires_at < now,
    ).all()
    for voucher in stale:
        voucher.status = 'expired'
    if stale:
        db.session.commit()
    return len(stale)


# --- quoting --------------------------------------------------------------

def is_eligible(user):
    """Whether this customer can earn or spend an offer.

    Offers are for registered accounts only. A walk-in can still print — the
    counter creates them a temporary guest account — but a guest earns no bulk
    discount, no voucher and no referral reward. Anonymous callers (no user at
    all) are treated the same way.
    """
    if user is None:
        return False
    if not getattr(user, 'is_authenticated', True):
        return False
    return not bool(getattr(user, 'is_guest', False))


def _capped(amount, cap):
    amount = round(amount, 2)
    if cap:
        return min(amount, float(cap))
    return amount


def quote(base_cost, pages, user=None, voucher='auto'):
    """Work out what a job costs after offers.

    `voucher` may be a DiscountVoucher, None to ignore vouchers, or 'auto' to
    pick the user's best one. Returns a dict; `total` is what to charge.
    """
    settings = get_settings()
    base_cost = round(float(base_cost or 0), 2)
    result = {
        'base_cost': base_cost,
        'total': base_cost,
        'discount_amount': 0.0,
        'bulk_amount': 0.0,
        'bulk_tier': None,
        'voucher': None,
        'voucher_amount': 0.0,
        'lines': [],
        'label': None,
        'eligible': True,
    }
    if base_cost <= 0:
        return result

    # Guests and walk-ins print at full price — no tier, no voucher.
    if not is_eligible(user):
        result['eligible'] = False
        return result

    if voucher == 'auto':
        voucher = best_voucher(user) if user is not None else None
    if voucher is not None and not voucher.is_usable():
        voucher = None

    tier = best_tier(pages) if settings['bulk_enabled'] else None
    bulk_amount = _capped(base_cost * tier.discount_percent / 100.0,
                          tier.max_discount) if tier else 0.0

    voucher_amount = 0.0
    if voucher is not None:
        basis = base_cost - bulk_amount if settings['offers_stack'] else base_cost
        voucher_amount = _capped(basis * voucher.discount_percent / 100.0,
                                 voucher.max_discount)

    if not settings['offers_stack'] and tier and voucher is not None:
        # Only one offer per order — apply whichever saves more, keep the other.
        if bulk_amount >= voucher_amount:
            voucher, voucher_amount = None, 0.0
        else:
            tier, bulk_amount = None, 0.0

    total_discount = min(round(bulk_amount + voucher_amount, 2), base_cost)

    lines = []
    if tier and bulk_amount > 0:
        lines.append({'label': tier.display_label, 'amount': bulk_amount})
    if voucher is not None and voucher_amount > 0:
        lines.append({'label': voucher.description or
                      f'{voucher.discount_percent:g}% off voucher',
                      'amount': voucher_amount})

    result.update({
        'total': round(base_cost - total_discount, 2),
        'discount_amount': total_discount,
        'bulk_amount': bulk_amount if tier else 0.0,
        'bulk_tier': tier,
        'voucher': voucher if voucher_amount > 0 else None,
        'voucher_amount': voucher_amount,
        'lines': lines,
        'label': '; '.join(f"{l['label']} (-₹{l['amount']:.2f})" for l in lines) or None,
    })
    return result


def quote_job(job, user=None, voucher='auto'):
    """Quote straight from a PrintJob, using its own page maths."""
    from app.services.pricing import calculate_job_cost
    from app.services.print_options import effective_pages

    base = calculate_job_cost(job)
    pages = effective_pages(job) * (job.copies or 1)
    return quote(base, pages, user=user if user is not None else job.user, voucher=voucher)


def apply_to_job(job, user=None, voucher='auto', consume=True, commit=True):
    """Price a job with offers applied and (optionally) spend the voucher.

    Sets `cost`, `base_cost`, `discount_amount`, `discount_label` and links the
    voucher. Pass `voucher=None` for bulk tiers only. Returns the quote dict.
    """
    release_voucher(job, commit=False)  # re-pricing must not strand a voucher
    q = quote_job(job, user=user, voucher=voucher)

    job.base_cost = q['base_cost']
    job.cost = q['total']
    job.discount_amount = q['discount_amount']
    job.discount_label = q['label']

    voucher = q['voucher']
    if voucher is not None and consume:
        voucher.status = 'used'
        voucher.used_at = utcnow()
        voucher.used_job_id = job.id
        job.voucher_id = voucher.id
    if commit:
        db.session.commit()
    return q


def release_voucher(job, commit=True):
    """Hand a voucher back when its job is cancelled, failed or re-priced."""
    if not job.voucher_id:
        return False
    voucher = db.session.get(DiscountVoucher, job.voucher_id)
    job.voucher_id = None
    if voucher is not None and voucher.status == 'used':
        voucher.status = 'available'
        voucher.used_at = None
        voucher.used_job_id = None
    if commit:
        db.session.commit()
    return True


def user_summary(user):
    """Everything the dashboard needs to show a user their offers.

    A guest still gets the tier list — so they can see what registering would
    be worth — but no code, no vouchers, and `eligible` False so the page can
    say plainly that the discounts need an account.
    """
    settings = get_settings()
    if not is_eligible(user):
        return {
            'settings': settings,
            'eligible': False,
            'code': None,
            'vouchers': [],
            'tiers': active_tiers() if settings['bulk_enabled'] else [],
            'referral_count': 0,
            'referral_qualified': 0,
            'slots_left': slots_left(settings),
        }

    referrals = Referral.query.filter_by(referrer_id=user.id).all()
    return {
        'settings': settings,
        'eligible': True,
        'code': ensure_referral_code(user),
        'vouchers': available_vouchers(user),
        'tiers': active_tiers() if settings['bulk_enabled'] else [],
        'referral_count': len(referrals),
        'referral_qualified': sum(1 for r in referrals if r.status == 'qualified'),
        'slots_left': slots_left(settings),
    }


def admin_stats():
    """Campaign health for the admin offers page."""
    settings = get_settings()
    return {
        'pending': Referral.query.filter_by(status='pending').count(),
        'qualified': Referral.query.filter_by(status='qualified').count(),
        'rewarded': rewarded_referral_count(),
        'slots_left': slots_left(settings),
        'vouchers_available': DiscountVoucher.query.filter_by(status='available').count(),
        'vouchers_used': DiscountVoucher.query.filter_by(status='used').count(),
        'total_saved': round(
            db.session.query(db.func.coalesce(db.func.sum(PrintJob.discount_amount), 0.0))
            .filter(PrintJob.status == 'completed').scalar() or 0.0, 2),
    }
