"""Lightweight, idempotent ALTER TABLE compat layer.

We use Flask-Migrate for the source of truth, but production users may upgrade
the code without running migrations. This module adds any missing columns at
startup so the new code never crashes against an old DB. New tables are
created by db.create_all() in the app factory.
"""
import logging
from sqlalchemy import inspect, text

log = logging.getLogger(__name__)


# (table, column, SQL type spec)
COLUMN_ADDITIONS = [
    # users
    ('users', 'failed_login_count', 'INTEGER DEFAULT 0'),
    ('users', 'locked_until', 'DATETIME'),
    ('users', 'password_changed_at', 'DATETIME'),
    ('users', 'last_login_at', 'DATETIME'),
    ('users', 'is_guest', 'BOOLEAN DEFAULT 0'),
    ('users', 'guest_expires_at', 'DATETIME'),
    ('users', 'referral_code', 'VARCHAR(16)'),
    ('users', 'referred_by_id', 'INTEGER'),

    # print_jobs
    ('print_jobs', 'cost_locked', 'BOOLEAN DEFAULT 0'),
    ('print_jobs', 'last_status_at', 'DATETIME'),
    ('print_jobs', 'retry_count', 'INTEGER DEFAULT 0'),
    ('print_jobs', 'printer_id', 'VARCHAR(64)'),
    ('print_jobs', 'claimed_by_agent', 'VARCHAR(64)'),
    ('print_jobs', 'claimed_at', 'DATETIME'),
    ('print_jobs', 'preview_status', "VARCHAR(20) DEFAULT 'pending'"),
    ('print_jobs', 'preview_pages', 'INTEGER DEFAULT 0'),
    ('print_jobs', 'failed_reason', 'VARCHAR(500)'),
    ('print_jobs', 'receipt_filename', 'VARCHAR(255)'),
    ('print_jobs', 'files_purged_at', 'DATETIME'),
    ('print_jobs', 'base_cost', 'FLOAT DEFAULT 0'),
    ('print_jobs', 'discount_amount', 'FLOAT DEFAULT 0'),
    ('print_jobs', 'discount_label', 'VARCHAR(200)'),
    ('print_jobs', 'voucher_id', 'INTEGER'),
    ('print_jobs', 'paper_cost', 'FLOAT DEFAULT 0'),
    ('print_jobs', 'ink_cost', 'FLOAT DEFAULT 0'),
    ('print_jobs', 'sheets_used', 'INTEGER DEFAULT 0'),
    ('print_jobs', 'impressions_used', 'INTEGER DEFAULT 0'),
    ('print_jobs', 'page_ranges', 'VARCHAR(120)'),
    ('print_jobs', 'pages_per_sheet', 'INTEGER DEFAULT 1'),
    ('print_jobs', 'page_set', "VARCHAR(10) DEFAULT 'all'"),
    ('print_jobs', 'output_order', "VARCHAR(10) DEFAULT 'normal'"),
    ('print_jobs', 'orientation', "VARCHAR(20) DEFAULT 'auto'"),
    ('print_jobs', 'fit_to_page', 'BOOLEAN DEFAULT 0'),
    ('print_jobs', 'print_quality', "VARCHAR(10) DEFAULT 'normal'"),
    ('print_jobs', 'collate', 'BOOLEAN DEFAULT 1'),

    # kiosk_state
    ('kiosk_state', 'token_expires_at', 'DATETIME'),

    # agent_status (multi-printer)
    ('agent_status', 'printer_id', 'VARCHAR(64)'),
    ('agent_status', 'agent_version', 'VARCHAR(40)'),
    ('agent_status', 'mac_address', 'VARCHAR(32)'),
    ('agent_status', 'hostname', 'VARCHAR(120)'),
    ('agent_status', 'ip_address', 'VARCHAR(64)'),
    ('agent_status', 'platform', 'VARCHAR(160)'),
    ('agent_status', 'activity', 'VARCHAR(20)'),
    ('agent_status', 'active_job_count', 'INTEGER DEFAULT 0'),
    ('agent_status', 'last_error', 'VARCHAR(300)'),
    ('agent_status', 'agent_started_at', 'DATETIME'),
    ('agent_status', 'kiosk_last_seen', 'DATETIME'),
    ('agent_status', 'key_hash', 'VARCHAR(64)'),
    ('agent_status', 'key_prefix', 'VARCHAR(12)'),
    ('agent_status', 'enrolled_at', 'DATETIME'),
    ('agent_status', 'revoked_at', 'DATETIME'),

    # advertisements (rich media)
    ('advertisements', 'media_type', "VARCHAR(16) DEFAULT 'text'"),
    ('advertisements', 'media_filename', 'VARCHAR(255)'),
    ('advertisements', 'media_mime', 'VARCHAR(100)'),
    ('advertisements', 'media_pages', 'INTEGER DEFAULT 0'),
    ('advertisements', 'html_content', 'TEXT'),
    ('advertisements', 'link_url', 'VARCHAR(500)'),
    ('advertisements', 'duration_seconds', 'INTEGER DEFAULT 6'),
    ('advertisements', 'display_order', 'INTEGER DEFAULT 0'),

    # payment_ledger
    ('payment_ledger', 'entry_type', "VARCHAR(20) DEFAULT 'charge'"),

    # pricing — separate rate for duplex sheets. Added as a nullable column so
    # the existing uq_pricing_size_color constraint is untouched; NULL means
    # "same as simplex", which is exactly the old behaviour.
    ('pricing', 'duplex_price_per_page', 'FLOAT'),
]


