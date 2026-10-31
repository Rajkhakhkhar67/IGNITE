"""Database package for Learning Platform."""

from database.db import close_db, get_db, init_db

__all__ = ["close_db", "get_db", "init_db"]
