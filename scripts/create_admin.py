#!/usr/bin/env python3
"""Create an admin user for PrintFlow."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bcrypt
from app import create_app
from app.extensions import db
from app.models import User


def create_admin():
    app = create_app()
    with app.app_context():
        username = input('Admin username: ').strip().lower()
        if not username:
            print('Username required.')
            return

        if User.query.filter_by(username=username).first():
            print(f'User "{username}" already exists.')
            make_admin = input('Make them admin? (y/n): ').strip().lower()
            if make_admin == 'y':
                user = User.query.filter_by(username=username).first()
                user.is_admin = True
                db.session.commit()
                print(f'"{username}" is now an admin.')
            return

        email = input('Email: ').strip().lower()
        full_name = input('Full name: ').strip()
        password = input('Password: ').strip()

        if len(password) < 6:
            print('Password must be at least 6 characters.')
            return

        pw_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        user = User(
            username=username,
            email=email,
            full_name=full_name,
            password_hash=pw_hash,
            is_admin=True,
        )
        db.session.add(user)
        db.session.commit()
        print(f'Admin user "{username}" created successfully.')


if __name__ == '__main__':
    create_admin()
