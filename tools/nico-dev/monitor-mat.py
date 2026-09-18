#!/usr/bin/env python3
"""
nico-dev — watch a MAT run: NICo's view (admin CLI) and MAT's view (its logs),
refreshed every 30 s, in a full-screen text UI or as plain text.

  monitor-mat.py --admin-cli <site>/run-admin-cli.sh \\
                 [--mat-log /var/log/machine-a-tron-dc1-base.log ...] \\
                 [--interval 30] [--once] [--no-tui]

  --admin-cli   the site's run-admin-cli.sh wrapper (or a nico-admin-cli on PATH
                with API_URL/*_PATH already exported). Required.
  --mat-log     a MAT log file; repeatable; optional. Without it the monitor
                shows the server side only. Unreadable files are reported, not
                fatal (MAT logs under /var/log are root-owned: run with sudo).
  --interval    seconds between refreshes (default 30).
  --once        one refresh, plain text, exit (for scripts and pasting).
  --no-tui      plain text every interval instead of the curses screen.

What it shows
  1. Expected machines registered with NICo (count; MAT registers its hosts).
  2. Site-explorer endpoints: address, type, vendor, pre-ingestion state, the
     machine each became, the last exploration error.
  3. Machines: state as NICo reports it, the lifecycle milestone that state
     belongs to, how many milestones remain to Ready, and Ready/Failed marks.
  4. MAT's own view per mock host and DPU, from the log: MAT FSM state, the
     API state MAT last observed, the OS it booted, the last timer it armed.
     (Every MAT log line inside a machine's iteration span ends with
     mat_host_id=… [dpu_index=…] api_state=… state=… booted_os=…; the monitor
     keeps the latest per machine.)

Milestones, from docs/architecture/state_machines/managedhost.md: a managed
host walks Created → DpuDiscovering → DPUInitializing → HostInitializing →
[BomValidating → Validation → Measuring, when enabled] → Ready. The state
string's prefix (before "/") names the milestone; the part after it is the
sub-state. "to go" counts milestones on the path, so it is an upper bound
when the optional ones are disabled.
"""

import argparse
import curses
import os
import re
import subprocess
import sys
import time
from collections import OrderedDict
from datetime import datetime

# ── the lifecycle path (top-level milestones, in order) ──────────────────────
MILESTONES = ['Created', 'DpuDiscovering', 'DPUInitializing', 'HostInitializing',
              'BomValidating', 'Validation', 'Measuring', 'Ready']
OPTIONAL = {'BomValidating', 'Validation', 'Measuring'}
END_STATE = 'Ready'


SUB_RE = re.compile(r'\{\s*state:\s*([A-Za-z_]+)', re.I)


def compact(state):
    """Shorter, readable form of a NICo state string for the table."""
    m = SUB_RE.search(state)
    top = state.split('/')[0]
    canon = next((x for x in MILESTONES if x.lower() == top.lower()), top)
    if m:
        inner = m.group(1)
        return f'{canon}/Dpf:{inner}' if 'dpfstates' in state.lower() else f'{canon}/{inner}'
    rest = state.split('/', 1)[1] if '/' in state else ''
    return f'{canon}/{rest}' if rest else canon


def milestone_of(state):
    """(milestone, sub_state) for a NICo state string like 'HostInitializing/Discovered'."""
    top, _, sub = state.partition('/')
    top = top.strip()
    for m in MILESTONES:
        if top.lower().startswith(m.lower()):
            return m, sub
    return top, sub          # Failed, Decommissioning, Assigned, … : off the ingestion path


def to_go(state):
    """Milestones left to Ready as text: '0', '≤3' (optional ones may be skipped), or '-'."""
    m, _ = milestone_of(state)
    if m not in MILESTONES:
        return '-'
    i = MILESTONES.index(m)
    rest = MILESTONES[i + 1:]
    mandatory = [r for r in rest if r not in OPTIONAL]
    if len(rest) == len(mandatory):
        return str(len(rest))
    return f'{len(mandatory)}-{len(rest)}'


