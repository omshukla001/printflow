#!/usr/bin/env python3
"""One-shot DB upgrade helper for production deployments.

Adds the columns/indexes introduced in the security/reliability pass without
needing Flask-Migrate set up. Idempotent — safe to run repeatedly.

Usage:
    venv/bin/python scripts/upgrade_db.py
"""
import os
import sys

# Make project root importable
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)

os.environ.setdefault('FLASK_SKIP_SCHEDULER', '1')

from app import create_app
from app.extensions import db
from app.services.compat import apply_compat_schema


def main():
    app = create_app()
    with app.app_context():
        db.create_all()
        apply_compat_schema(db)
        print('Schema upgrade applied.')


if __name__ == '__main__':
    main()
