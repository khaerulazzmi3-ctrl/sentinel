#!/usr/bin/env python3
"""SENTINEL ARENA launcher.

    python run.py                 # http://127.0.0.1:8787
    python run.py --public        # bind 0.0.0.0 (LAN / behind a tunnel)
    python run.py --port 9000
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from server.app import serve  # noqa: E402

if __name__ == "__main__":
    serve(sys.argv[1:])
