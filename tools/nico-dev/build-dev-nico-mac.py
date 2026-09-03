#!/usr/bin/env python3
"""Compatibility shim — the build script is now build-dev-nico.py.

It was named for the Mac before nico-dev ran on Linux hosts; the code was
always host-arch aware. This shim forwards every argument and will be
removed in a later release.
"""
import os
import sys

target = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'build-dev-nico.py')
print('note: build-dev-nico-mac.py is now build-dev-nico.py (same script, any host) '
      '— update your command.', file=sys.stderr)
os.execv(sys.executable, [sys.executable, target] + sys.argv[1:])
