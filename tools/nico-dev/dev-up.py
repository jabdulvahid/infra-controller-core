#!/usr/bin/env python3
"""Platform dispatcher — implementations: dev-up_mac.py / dev-up_linux.py."""
import os
import platform
import sys

base = os.path.splitext(os.path.realpath(__file__))[0]
suffix = {'Darwin': '_mac', 'Linux': '_linux'}.get(platform.system())
if not suffix:
    sys.exit(f'Unsupported host platform: {platform.system()}')
target = f'{base}{suffix}.py'
if not os.path.exists(target):
    sys.exit(f'{os.path.basename(target)} is not implemented yet '
             f'on this platform')
os.environ['NICO_DEV_ENTRY'] = os.path.realpath(__file__)
os.execv(sys.executable, [sys.executable, target] + sys.argv[1:])
