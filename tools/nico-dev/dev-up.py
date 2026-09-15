#!/usr/bin/env python3
"""Compatibility shim: dev-up.py was renamed bring-up.py (2026-09-15).
Runs bring-up.py with the same arguments. Update your notes; this shim will
go away with a later validated tag."""
import os
import sys

print('note: dev-up.py is now bring-up.py — running bring-up.py', file=sys.stderr)
target = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'bring-up.py')
os.execv(sys.executable, [sys.executable, target] + sys.argv[1:])