INDEX_ADDITIONS = [
    ('ix_print_jobs_printer_id', 'print_jobs', 'printer_id'),
    ('ix_print_jobs_status_submitted', 'print_jobs', 'status, submitted_at'),
    ('ix_print_jobs_user_status', 'print_jobs', 'user_id, status'),
    ('ix_agent_status_printer_id', 'agent_status', 'printer_id'),
    ('ix_payment_ledger_user_id', 'payment_ledger', 'user_id'),
    ('ix_payment_ledger_created_at', 'payment_ledger', 'created_at'),
    ('ix_audit_log_created_at', 'audit_log', 'created_at'),
    ('ix_audit_log_action', 'audit_log', 'action'),
    ('ix_pricing_history_changed_at', 'pricing_history', 'changed_at'),
    ('ix_users_referral_code', 'users', 'referral_code'),
    ('ix_discount_vouchers_user_id', 'discount_vouchers', 'user_id'),
    ('ix_discount_vouchers_status', 'discount_vouchers', 'status'),
    ('ix_referrals_referrer_id', 'referrals', 'referrer_id'),
    ('ix_referrals_status', 'referrals', 'status'),
    ('ix_stock_items_kind', 'stock_items', 'kind'),
    ('ix_stock_movements_item', 'stock_movements', 'stock_item_id'),
    ('ix_stock_movements_created_at', 'stock_movements', 'created_at'),
    ('ix_alerts_active', 'alerts', 'is_active'),
    ('ix_alerts_subject', 'alerts', 'subject'),
    ('ix_alerts_created_at', 'alerts', 'created_at'),
    ('ix_agent_status_key_hash', 'agent_status', 'key_hash'),
    ('ix_enrollment_codes_code', 'enrollment_codes', 'code'),
]


def apply_compat_schema(db):
    """Add new columns/indexes that may be missing from older DBs."""
    inspector = inspect(db.engine)

    with db.engine.begin() as conn:
        for table, col, spec in COLUMN_ADDITIONS:
            if not inspector.has_table(table):
                continue
            existing = {c['name'] for c in inspector.get_columns(table)}
            if col in existing:
                continue
            try:
                conn.execute(text(f'ALTER TABLE {table} ADD COLUMN "{col}" {spec}'))
                log.info('Added column %s.%s', table, col)
            except Exception as e:
                log.warning('Failed to add %s.%s: %s', table, col, e)

        # Refresh inspector after column adds
        inspector = inspect(db.engine)
        for idx_name, table, cols in INDEX_ADDITIONS:
            if not inspector.has_table(table):
                continue
            existing_idx = {i['name'] for i in inspector.get_indexes(table)}
            if idx_name in existing_idx:
                continue
            try:
                conn.execute(text(f'CREATE INDEX IF NOT EXISTS {idx_name} ON {table} ({cols})'))
            except Exception as e:
                log.debug('Index %s create skipped: %s', idx_name, e)