# ── admin CLI ────────────────────────────────────────────────────────────────
def run_cli(admin_cli, args, timeout=60):
    """Run the admin CLI; returns (stdout, error-or-None). Never raises."""
    try:
        r = subprocess.run([admin_cli] + args, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return '', f'{admin_cli}: not found'
    except subprocess.TimeoutExpired:
        return '', f'{" ".join(args)}: timed out after {timeout}s'
    if r.returncode != 0:
        err = (r.stderr or r.stdout).strip().splitlines()
        return r.stdout, (err[-1][:160] if err else f'exit {r.returncode}')
    return r.stdout, None


def parse_table(text):
    """Parse a prettytable-style table (| cells |, +---+ borders) into a list of
    dicts keyed by lower-cased header. Tolerates box-drawing borders too. A
    wrapped cell continues on a following line whose key column (the first
    non-empty header, e.g. Id or Address) is empty; those lines are merged
    into the row above instead of counted as rows."""
    rows = []
    header = None
    key_idx = None
    for raw in text.splitlines():
        line = ANSI_RE.sub('', raw).strip()
        if not line or set(line) <= set('+-=|│┼─┌┐└┘├┤┬┴ '):
            continue
        if '|' in line:
            cells = [c.strip() for c in line.strip('|').split('|')]
        elif '│' in line:
            cells = [c.strip() for c in line.strip('│').split('│')]
        else:
            continue
        if header is None:
            header = [c.lower() for c in cells]
            key_idx = next((i for i, h in enumerate(header) if h), 0)
            continue
        if len(cells) == len(header) and rows and not cells[key_idx]:
            for i, c in enumerate(cells):            # continuation of the previous row
                if c:
                    rows[-1][header[i]] = (rows[-1].get(header[i], '') + c).strip()
            continue
        if len(cells) != len(header):
            # a wrapped continuation line: append to the previous row's cells
            if rows and len(cells) == len(header):
                pass
            elif rows:
                for i, c in enumerate(cells[:len(header)]):
                    if c:
                        key = header[i]
                        rows[-1][key] = (rows[-1].get(key, '') + ' ' + c).strip()
            continue
        rows.append(dict(zip(header, cells)))
    return rows


def col(row, *names, default=''):
    """First matching column by (case-insensitive, space-insensitive) name."""
    norm = {k.replace(' ', '').lower(): v for k, v in row.items()}
    for n in names:
        v = norm.get(n.replace(' ', '').lower())
        if v is not None:
            return v
    return default


def fetch_server(admin_cli):
    """One refresh of NICo's view. Returns a dict; errors are strings inside it."""
    out = {'errors': []}
    text, err = run_cli(admin_cli, ['expected-machine', 'show'])
    out['expected'] = parse_table(text) if text else []
    if err:
        out['errors'].append(f'expected-machine show: {err}')

    text, err = run_cli(admin_cli, ['site-explorer', 'get-report', 'endpoint'])
    out['endpoints'] = parse_table(text) if text else []
    if err:
        out['errors'].append(f'site-explorer get-report endpoint: {err}')

    text, err = run_cli(admin_cli, ['machine', 'show'])
    out['machines'] = parse_table(text) if text else []
    if err:
        out['errors'].append(f'machine show: {err}')
    return out


# ── MAT logs ─────────────────────────────────────────────────────────────────
ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
KV_RE = re.compile(r'(\bmat_host_id|\bdpu_index|\bapi_state|\bstate|\bbooted_os)=(\S+)')
TIMER_RE = re.compile(r'Timer armed: (\w+) \((\w+)\)')
DUR_RE = re.compile(r'duration=(\S+)')
TS_RE = re.compile(r'^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)')
FSM_RE = re.compile(r'next_state=MachineFsm \{ state: (\w+)')


class MatLog:
    """Incremental reader of one MAT log: keeps the latest span fields per
    (mat_host_id, dpu_index|host), the last timer armed, and a few counters."""

    def __init__(self, path):
        self.path = path
        self.pos = 0
        self.error = None
        self.machines = OrderedDict()   # key → dict(api_state, state, booted_os, timer, ts)
        self.lines = 0
        self.firmware_noise = 0
        self.first_ts = None
        self.last_ts = None

    def refresh(self):
        try:
            size = os.path.getsize(self.path)
        except OSError as e:
            self.error = f'{self.path}: {e.strerror}'
            return
        if size < self.pos:           # rotated or restarted: start over
            self.pos = 0
            self.machines.clear()
        try:
            with open(self.path, 'r', errors='replace') as f:
                f.seek(self.pos)
                for line in f:
                    self.pos += len(line.encode('utf-8', 'replace'))
                    self._ingest(line.rstrip('\n'))
            self.error = None
        except PermissionError:
            self.error = f'{self.path}: permission denied (run with sudo)'
        except OSError as e:
            self.error = f'{self.path}: {e.strerror}'

    def _ingest(self, line):
        self.lines += 1
        line = ANSI_RE.sub('', line)
        m = TS_RE.match(line)
        ts = m.group(1) if m else None
        if ts:
            self.first_ts = self.first_ts or ts
            self.last_ts = ts
        if 'Desired firmware versions changed' in line:
            self.firmware_noise += 1
            return
        kv = dict(KV_RE.findall(line))
        host = kv.get('mat_host_id')
        if not host:
            return
        key = (host, kv.get('dpu_index'))
        rec = self.machines.setdefault(key, {'api_state': '?', 'state': '?', 'booted_os': '?',
                                             'timer': '', 'ts': ''})
        for k in ('api_state', 'state', 'booted_os'):
            if k in kv:
                rec[k] = kv[k].rstrip(',')
        t = TIMER_RE.search(line)
        if t:
            d = DUR_RE.search(line)
            rec['timer'] = f'{t.group(1)} ({t.group(2)}{" " + d.group(1) if d else ""})'
        f = FSM_RE.search(line)
        if f:
            rec['state'] = f.group(1)
        if ts:
            rec['ts'] = ts[11:]


# ── rendering ────────────────────────────────────────────────────────────────
# A screen is a list of lines; a line is a list of (text, style) segments so the
# curses view can colour them (k9s-style: sections blue, title teal, column
# headers dim, states by meaning) while plain text just joins the segments.
TITLE, SECTION, HDR, OK, WIP, BAD, NUM, PLAIN = 'title', 'section', 'hdr', 'ok', 'wip', 'bad', 'num', ''


def short(s, n):
    s = s or ''
    return s if len(s) <= n else s[:max(0, n - 1)] + '…'


def state_style(state):
    st = (state or '').lower()
    if st.startswith('ready') or st in ('machineup', 'complete'):
        return OK
    if st.startswith('failed') or 'error' in st:
        return BAD
    return WIP


def render(server, logs, admin_cli, interval, width=120):
    now = datetime.now().strftime('%H:%M:%S')
    L = []
    machines = server.get('machines', [])
    ready = sum(1 for r in machines if col(r, 'state').lower().startswith('ready'))
    failed = sum(1 for r in machines if col(r, 'state').lower().startswith('failed'))
    hosts = sum(1 for r in machines if 'dpu' not in col(r, 'type').lower())
    L.append([('MAT run monitor', TITLE), (f'  {now}  refresh {interval}s   admin-cli: {short(admin_cli, 60)}', PLAIN)])
    L.append([('expected machines: ', PLAIN), (str(len(server.get('expected', []))), NUM),
              ('    endpoints: ', PLAIN), (str(len(server.get('endpoints', []))), NUM),
              ('    machines: ', PLAIN), (str(len(machines)), NUM), (f' ({hosts} hosts, {len(machines) - hosts} DPUs)', PLAIN),
              ('    Ready: ', PLAIN), (f'{ready}/{len(machines)}', OK if machines and ready == len(machines) else NUM),
              ('    Failed: ', PLAIN), (str(failed), BAD if failed else PLAIN)])
    for e in server.get('errors', []):
        L.append([(f'  ! {e}', BAD)])
    L.append([])

    L.append([('ENDPOINTS (site explorer)', SECTION)])
    L.append([(f'  {"address":<14} {"type":<5} {"vendor":<8} {"pre-ingestion":<14} {"machine":<44} last error', HDR)])
    eps = server.get('endpoints', [])
    if not eps:
        L.append([('  (none yet)', HDR)])
    for r in eps:
        pre = col(r, 'pre-ingestion state', 'preingestionstate')
        err = col(r, 'last exploration error', 'lastexplorationerror')
        L.append([(f'  {short(col(r, "address"), 14):<14} {short(col(r, "type"), 5):<5} {short(col(r, "vendor"), 8):<8} ', PLAIN),
                  (f'{short(pre, 14):<14}', state_style(pre)),
                  (f' {short(col(r, "machineid", "machine id", "machine"), 44):<44} ', PLAIN),
                  (short(err, max(10, width - 96)), BAD if err else PLAIN)])
    L.append([])

    L.append([('MACHINES (NICo)', SECTION), (f'   end state: {END_STATE}   milestones: {" > ".join(MILESTONES)}', PLAIN)])
    L.append([(f'  {"id":<44} {"type":<6} {"milestone":<17} {"to-go":<6} state (full, as NICo reports it)', HDR)])
    if not machines:
        L.append([('  (none yet)', HDR)])
    for r in machines:
        state = col(r, 'state')
        m, _ = milestone_of(state)
        sty = state_style(state)
        mark = ' ✓' if sty == OK else (' ✗' if sty == BAD else '')
        L.append([(f'  {short(col(r, "id"), 44):<44} {short(col(r, "type"), 6):<6} {short(m, 17):<17} ', PLAIN),
                  (f'{to_go(state) + mark:<6}', sty), (' ' + state, sty)])
    L.append([])

    for log in logs:
        head = [(f'MAT ({os.path.basename(log.path)})', SECTION)]
        if log.lines:
            head.append((f'   lines: {log.lines}   span {log.first_ts[11:] if log.first_ts else "?"}–{log.last_ts[11:] if log.last_ts else "?"}', PLAIN))
        if log.firmware_noise:
            head.append((f'   firmware-refresh noise lines: {log.firmware_noise}', HDR))
        L.append(head)
        if log.error:
            L.append([(f'  ! {log.error}', BAD)])
        elif not log.machines:
            L.append([('  (no machine iteration lines yet)', HDR)])
        else:
            L.append([(f'  {"mat_host_id":<38} {"dpu":<4} {"MAT state":<22} {"API state (as MAT sees it)":<34} {"booted OS":<10} {"last timer":<30} at', HDR)])
            with_state = [(k, r) for k, r in log.machines.items() if r['state'] != '?']
            only_bmc = len(log.machines) - len(with_state)
            for (host, dpu), rec in sorted(with_state, key=lambda kv: (kv[0][0], kv[0][1] or '')):
                L.append([(f'  {host:<38} {(dpu or "host"):<4} ', PLAIN),
                          (f'{short(rec["state"], 22):<22}', state_style(rec['state'])),
                          (f' {short(rec["api_state"], 34):<34} {short(rec["booted_os"], 10):<10} ', PLAIN),
                          (f'{short(rec["timer"], 30):<30}', NUM), (f' {rec["ts"]}', PLAIN)])
            if only_bmc:
                L.append([(f'  (+{only_bmc} ids seen only in BMC-mock lines, no iteration state — the DPU BMC mocks)', HDR)])
        L.append([])
    if not logs:
        L.append([("MAT logs: none given (server view only). Add --mat-log <file> for MAT's own view.", HDR)])
    return L


def plain(lines):
    return '\n'.join(''.join(t for t, _ in line) for line in lines)


def run_plain(admin_cli, logs, interval, once):
    while True:
        server = fetch_server(admin_cli)
        for log in logs:
            log.refresh()
        print(plain(render(server, logs, admin_cli, interval)))
        if once:
            return
        print('-' * 100, flush=True)
        time.sleep(interval)


def run_tui(admin_cli, logs, interval):
    def main(stdscr):
        curses.curs_set(0)
        stdscr.nodelay(True)
        attrs = {PLAIN: curses.A_NORMAL}
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            pairs = {TITLE: curses.COLOR_CYAN, SECTION: curses.COLOR_BLUE, OK: curses.COLOR_GREEN,
                     WIP: curses.COLOR_YELLOW, BAD: curses.COLOR_RED, NUM: curses.COLOR_MAGENTA, HDR: -1}
            for i, (name, color) in enumerate(pairs.items(), start=1):
                curses.init_pair(i, color, -1)
                attrs[name] = curses.color_pair(i)
            attrs[TITLE] |= curses.A_BOLD
            attrs[SECTION] |= curses.A_BOLD
            attrs[BAD] |= curses.A_BOLD
            attrs[HDR] = curses.A_DIM
        else:
            for name in (TITLE, SECTION):
                attrs[name] = curses.A_BOLD
            attrs[HDR] = curses.A_DIM
            for name in (OK, WIP, BAD, NUM):
                attrs[name] = curses.A_NORMAL
        last = 0
        lines = [[('starting…', HDR)]]
        while True:
            now = time.time()
            if now - last >= interval:
                server = fetch_server(admin_cli)
                for log in logs:
                    log.refresh()
                lines = render(server, logs, admin_cli, interval, width=stdscr.getmaxyx()[1])
                last = now
            h, w = stdscr.getmaxyx()
            stdscr.erase()
            for y, line in enumerate(lines[:h - 2]):
                x = 0
                for text, style in line:
                    if x >= w - 1:
                        break
                    try:
                        stdscr.addnstr(y, x, text, w - 1 - x, attrs.get(style, curses.A_NORMAL))
                    except curses.error:
                        pass
                    x += len(text)
            remaining = max(0, int(interval - (time.time() - last)))
            try:
                stdscr.addnstr(h - 1, 0, f'q quit   r refresh now   next refresh in {remaining}s', w - 1, curses.A_REVERSE)
            except curses.error:
                pass
            stdscr.refresh()
            ch = stdscr.getch()
            if ch in (ord('q'), ord('Q'), 27):
                return
            if ch in (ord('r'), ord('R')):
                last = 0
            curses.napms(500)
    curses.wrapper(main)


def main():
    p = argparse.ArgumentParser(description='Watch a MAT run from NICo\'s and MAT\'s side',
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument('--admin-cli', required=True, help='the site\'s run-admin-cli.sh (or a configured nico-admin-cli)')
    p.add_argument('--mat-log', action='append', default=[], metavar='FILE', help='MAT log file (repeatable, optional)')
    p.add_argument('--interval', type=int, default=30, help='seconds between refreshes (default 30)')
    p.add_argument('--once', action='store_true', help='one plain-text refresh, then exit')
    p.add_argument('--no-tui', action='store_true', help='plain text instead of the full-screen view')
    a = p.parse_args()
    logs = [MatLog(f) for f in a.mat_log]
    if a.once or a.no_tui or not sys.stdout.isatty():
        try:
            run_plain(a.admin_cli, logs, a.interval, a.once)
        except KeyboardInterrupt:
            pass
    else:
        run_tui(a.admin_cli, logs, a.interval)


if __name__ == '__main__':
    main()
