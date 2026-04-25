"""Thin wrapper that simply starts :mod:`app.main`.

Offered for discoverability; you can also run ``python -m app.main``.
"""
from app.main import main

if __name__ == "__main__":
    main()
