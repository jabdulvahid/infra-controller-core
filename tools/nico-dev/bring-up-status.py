#!/usr/bin/env python3
"""
nico-dev — high-level progress of a running (or finished) bring-up, from the
progress file the runner writes (progress.py). Run it in a second terminal
next to bring-up.py; the runner's own output is not changed.

  bring-up-status.py --config bringup-<site>.yaml      # follow, redraw every 2 s
  bring-up-status.py --name nico-vm1 [--share DIR]     # same, by VM name
  bring-up-status.py --config … --once                 # one plain snapshot (paste-friendly)

The file is <share>/.bring-up/<vm-name>.jsonl. With --config, share and name
come from the config the same way the runner derives them; --share overrides.

What it shows: every step with ✓ done / ▶ running / ✗ failed / ○ not yet,
the time each took, what the current step is doing inside (image 2/3,
release 7/12 …), how to reach the VM and the cluster, the URL at the end,
and the resume command after a failure.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

NICO_DEV = Path(__file__).resolve().parent
import importlib.util as _ilu
_spec_p = _ilu.spec_from_file_location('progress', NICO_DEV / 'progress.py')
_progress = _ilu.module_from_spec(_spec_p)
_spec_p.loader.exec_module(_progress)

# Same self-locating default as the runner: <share>/<repo>/tools/nico-dev.
DEF_SHARE = NICO_DEV.parents[2] if NICO_DEV.parent.name == 'tools' else NICO_DEV.parents[1]


def hms(secs):
    secs = int(secs)
    if secs >= 3600:
        return f'{secs // 3600}h{(secs % 3600) // 60:02d}m'
    return f'{secs // 60}m{secs % 60:02d}s'


def load(path):
    events = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass          # a line still being written
    except FileNotFoundError:
        return None
    return events


def render(events, path, now=None):
    now = now or time.time()
    if events is None:
        return [f'no progress file yet: {path}',
                'bring-up.py writes it when it starts its first step; '
                'a run started before this feature has none.']
    plan = next((e for e in events if e['kind'] == 'plan'), None)
    if plan is None:
        return [f'{path}: no plan event yet']
    steps = plan['steps']
    keys = [s['key'] for s in steps]
    state = {k: {'status': 'todo', 'secs': None, 'stage': '', 'start': None} for k in keys}
    # Steps before `first` on a resume were done by an earlier run.
    for k in keys[:keys.index(plan.get('first', keys[0]))]:
        state[k]['status'] = 'earlier'
    finished = None
    failed = None
    for e in events:
        k = e.get('step')
        if e['kind'] == 'start':
            state[k].update(status='running', start=e['ts'], stage='', secs=None)
        elif e['kind'] == 'stage':
            if k in state:
                state[k]['stage'] = e.get('detail', '')
        elif e['kind'] == 'done':
            state[k].update(status='done', secs=e['secs'])
        elif e['kind'] == 'fail':
            state[k].update(status='failed', secs=e['secs'])
            failed = e
        elif e['kind'] == 'finished':
            finished = e
    t0 = plan['ts']
    elapsed = (finished['ts'] if finished else now) - t0
    started = time.strftime('%H:%M', time.localtime(t0))
    head = f'bring-up {plan["name"]} — {plan["dc"]}/{plan["site"]} — {plan["mode"]} — started {started}, {hms(elapsed)}'
    if finished:
        head += '  ✓ FINISHED'
    elif failed:
        head += '  ✗ FAILED'
    L = [head, '']
    marks = {'done': '✓', 'earlier': '✓', 'running': '▶', 'failed': '✗', 'todo': '○'}
    for i, s in enumerate(steps, 1):
        st = state[s['key']]
        mark = marks[st['status']]
        if st['status'] == 'running':
            dur = hms(now - st['start'])
        elif st['secs'] is not None:
            dur = hms(st['secs'])
        elif st['status'] == 'earlier':
            dur = 'earlier run'
        else:
            dur = ''
        line = f'{i:>3} {s["key"]:<9} {mark}  {dur:>11}   {s["desc"]}'
        if st['status'] == 'running':
            line += '   ◀ current'
            if st['stage']:
                L.append(line)
                line = f'{"":>3} {"":<9}    {"":>11}   ↳ {st["stage"]}'
        elif st['status'] == 'failed' and st['stage']:
            L.append(line)
            line = f'{"":>3} {"":<9}    {"":>11}   ↳ failed during: {st["stage"]}'
        L.append(line)
    L.append('')
    L.append(f'ssh:      ssh {plan["user"]}@{plan["ip"]}')
    L.append(f'site:     {plan["site_dir"]}')
    L.append(f'kubectl:  KUBECONFIG={plan["kubeconfig"]} kubectl get pods -A')
    L.append(f'done:     {plan["admin_url"]}')
    if failed:
        L.append('')
        L.append(f'✗ step {failed["step"]} failed (exit {failed["rc"]}) — the runner printed the '
                 f'recovery notes; resume with:')
        L.append(f'    {failed["resume"]}')
    return L


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument('--config', help='the bringup yaml given to bring-up.py (name and share come from it)')
    p.add_argument('--name', help='VM name (bringup.yaml name:)')
    p.add_argument('--share', default=None, help=f'share folder (default: from --config, else {DEF_SHARE})')
    p.add_argument('--file', default=None, help='the progress file itself (overrides the above)')
    p.add_argument('--interval', type=float, default=2.0, help='redraw interval in seconds (default 2)')
    p.add_argument('--once', action='store_true', help='print once and exit')
    a = p.parse_args()

    name, share = a.name, a.share
    if a.config:
        try:
            import yaml
            cfg = yaml.safe_load(open(os.path.expanduser(a.config))) or {}
        except ImportError:
            sys.exit('pyyaml is needed to read --config; pass --name and --share instead')
        name = name or cfg.get('name')
        share = share or cfg.get('share')
    if a.file:
        path = Path(os.path.expanduser(a.file))
    else:
        if not name:
            sys.exit('give --config, --name or --file')
        path = _progress.path_for(share or DEF_SHARE, name)

    if a.once or not sys.stdout.isatty():
        print('\n'.join(render(load(path), path)))
        return
    try:
        while True:
            lines = render(load(path), path)
            sys.stdout.write('\033[H\033[J' + '\n'.join(lines) + '\n\n'
                             f'refresh {a.interval:g}s — Ctrl-C to stop (the bring-up keeps running)\n')
            sys.stdout.flush()
            time.sleep(a.interval)
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
