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
the time each took, the NICo releases grouped as core and rest, what the
current step is doing inside (image 2/3, release 7/12 …), how to reach the
VM and the cluster, the URL at the end, and the resume command after a
failure. Colour on a terminal (NO_COLOR / FORCE_COLOR honoured); --once and
pipes are plain.
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

# ── colour (same rules as the runner: terminal only, NO_COLOR / FORCE_COLOR) ──
COLOR = {'on': False}


def paint(code, s):
    return f'\033[{code}m{s}\033[0m' if COLOR['on'] and s else s


def green(s):  return paint('32', s)
def red(s):    return paint('1;31', s)
def yellow(s): return paint('33', s)
def cyan(s):   return paint('36', s)
def dim(s):    return paint('2', s)
def bold(s):   return paint('1', s)


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
        if e['kind'] == 'plan':
            # A resume (--from) appends a new plan to the same file: the
            # earlier attempt's verdict no longer describes the run.
            failed = finished = None
        elif e['kind'] == 'start':
            if failed and failed.get('step') == k:
                failed = None          # the failed step is being retried
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
    n_done = sum(1 for s in state.values() if s['status'] in ('done', 'earlier'))
    head = (bold(f'bring-up {plan["name"]}') + f' — {plan["dc"]}/{plan["site"]} — {plan["mode"]}'
            f' — started {started}, {hms(elapsed)} — {n_done}/{len(steps)} steps done')
    if finished:
        head += '  ' + green('✓ FINISHED')
    elif failed:
        head += '  ' + red('✗ FAILED')
    L = [head, '']
    marks = {'done': green('✓'), 'earlier': green('✓'), 'running': yellow('▶'),
             'failed': red('✗'), 'todo': dim('○')}
    kw = max(9, max(len(k) for k in keys))          # key column width
    group = None
    for i, s in enumerate(steps, 1):
        g = s.get('group') or ''
        if g != group:
            group = g
            if g:
                n_in = sum(1 for x in steps if x.get('group') == g)
                what = 'the platform releases and nico' if g == 'core' else 'the REST stack'
                L.append(dim(f'    ── {g}: {what} ({n_in} releases) ──'))
        st = state[s['key']]
        if st['status'] == 'running':
            dur = hms(now - st['start'])
        elif st['secs'] is not None:
            dur = hms(st['secs'])
        elif st['status'] == 'earlier':
            dur = 'earlier run'
        else:
            dur = ''
        desc = s['desc'].split(': ', 1)[1] if g and s['desc'].startswith(g + ': ') else s['desc']
        key = f'{"  " if g else ""}{s["key"]}'.ljust(kw + 2)   # releases indented, marks aligned
        row = f'{i:>3} {key} {marks[st["status"]]}  {dur:>11}   '
        sub = f'{"":>3} {"":<{kw + 2}}    {"":>11}   '
        if st['status'] == 'running':
            L.append(row + yellow(desc + '   ◀ current'))
            if st['stage']:
                L.append(sub + yellow(f'↳ {st["stage"]}'))
        elif st['status'] == 'failed':
            L.append(row + red(desc))
            if st['stage']:
                L.append(sub + red(f'↳ failed during: {st["stage"]}'))
        elif st['status'] == 'todo':
            L.append(row + dim(desc))
        else:
            L.append(row + desc)
    L.append('')

    def ready_after(step):
        """The step that provides an access line is done (or was done by an earlier run)."""
        return state.get(step, {}).get('status') in ('done', 'earlier')

    def access(label, text, step, what):
        if ready_after(step):
            L.append(cyan(f'{label:<10}') + text)
        else:
            L.append(dim(f'{label:<10}{what} — not ready, after step {step}'))

    access('ssh:', f'ssh {plan["user"]}@{plan["ip"]}', 'prep', f'the VM at {plan["ip"]}')
    access('site:', plan['site_dir'], 'site', 'the site folder')
    access('kubectl:', f'KUBECONFIG={plan["kubeconfig"]} kubectl get pods -A', 'cp', 'the cluster')
    if finished:
        L.append(cyan('admin UI: ') + green(plan['admin_url']))
    else:
        L.append(dim(f'{"admin UI:":<10}{plan["admin_url"]} — not ready, after step route'))
    if failed:
        L.append('')
        L.append(red(f'✗ step {failed["step"]} failed (exit {failed["rc"]})') +
                 ' — the runner printed the recovery notes; resume with:')
        L.append(bold(f'    {failed["resume"]}'))
    return L


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument('--config', help='the bringup yaml given to bring-up.py (name and share come from it)')
    p.add_argument('--name', help='VM name (bringup.yaml name:)')
    p.add_argument('--share', default=None, help=f'share folder (default: from --config, else {DEF_SHARE})')
    p.add_argument('--file', default=None, help='the progress file itself (overrides the above)')
    p.add_argument('--interval', type=float, default=2.0, help='redraw interval in seconds (default 2)')
    p.add_argument('--once', action='store_true', help='print once, plain, and exit')
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
        COLOR['on'] = bool(os.environ.get('FORCE_COLOR')) and not os.environ.get('NO_COLOR')
        print('\n'.join(render(load(path), path)))
        return
    COLOR['on'] = not os.environ.get('NO_COLOR')
    try:
        while True:
            lines = render(load(path), path)
            sys.stdout.write('\033[H\033[J' + '\n'.join(lines) + '\n\n'
                             + dim(f'refresh {a.interval:g}s — Ctrl-C to stop (the bring-up keeps running)') + '\n')
            sys.stdout.flush()
            time.sleep(a.interval)
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
