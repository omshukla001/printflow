import logging
import os
from flask import Flask, render_template
from config import Config
from app.extensions import db, login_manager, csrf, limiter, migrate


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    # Logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    )

    # Fail fast on insecure production config
    if hasattr(config_class, 'validate'):
        config_class.validate()

    # Ensure directories exist
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    os.makedirs(app.config['PREVIEW_FOLDER'], exist_ok=True)
    os.makedirs(app.config.get('RECEIPT_FOLDER',
                                os.path.join(os.path.dirname(app.config['UPLOAD_FOLDER']),
                                             'receipts')), exist_ok=True)
    os.makedirs(os.path.join(os.path.dirname(app.config['UPLOAD_FOLDER']), 'ads'), exist_ok=True)
    db_uri = app.config['SQLALCHEMY_DATABASE_URI']
    if db_uri.startswith('sqlite:///'):
        os.makedirs(os.path.dirname(db_uri.replace('sqlite:///', '')), exist_ok=True)

    # Init extensions
    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    migrate.init_app(app, db, directory=os.path.join(os.path.dirname(__file__), '..', 'migrations'))

    # Limiter — configure storage from app config, apply defaults
    limiter.init_app(app)
    limiter.default_limits = [app.config.get('RATE_LIMIT_DEFAULT', '200 per minute')]

    @login_manager.user_loader
    def load_user(user_id):
        from app.models import User
        return db.session.get(User, int(user_id))

    # Register blueprints
    from app.routes.auth import auth_bp
    from app.routes.user import user_bp
    from app.routes.admin import admin_bp
    from app.routes.api import api_bp
    from app.routes.kiosk import kiosk_bp, checkin_bp
    from app.routes.agent import agent_bp
    from app.routes.install import install_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(user_bp, url_prefix='/user')
    app.register_blueprint(admin_bp, url_prefix='/admin')
    app.register_blueprint(api_bp, url_prefix='/api')
    app.register_blueprint(kiosk_bp)
    app.register_blueprint(checkin_bp)
    app.register_blueprint(agent_bp, url_prefix='/api/agent')
    app.register_blueprint(install_bp)

    # API endpoints are intended for JS clients with same-origin cookies; they
    # are CSRF-protected when state-changing. SSE and read-only routes are GET
    # so CSRF doesn't apply.

    # Error handlers
    @app.errorhandler(404)
    def not_found(e):
        return render_template('errors/404.html'), 404

    @app.errorhandler(413)
    def too_large(e):
        from flask import jsonify, request
        if request.path.startswith('/api/'):
            return jsonify({'error': 'File too large'}), 413
        return render_template('errors/500.html',
                               message='File too large'), 413

    @app.errorhandler(429)
    def too_many(e):
        from flask import jsonify, request
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Too many requests'}), 429
        return render_template('errors/500.html',
                               message='Too many requests — please slow down.'), 429

    @app.errorhandler(500)
    def server_error(e):
        return render_template('errors/500.html'), 500

    # Schema setup
    with app.app_context():
        db.create_all()
        from app.services.compat import apply_compat_schema
        apply_compat_schema(db)
        _seed_pricing()
        from app.services.offers import seed_default_tiers
        seed_default_tiers()
        from app.services.stock import seed_defaults as seed_stock
        seed_stock()

    # Async preview worker
    from app.services import preview_worker
    preview_worker.start(app)

    # Watchdog scheduler (skip when running tests or migrations)
    if not app.config.get('TESTING', False) and os.environ.get('FLASK_SKIP_SCHEDULER') != '1':
        from app.services.watchdog import start_scheduler
        start_scheduler(app)

    # CUPS monitor (only in local mode)
    if not app.config.get('CLOUD_MODE', False) and os.environ.get('FLASK_SKIP_SCHEDULER') != '1':
        from app.services.queue_manager import start_cups_monitor
        start_cups_monitor(app)

    return app


# (paper, colour, simplex sheet price, duplex sheet price)
# Colour is simplex-only, so its duplex rate is never reached — it mirrors the
# simplex rate so any fallback path still charges the right amount.
PRICING_DEFAULTS = [
    ('A4', 'bw', 2.0, 3.0),
    ('A4', 'color', 5.0, 5.0),
    ('A3', 'bw', 3.0, 4.0),
    ('A3', 'color', 25.0, 25.0),
    ('Letter', 'bw', 2.0, 3.0),
    ('Letter', 'color', 5.0, 5.0),
]


def _seed_pricing():
    """Seed pricing on a fresh DB, and backfill duplex rates on an existing one.

    Only ever fills a duplex rate that is NULL — an admin's edited prices are
    never overwritten on boot.
    """
    from app.models import Pricing
    if Pricing.query.count() == 0:
        for paper, color, price, duplex in PRICING_DEFAULTS:
            db.session.add(Pricing(paper_size=paper, color_mode=color,
                                   price_per_page=price,
                                   duplex_price_per_page=duplex))
        db.session.commit()
        return

    lookup = {(p, c): d for p, c, _, d in PRICING_DEFAULTS}
    changed = False
    for row in Pricing.query.filter(Pricing.duplex_price_per_page.is_(None)).all():
        # Fall back to the simplex price if this paper/colour isn't a known default.
        row.duplex_price_per_page = lookup.get((row.paper_size, row.color_mode),
                                               row.price_per_page)
        changed = True
    if changed:
        db.session.commit()
