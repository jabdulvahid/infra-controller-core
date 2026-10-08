#!/usr/bin/env python3
"""
nico-dev — watch a MAT run: NICo's view (admin CLI) and MAT's view (its logs),
refreshed every 10 s, in a full-screen text UI or as plain text.

  monitor-mat.py --admin-cli <site>/run-admin-cli.sh \\
                 [--mat-log /var/log/machine-a-tron-dc1-base.log ...] \\
                 [--interval 10] [--history FILE | --no-history] [--filter SUBSTR] \\
                 [--once] [--no-tui]

  --admin-cli   the site's run-admin-cli.sh wrapper (or a nico-admin-cli on PATH
                with API_URL/*_PATH already exported). Required.
  --mat-log     a MAT log file; repeatable; optional. Without it the monitor
                shows the server side only. Unreadable files are reported, not
                fatal (MAT logs under /var/log are root-owned: run with sudo).
  --interval    seconds between refreshes (default 10).
  --history     file the state transitions are appended to and read back from
                at start (default <site>/monitor-mat-history.log next to the
                admin CLI wrapper); --no-history keeps them in memory only.
  --filter      show only one object's transitions on the history and timeline
                pages: part of a BMC address, a machine id or a kind (the TUI's
                `/` key does the same).
  --once        one refresh, plain text, exit (for scripts and pasting).
  --no-tui      plain text every interval instead of the curses screen.
  --kubeconfig  for the DPF section (default: the *.kubeconfig.yaml next to the
                admin CLI wrapper, i.e. the site folder). --no-dpf skips it.
  --dpf-namespace  where the DPF resources live (default dpf-operator-system).

What it shows
  1. Expected machines registered with NICo (count; MAT registers its hosts).
  2. Site-explorer endpoints: address, type, vendor, pre-ingestion state, the
     machine each became, the last exploration error.
  3. Machines: state as NICo reports it, the lifecycle milestone that state
     belongs to, how many milestones remain to Ready, and Ready/Failed marks.
     A machine id is tagged with what a person remembers, `[host 11.140.2.3]`
     or `[dpu 11.140.2.2]`: DPUs from the endpoint report's MachineId column,
     hosts by matching the product serial of `machine show <id>` (read once
     per host) against the report's Serial Number column. The tags also
     appear on the history and timeline pages and answer to the filter.
  4. DPUs as NICo sees them (`dpu status`: state, health, firmware status) and
     as DPF sees them (kubectl: every DPU resource with its phase, where that
     phase sits on the simulator's happy path, how many phases remain, how long
     it has been in the phase, and whether the node has a host reboot pending),
     plus the simulator pod's state.
  5. MAT's own view per mock host and DPU, from the log: MAT FSM state, the
     API state MAT last observed, the OS it booted, the last timer it armed.
     (Every MAT log line inside a machine's iteration span ends with
     mat_host_id=… [dpu_index=…] api_state=… state=… booted_os=…; the monitor
     keeps the latest per machine.)

Pages (full-screen mode). Page 0 is the overview: every section expanded
except MAT, history and timeline, which are collapsed to a count line because
they have their own pages (e m u d l t y toggle any section on page 0). Pages
1-7 show one section alone, in full, and scroll: 1 endpoints, 2 machines,
3 DPUs (NICo), 4 DPF, 5 MAT, 6 history (every state change the monitor saw,
with the time of the poll that first saw it: machines, endpoints, DPUs and
DPF phases; a fleet reset by reset-mat-state.py shows as a separator),
7 timeline (per object, its states in order with how long each was held;
machines come from NICo's own state history, `machine show <id> -c 250`,
which is complete, exactly timed and independent of when the monitor
started, so a MAT run that began before the monitor is shown in full;
endpoints and DPF phases, which NICo keeps no history for, come from the
poll diary since the last fleet reset). / on pages 6 and 7 filters them to
one object (part of a BMC address, a machine id or a kind); an empty answer
clears. Keys: the digit, or ←/→,
Tab/Shift-Tab, n/p to step; ↑/↓ (j/k), PgUp/PgDn (Space), Home/End to scroll;
? or h opens a help page listing the pages, keys and columns; r refreshes now;
q quits. With several --mat-log files the MAT page opens on the most recently
written one; [ and ] step through the others and a shows them all (--all-logs
opens on all). Plain-text mode (--once, --no-tui) prints every section. nico-api's
pre-ingestion firmware decisions (checked, upgraded, satisfied, not checked,
complete, and why) are read from the pod log with kubectl each refresh and
shown under each endpoint on pages 1 and 7; --no-api-log turns that off.

Milestones, from docs/architecture/state_machines/managedhost.md: a managed
host walks Created → DpuDiscovering → DPUInitializing → HostInitializing →
[BomValidating → Validation → Measuring, when enabled] → Ready. The state
string's prefix (before "/") names the milestone; the part after it is the
sub-state. "to go" counts milestones on the path, so it is an upper bound
when the optional ones are disabled.
"""

import argparse
import json
import curses
import os
import re
import subprocess
import sys
import threading
import time
from collections import OrderedDict
import warnings
from datetime import datetime, timezone

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


# ── DPF (doca-platform v26.4.0 phases, in the order the simulator walks them) ──
DPF_PATH = ['Initializing', 'Node Effect', 'Pending', 'Prepare BFB', 'DPU Config',
            'Config FW Parameters', 'Initialize Interface', 'OS Installing', 'Rebooting',
            'DPU Cluster Config', 'Host Network Configuration', 'Node Effect Removal', 'Ready']
DPF_GATED = {'Node Effect': 'waits for the node-effect hold', 'Rebooting': 'waits for NICo to power-cycle the host'}
ANN_REBOOT_REQUIRED = 'provisioning.dpu.nvidia.com/dpunode-external-reboot-required'
ANN_PHASE_ENTERED = 'sim.dpu.nvidia.com/phase-entered-at'
ANN_REBOOT_REQUESTED = 'sim.dpu.nvidia.com/node-reboot-requested-at'
ANN_REBOOT_COMPLETED = 'sim.dpu.nvidia.com/node-reboot-completed-at'


def dpf_to_go(phase):
    if phase in DPF_PATH:
        return str(len(DPF_PATH) - 1 - DPF_PATH.index(phase))
    return '-'


def age(ts):
    """'3m12s' since an RFC3339 timestamp, or ''."""
    if not ts:
        return ''
    try:
        t = datetime.strptime(ts[:19], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc)
        d = int((datetime.now(timezone.utc) - t).total_seconds())
        return f'{d // 60}m{d % 60:02d}s' if d >= 60 else f'{d}s'
    except ValueError:
        return ''


def kubectl_json(kubeconfig, args, timeout=30):
    env = dict(os.environ, KUBECONFIG=kubeconfig) if kubeconfig else os.environ
    try:
        r = subprocess.run(['kubectl'] + args + ['-o', 'json'], capture_output=True, text=True,
                           timeout=timeout, env=env)
    except FileNotFoundError:
        return None, 'kubectl: not found'
    except subprocess.TimeoutExpired:
        return None, 'kubectl: timed out'
    if r.returncode != 0:
        return None, (r.stderr.strip().splitlines() or ['kubectl failed'])[-1][:160]
    try:
        return json.loads(r.stdout), None
    except ValueError:
        return None, 'kubectl: unparsable output'


def fetch_dpf(kubeconfig, namespace):
    out = {'errors': [], 'dpus': [], 'nodes': {}, 'devices': 0, 'sim': ''}
    d, err = kubectl_json(kubeconfig, ['-n', namespace, 'get', 'dpus'])
    if err:
        out['errors'].append(f'dpus: {err}')
    else:
        for it in d.get('items', []):
            m, st, sp = it['metadata'], it.get('status', {}) or {}, it.get('spec', {}) or {}
            ann = m.get('annotations') or {}
            out['dpus'].append({'name': m['name'], 'node': sp.get('dpuNodeName') or sp.get('nodeName') or '',
                                'phase': st.get('phase') or '', 'since': ann.get(ANN_PHASE_ENTERED, ''),
                                'error': next((c.get('message', '') for c in st.get('conditions', [])
                                               if c.get('status') == 'False' and c.get('type', '').endswith('Ready')), '')})
    d, err = kubectl_json(kubeconfig, ['-n', namespace, 'get', 'dpunodes'])
    if err:
        out['errors'].append(f'dpunodes: {err}')
    else:
        for it in d.get('items', []):
            ann = it['metadata'].get('annotations') or {}
            out['nodes'][it['metadata']['name']] = {
                'reboot_required': ann.get(ANN_REBOOT_REQUIRED, ''),
                'reboot_requested': ann.get(ANN_REBOOT_REQUESTED, ''),
                'reboot_completed': ann.get(ANN_REBOOT_COMPLETED, ''),
                'dpus': len((it.get('spec') or {}).get('dpus') or [])}
    d, err = kubectl_json(kubeconfig, ['-n', namespace, 'get', 'dpudevices'])
    if not err:
        out['devices'] = len(d.get('items', []))
    d, err = kubectl_json(kubeconfig, ['-n', namespace, 'get', 'pods'])
    if not err:
        for it in d.get('items', []):
            if it['metadata']['name'].startswith('dpf-sim-controller'):
                cs = (it.get('status', {}).get('containerStatuses') or [{}])[0]
                out['sim'] = f"{it['status'].get('phase', '?')}, restarts {cs.get('restartCount', 0)}"
    return out


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
    # `machine show <id> -c 250` gives two things the lists do not: the
    # machine's own state history as NICo recorded it (every persisted state
    # with its timestamp, 250 kept per machine) and the product serial, which
    # ties a host to its BMC address through the endpoint report's Serial Number
    # column (the report names machine ids for DPU BMCs only). One call per
    # machine, and only when its State Version changed since the last fetch or
    # a host's serial is still unknown, so a quiet fleet costs nothing.
    # A "Host (Predicted)" machine, created from its DPUs before the host BMC
    # report is tied to it, has no serial yet; only a found serial is cached.
    listed = set()
    for r in out['machines']:
        mid, mtype = col(r, 'id'), col(r, 'type').lower()
        if not mid:
            continue
        listed.add(mid)
        version = col(r, 'state version', 'stateversion')
        entry = MACHINE_HISTORY.get(mid)
        need_serial = mtype.startswith('host') and 'predicted' not in mtype and mid not in HOST_SERIALS
        if entry is None or entry['version'] != version or need_serial:
            detail, err = run_cli(admin_cli, ['machine', 'show', mid, '-c', '250'])
            if err is None:
                m = re.search(r'^PRODUCT SERIAL\s*:\s*(\S+)', detail or '', re.M)
                if m:
                    HOST_SERIALS[mid] = m.group(1)
                rows = parse_state_history(detail or '')
                if rows or entry is None:
                    MACHINE_HISTORY[mid] = {'version': version, 'rows': rows, 'gone': False,
                                            'state': col(r, 'state'), 'type': col(r, 'type')}
            elif entry is None:
                out['errors'].append(f'machine show {mid[:12]}…: {err}')
        else:
            entry['state'], entry['type'], entry['gone'] = col(r, 'state'), col(r, 'type'), False
    if not out['errors']:
        for mid, entry in MACHINE_HISTORY.items():
            if mid not in listed:
                entry['gone'] = True
    out['host_serials'] = dict(HOST_SERIALS)

    text, err = run_cli(admin_cli, ['dpu', 'status'])
    out['dpus'] = parse_table(text) if text else []
    if err:
        out['errors'].append(f'dpu status: {err}')

    text, err = run_cli(admin_cli, ['dpf', 'show'])
    out['dpf'] = {col(r, 'id'): r for r in parse_table(text)} if text else {}
    if err:
        out['errors'].append(f'dpf show: {err}')
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
        self.mtime = None               # last write to the file, for the "idle" indicator

    def refresh(self):
        try:
            size = os.path.getsize(self.path)
            self.mtime = os.path.getmtime(self.path)
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
STYLE_BY_NAME = {'plain': PLAIN, 'hdr': HDR, 'ok': OK, 'wip': WIP, 'bad': BAD, 'num': NUM}
# sections, their toggle key, and whether they show by default
# Page 0 shows every section expanded except MAT, which has its own page (5):
# it is the longest table and the one that pushed the others off the screen.
SECTIONS = [('endpoints', 'e', True), ('machines', 'm', True), ('dpus', 'u', True), ('dpf', 'd', True), ('mat', 'l', False),
            ('history', 't', False), ('timeline', 'y', False)]
DEFAULT_SHOW = {name for name, _, on in SECTIONS if on}
# Pages: 0 is the overview (every section, collapsed or expanded per the
# toggles above); 1..5 show one section in full, scrollable.
PAGES = ['all'] + [name for name, _, _ in SECTIONS]


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


MAT_CMD_RE = re.compile(r'(?:^|/)(machine-a-tron(?:\.[^/\s]+)?)(?:\s|$)')


def mat_pids():
    """Running MAT processes as [(pid, binary name)], empty when none. The
    monitor only reads the log and the API, so without this a MAT that was
    killed looks like a quiet one. run-mat*.sh installs the binary as
    /usr/local/bin/machine-a-tron.<variant>, so the 15-char process name is
    truncated and `pgrep -x machine-a-tron` misses it: match the command line
    instead, binary name with optional .<variant>, followed by its config
    argument. The monitor's own --mat-log …/machine-a-tron-dc1.log does not
    match (a '-' follows the name), nor does the sudo/env wrapper twice, since
    the pid list is deduplicated by binary name."""
    try:
        r = subprocess.run(['pgrep', '-af', 'machine-a-tron'], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return []
    found = {}
    for line in r.stdout.splitlines():
        pid, _, cmd = line.partition(' ')
        m = MAT_CMD_RE.search(cmd)
        if m and pid.isdigit() and 'sudo' not in cmd.split(' ', 1)[0]:
            found.setdefault(m.group(1), pid)
    return [(pid, name) for name, pid in found.items()]


def log_age(log):
    """Seconds since the MAT log was last written, or None."""
    return None if log is None or log.mtime is None else max(0, int(time.time() - log.mtime))


def fmt_age(secs):
    if secs < 60:
        return f'{secs}s'
    if secs < 3600:
        return f'{secs // 60}m{secs % 60:02d}s'
    return f'{secs // 3600}h{(secs % 3600) // 60:02d}m'


def render_header(server, admin_cli, interval, page='all'):
    now = datetime.now().strftime('%H:%M:%S')
    pids = mat_pids()
    machines = server.get('machines', [])
    ready = sum(1 for r in machines if col(r, 'state').lower().startswith('ready'))
    failed = sum(1 for r in machines if col(r, 'state').lower().startswith('failed'))
    hosts = sum(1 for r in machines if 'dpu' not in col(r, 'type').lower())
    if page == 'all':
        where = ''
    elif page in PAGES:
        where = f'   page {PAGES.index(page)}/{len(PAGES) - 1}: {page}'
    else:
        where = f'   {page}'
    L = [[('MAT run monitor', TITLE), (f'  {now}  refresh {interval}s{where}   MAT process: ', PLAIN),
          ((f'running ({", ".join(f"{name} pid {pid}" for pid, name in pids)})', OK) if pids else ('NOT RUNNING', BAD)),
          (f'   admin-cli: {short(admin_cli, 60)}', PLAIN)],
         [('expected machines: ', PLAIN), (str(len(server.get('expected', []))), NUM),
          ('    endpoints: ', PLAIN), (str(len(server.get('endpoints', []))), NUM),
          ('    machines: ', PLAIN), (str(len(machines)), NUM), (f' ({hosts} hosts, {len(machines) - hosts} DPUs)', PLAIN),
          ('    Ready: ', PLAIN), (f'{ready}/{len(machines)}', OK if machines and ready == len(machines) else NUM),
          ('    Failed: ', PLAIN), (str(failed), BAD if failed else PLAIN)]]
    build = mat_build_info(admin_cli)
    if build:
        L.append([('MAT binary: ', PLAIN), (build, HDR)])
    for e in server.get('errors', []):
        L.append([(f'  ! {e}', BAD)])
    L.append([])
    return L


def mat_build_info(admin_cli):
    """`commit <sha> (<branch>) built <time>` from <site>/mat/BUILD_INFO, written
    by build-nico-clis.py next to the binary; empty when absent."""
    try:
        text = open(os.path.join(os.path.dirname(os.path.abspath(admin_cli)), 'mat', 'BUILD_INFO')).read().strip()
    except OSError:
        return ''
    m = re.search(r'(commit \S+ \([^)]*\) built \S+)', text)
    return m.group(1) if m else short(text, 80)


def sec_endpoints(server, width):
    L = [[('ENDPOINTS (site explorer)', SECTION)],
         [(f'  {"address":<14} {"kind":<5} {"type":<5} {"vendor":<8} {"pre-ingestion":<14} {"machine":<44} last error', HDR)]]
    eps = server.get('endpoints', [])
    if not eps:
        L.append([('  (none yet)', HDR)])
    for r in eps:
        pre = col(r, 'pre-ingestion state', 'preingestionstate')
        err = col(r, 'last exploration error', 'lastexplorationerror')
        L.append([(f'  {short(col(r, "address"), 14):<14} {ENDPOINT_KIND.get(col(r, "address"), ""):<5} {short(col(r, "type"), 5):<5} {short(col(r, "vendor"), 8):<8} ', PLAIN),
                  (f'{short(pre, 14):<14}', state_style(pre)),
                  (f' {short(col(r, "machineid", "machine id", "machine"), 44):<44} ', PLAIN),
                  (short(err, max(10, width - 102)), BAD if err else PLAIN)])
        # the latest firmware decision nico-api logged for this BMC (current run)
        last = APILOG.last_for(col(r, 'address'), run_start()) if APILOG else None
        if last:
            text, style = APILOG.decision(last[2], last[3]) or (last[2], 'plain')
            L.append([(f'  {"":<20} firmware check {last[0][11:19]}: ', HDR), (short(text, max(20, width - 48)), STYLE_BY_NAME[style])])
    if APILOG and APILOG.error:
        L.append([(f'  ! nico-api log: {APILOG.error}', BAD)])
    return L


def sec_machines(server, width):
    machines = server.get('machines', [])
    L = [[('MACHINES (NICo)', SECTION), (f'   end state: {END_STATE}   milestones: {" > ".join(MILESTONES)}', PLAIN)],
         [(f'  {"id":<44} {"type":<6} {"milestone":<17} {"to-go":<6} state (full, as NICo reports it)', HDR)]]
    if not machines:
        L.append([('  (none yet)', HDR)])
    for r in machines:
        state = col(r, 'state')
        m, _ = milestone_of(state)
        sty = state_style(state)
        mark = ' ✓' if sty == OK else (' ✗' if sty == BAD else '')
        L.append([(f'  {label("machine", col(r, "id"), 44):<44} {short(col(r, "type"), 6):<6} {short(m, 17):<17} ', PLAIN),
                  (f'{to_go(state) + mark:<6}', sty), (' ' + state, sty)])
    return L


def sec_dpus(server, width):
    # DPUs as NICo sees them
    L = [[('DPUS (NICo: dpu status, dpf show)', SECTION)]]
    dpus = server.get('dpus', [])
    dpf_rows = server.get('dpf', {})
    if not dpus:
        L.append([('  (none yet)', HDR)])
        return L
    L.append([(f'  {"dpu id":<44} {"type":<12} {"healthy":<8} {"version status":<16} {"dpf":<18} state', HDR)])
    for r in dpus:
        did = col(r, 'dpu id', 'dpuid', 'id')
        st = col(r, 'state')
        healthy = col(r, 'healthy')
        drow = dpf_rows.get(did) or {}
        dpf_txt = ''
        if drow:
            dpf_txt = ('enabled' if col(drow, 'enabled').lower() in ('true', 'yes') else 'disabled') + \
                      (' +ingested' if col(drow, 'used for ingestion', 'usedforingestion').lower() in ('true', 'yes') else '')
        L.append([(f'  {short(did, 44):<44} {short(col(r, "dpu type", "dputype", "type"), 12):<12} ', PLAIN),
                  (f'{short(healthy, 8):<8}', OK if healthy.lower() in ('true', 'yes', 'healthy') else (WIP if healthy else PLAIN)),
                  (f' {short(col(r, "version status", "versionstatus"), 16):<16} {short(dpf_txt, 18):<18} ', PLAIN),
                  (st, state_style(st))])
    return L


def sec_dpf(dpf, width):
    # DPUs as DPF sees them
    if dpf is None:
        return [[('DPF (kubectl)', SECTION), ('  not watched (--no-dpf)', HDR)]]
    n = len(dpf['dpus'])
    ready = sum(1 for d in dpf['dpus'] if d['phase'] == 'Ready')
    err_n = sum(1 for d in dpf['dpus'] if d['phase'] == 'Error')
    L = [[('DPF (kubectl)', SECTION),
          (f'   DPUNodes: {len(dpf["nodes"])}   DPUDevices: {dpf["devices"]}   DPUs: {n}   Ready: ', PLAIN),
          (f'{ready}/{n}', OK if n and ready == n else NUM), ('   Error: ', PLAIN), (str(err_n), BAD if err_n else PLAIN),
          ('   simulator: ', PLAIN), (dpf['sim'] or 'not found', OK if dpf['sim'].startswith('Running') else BAD)]]
    for e in dpf['errors']:
        L.append([(f'  ! {e}', BAD)])
    L.append([(f'  happy path: {" > ".join(DPF_PATH)}', HDR)])
    if not dpf['dpus']:
        L.append([('  (no DPU resources yet — NICo creates them once the host reaches DPUInitializing)', HDR)])
        return L
    L.append([(f'  {"dpu":<50} {"phase":<28} {"to-go":<6} {"in phase":<9} node reboot', HDR)])
    for d in sorted(dpf['dpus'], key=lambda x: x['name']):
        node = dpf['nodes'].get(d['node'] or d['name'].split('-device-')[0], {})
        reboot = ''
        if node.get('reboot_required') == 'true':
            reboot = 'REQUIRED — waiting for NICo to power-cycle the host'
        elif node.get('reboot_completed'):
            reboot = f'done {age(node["reboot_completed"])} ago'
        elif node.get('reboot_requested'):
            reboot = f'requested {age(node["reboot_requested"])} ago'
        sty = OK if d['phase'] == 'Ready' else (BAD if d['phase'] == 'Error' else WIP)
        L.append([(f'  {short(d["name"], 50):<50} ', PLAIN), (f'{short(d["phase"], 28):<28}', sty),
                  (f' {dpf_to_go(d["phase"]):<6} {age(d["since"]):<9} ', PLAIN),
                  (reboot + (f'  {d["error"]}' if d['error'] else ''), BAD if 'REQUIRED' in reboot or d['error'] else PLAIN)])
        if d['phase'] in DPF_GATED and d['phase'] != 'Ready':
            L.append([(f'  {"":<50} ({DPF_GATED[d["phase"]]})', HDR)])
    return L


def sec_mat(logs, width):
    if not logs:
        return [[("MAT logs: none given (server view only). Add --mat-log <file> for MAT's own view.", HDR)]]
    L = []
    for log in logs:
        head = [(f'MAT ({os.path.basename(log.path)})', SECTION)]
        if log.lines:
            head.append((f'   lines: {log.lines}   span {log.first_ts[11:] if log.first_ts else "?"}–{log.last_ts[11:] if log.last_ts else "?"}', PLAIN))
        age = log_age(log)
        if age is not None:
            # MAT writes something every few seconds while it runs; a quiet
            # log with no process behind it means the run is over.
            running = bool(mat_pids())
            head.append((f'   last write {fmt_age(age)} ago', PLAIN if running and age < 120 else BAD))
            if not running:
                head.append(('   — MAT is not running', BAD))
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
        if log is not logs[-1]:
            L.append([])
    return L


class Transitions:
    """Remembers every object's state between refreshes and records each change
    with the time of the poll that first saw it, in memory for the history page
    and appended to a plain-text file for reading after the run. Objects:
    machines (state), endpoints (state / pre-ingestion state), DPUs (state) and
    DPF DPU resources (phase). Resolution is the poll interval."""

    LINE_RE = re.compile(r'^(\S+)\s+(\S+)\s+(\S+)\s+(.*?) -> (.*)$')
    # reset-mat-state.py appends this when it wipes the fleet: the boundary
    # between runs, since NICo's ids are deterministic and reappear after a reset
    MARK_RE = re.compile(r'^# ---- fleet reset (\S+)')

    def __init__(self, path=None):
        self.path = path
        self.last = {}          # (kind, id) → state string
        self.events = []        # (time, kind, id, old, new)
        self.error = None
        self.loaded = 0         # transitions read back from an existing file
        if path:
            self._load(path)
            try:
                with open(path, 'a') as f:
                    f.write(f'# monitor-mat transitions — started {datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}; '
                            f'time = the poll that first saw the new state\n')
            except OSError as e:
                self.error = f'{path}: {e.strerror}'
                self.path = None

    def _load(self, path):
        """Read an existing history file so a restarted monitor shows the whole
        run and continues from the last recorded state of every object."""
        try:
            with open(path) as f:
                for line in f:
                    mark = self.MARK_RE.match(line)
                    if mark:
                        # everything before the reset is a previous run
                        self.events.append((mark.group(1), 'reset', '', None, 'fleet reset'))
                        self.last.clear()
                        continue
                    m = self.LINE_RE.match(line.rstrip('\n'))
                    if not m:
                        continue
                    now, kind, ident, old, new = m.groups()
                    self.events.append((now, kind, ident, None if old == '(new)' else old, new))
                    if new == '(gone)':
                        self.last.pop((kind, ident), None)
                    else:
                        self.last[(kind, ident)] = new
        except FileNotFoundError:
            return
        except OSError as e:
            self.error = f'{path}: {e.strerror}'
            return
        self.loaded = len(self.events)

    def observe(self, server, dpf):
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        update_labels(server)
        seen = {}
        for r in server.get('machines', []):
            seen[('machine', col(r, 'id'))] = col(r, 'state')
        for r in server.get('endpoints', []):
            # the endpoint report has no plain state column; its lifecycle is the
            # pre-ingestion state, plus whether a machine exists for it yet
            pre = col(r, 'pre-ingestion state', 'preingestionstate') or '?'
            has_machine = bool(col(r, 'machineid', 'machine id', 'machine'))
            seen[('endpoint', col(r, 'address'))] = pre + (' (machine)' if has_machine else '')
        for r in server.get('dpus', []):
            seen[('dpu', col(r, 'dpu id', 'dpuid', 'id'))] = col(r, 'state')
        for d in (dpf or {}).get('dpus', []):
            seen[('dpf', d['name'])] = d['phase']
        # a source that was not fetched this round (kubectl off, --no-dpf, an
        # admin-cli error) says nothing about its objects: do not mark them gone
        fetched = {'machine', 'endpoint', 'dpu'} if not server.get('errors') else set()
        if dpf is not None:
            fetched.add('dpf')
        for key, state in seen.items():
            old = self.last.get(key)
            if old != state:
                self._record(now, key, old, state)
        for key in [k for k in self.last if k not in seen and k[0] in fetched]:
            self._record(now, key, self.last[key], '(gone)')
            del self.last[key]
        self.last.update(seen)

    def _record(self, now, key, old, new):
        kind, ident = key
        self.events.append((now, kind, ident, old, new))
        if self.path:
            try:
                with open(self.path, 'a') as f:
                    f.write(f'{now} {kind:<8} {ident:<44} {old if old is not None else "(new)"} -> {new}\n')
            except OSError as e:
                self.error = f'{self.path}: {e.strerror}'
                self.path = None


class ApiLog:
    """nico-api's pre-ingestion firmware decisions, read from the pod log with
    kubectl every refresh and kept per BMC address, so the reason a host was
    upgraded or let through is on the endpoints and timeline pages instead of
    in a grep (20261008-#2). Only the decision lines are kept (the "Checking",
    "upload", "satisfies", "no definition", "not checked" and "complete" lines
    of check_firmware_versions_below_preingestion, plus the "Fresh exploration
    report" line that starts the check); they are appended to the history
    file as `#api` lines so a restarted monitor and a later reader have them.
    Older nico-api builds log some of these at debug only; what is not in the
    pod log cannot be shown."""

    LINE_RE = re.compile(r'^(\S+Z)\s+level=\w+\s+.*?msg="((?:[^"\\]|\\.)*)"(.*)$')
    ADDR_RE = re.compile(r'\bbmc_ip_address=(\S+)')
    # a logfmt value: quoted, a Debug list `["Bmc 2.0"]`, a Debug option
    # `Some("GB200 NVL")`, or a bare word
    KV_RE = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\[[^\]]*\]|Some\("(?:[^"\\]|\\.)*"\)|\S+)')
    FILE_RE = re.compile(r'^#api (\S+) (\S+) (.*?)(?: \| (.*))?$')
    INNER_RE = re.compile(r'\b(\w+): ("(?:[^"\\]|\\.)*"|[\w.]+)')
    # message prefix → how the decision reads on screen; {x} are logfmt fields
    DECISIONS = [
        ('Starting firmware upload', 'UPGRADE {firmware_type}', 'wip'),
        ('Checking firmware version', 'check {firmware_type} {inventory_id} {current} vs min {min_preingestion}', 'plain'),
        ('Firmware version satisfies', 'ok {firmware_type}', 'ok'),
        ('No host firmware definition matches', 'complete: no definition for vendor={vendor} model={model}', 'hdr'),
        ('No inventory entry in the exploration report matches', 'NOT CHECKED {firmware_type}: no inventory entry', 'bad'),
        ('The matching inventory entry reports no version', 'NOT CHECKED {firmware_type}: {inventory_id} has no version', 'bad'),
        ('No listed component is below', 'complete: satisfied={satisfied} not_checked={not_checked}', 'ok'),
        ('Firmware versions satisfy preingestion requirements', 'complete (versions satisfy)', 'ok'),
        ('Fresh exploration report received', 'fresh report after BMC reset; checks start', 'plain'),
        ('No matching firmware info found', 'complete: no definition matched', 'hdr'),
        # the recheck between components and the activation of an upgrade
        ('Installing firmware', 'UPGRADE {fw_type} to {version} (recheck)', 'wip'),
        ('Firmware upgrade task complete; host is off, powering on', 'activating: task done, host off, powering on', 'wip'),
        ('Firmware upgrade task complete; initiating required BMC reboot', 'activating: task done, BMC reboot', 'wip'),
        ('Firmware upgrade task complete; initiating required reboot', 'activating: task done, host reboot (was {power_state})', 'wip'),
        ('Firmware upgrade task complete but reported version has not updated', 'waiting: {upgrade_type} still {current_version}, want {final_version}', 'plain'),
        ('Firmware upgrade now reports the new version', 'activated: now {current_version}', 'ok'),
        ('No further firmware updates needed', 'complete: no further updates needed', 'ok'),
    ]

    def __init__(self, kubeconfig, namespace='nico-system', path=None):
        self.kubeconfig, self.namespace, self.path = kubeconfig, namespace, path
        self.events = []          # (ts, address, msg, fields) in log order
        self.by_addr = {}         # address → [events]
        self.seen = set()         # (ts, address, msg) already recorded
        self.since = None         # timestamp of the newest line read, for --since-time
        self.error = None
        self.polls = 0
        if path:
            self._load(path)

    def _load(self, path):
        try:
            with open(path) as f:
                for line in f:
                    m = self.FILE_RE.match(line.rstrip('\n'))
                    if m:
                        ts, addr, msg, fields = m.groups()
                        self._add(ts, addr, msg, self._kv(fields or '{}'), write=False)
        except OSError:
            return

    @staticmethod
    def _fields(rest):
        """The logfmt fields after msg, minus the noise, with quoting undone:
        `current="\\"1.0\\"" firmware_type=Uefi location=...` → {'current': '1.0', 'firmware_type': 'Uefi'}."""
        out = {}
        for k, v in ApiLog.KV_RE.findall(rest):
            if k in ('location', 'component', 'level', 'bmc_ip_address'):
                continue
            if v.startswith('"') and v.endswith('"'):
                v = v[1:-1].replace('\\"', '"')
            if v.startswith('Some(') and v.endswith(')'):
                v = v[5:-1]
            out[k] = v.strip('"')
        # fields inside a Debug-printed struct, `to_install=Thing { fw_type: Uefi,
        # version: "2.0" }`, are reachable by their inner names too
        for k, v in ApiLog.INNER_RE.findall(rest):
            out.setdefault(k, v.strip('"'))
        return out

    @staticmethod
    def _kv(fields):
        """Fields as a dict, from a dict, the JSON the history file holds, or a raw logfmt tail."""
        if isinstance(fields, dict):
            return fields
        if fields.startswith('{'):
            try:
                return json.loads(fields)
            except ValueError:
                return {}
        return ApiLog._fields(fields)

    @classmethod
    def decision(cls, msg, fields):
        """(text, style name) for a decision line; None for a line that is not one."""
        for prefix, template, style in cls.DECISIONS:
            if msg.startswith(prefix):
                kv = cls._kv(fields)
                text = template.format_map({k: kv.get(k, '') for k in re.findall(r'\{(\w+)\}', template)})
                text = re.sub(r' {2,}', ' ', text).strip()
                # a completion that skipped a listed component is the case to notice
                if prefix.startswith('No listed component') and kv.get('not_checked', '[]') not in ('[]', ''):
                    style = 'bad'
                return text, style
        return None

    def _add(self, ts, addr, msg, fields, write=True):
        key = (ts, addr, msg)
        if key in self.seen:
            return
        self.seen.add(key)
        ev = (ts, addr, msg, fields)
        self.events.append(ev)
        self.by_addr.setdefault(addr, []).append(ev)
        if write and self.path:
            try:
                with open(self.path, 'a') as f:
                    f.write(f'#api {ts} {addr} {msg}' + (f' | {json.dumps(fields)}' if fields else '') + '\n')
            except OSError as e:
                self.error = f'{self.path}: {e.strerror}'
                self.path = None

    def refresh(self):
        args = ['kubectl']
        if self.kubeconfig:
            args += ['--kubeconfig', self.kubeconfig]
        args += ['-n', self.namespace, 'logs', 'deploy/nico-api', '--timestamps', '--all-containers=false']
        # first poll: the run so far; later polls: since the newest line seen
        # (inclusive, the seen-set drops the repeat)
        args += ['--since-time', self.since] if self.since else ['--since', '3h']
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=60)
        except FileNotFoundError:
            self.error = 'kubectl: not found'
            return
        except subprocess.TimeoutExpired:
            self.error = 'kubectl logs: timed out'
            return
        if r.returncode != 0:
            self.error = (r.stderr.strip().splitlines() or ['kubectl logs failed'])[-1][:160]
            return
        self.error = None
        self.polls += 1
        newest = self.since
        for line in r.stdout.splitlines():
            if 'crates/preingestion-manager/' not in line:
                continue
            m = self.LINE_RE.match(line)
            if not m:
                continue
            ts, msg, rest = m.groups()
            if newest is None or ts > newest:
                newest = ts
            if self.decision(msg, rest) is None:
                continue
            a = self.ADDR_RE.search(rest)
            if not a:
                continue
            self._add(ts, a.group(1).rstrip(':'), msg, self._fields(rest))
        self.since = newest

    def for_addr(self, addr, after=None):
        """This address's decision lines, optionally only those at or after a time
        (the last fleet-reset marker, so a previous run's lines stay out)."""
        return [e for e in self.by_addr.get(addr, []) if after is None or e[0] >= after]

    def last_for(self, addr, after=None):
        evs = self.for_addr(addr, after)
        return evs[-1] if evs else None


HISTORY = Transitions()
APILOG = None   # ApiLog when the nico-api log is watched (default when kubectl and a kubeconfig are at hand)
FILTER = ''   # substring on kind or id that pages 6 and 7 restrict themselves to ('/' in the TUI, --filter)
LABELS = {}   # machine id → (host|dpu, BMC address): what a person remembers instead of the 60-char id
ENDPOINT_KIND = {}   # BMC address → host|dpu, from the endpoint report (a DPU BMC carries a MachineId as soon as it is explored)
HOST_SERIALS = {}   # host machine id → product serial, from `machine show <id>` (fetch_server)
# machine id → {'version', 'rows': [(timestamp, compact state, raw json)], 'state', 'type', 'gone'}:
# NICo's own state history per machine, refreshed when its State Version changes.
# Unlike the poll diary it is complete, exactly timed, and starts with the
# machine rather than with the monitor. A machine that left the list stays here
# as gone until the monitor restarts.
MACHINE_HISTORY = {}

HISTORY_ROW_RE = re.compile(r'^\s*(\{.*\})\s+(V\S+)\s+(\S+)\s*$')


def compact_state(raw):
    """`hostinit/waitingfordiscovery` from a state-history JSON row: the top
    state and the first nested state below it; the raw JSON when unparseable."""
    try:
        d = json.loads(raw)
    except ValueError:
        return raw[:60]
    top = str(d.get('state', '?'))
    for k, v in d.items():
        if k == 'state':
            continue
        if isinstance(v, dict) and 'state' in v:
            return f'{top}/{v["state"]}'
        if isinstance(v, str):
            return f'{top}/{v}'
    return top


def parse_state_history(text):
    """Rows of the STATE HISTORY table in `machine show <id> -c N` output, oldest
    first, as (timestamp, compact state, raw json)."""
    rows, inside = [], False
    for line in text.splitlines():
        if line.startswith('STATE HISTORY'):
            inside = True
            continue
        if inside and line and not line[0].isspace():
            break
        if inside:
            m = HISTORY_ROW_RE.match(line)
            if m:
                rows.append((m.group(3), compact_state(m.group(1)), m.group(1)))
    return rows


def update_labels(server):
    """From this refresh: the endpoint report's MachineId column maps a DPU's BMC
    address to its machine id (it is empty for host BMCs); a host joins through
    its product serial, which the report lists per BMC and fetch_server read from
    `machine show <id>`. The machine list's Type column says host or DPU. Kept
    across refreshes so a machine that is gone still carries its tag in the
    history."""
    types = {col(r, 'id'): col(r, 'type').lower() for r in server.get('machines', [])}
    dpu_ids = {col(r, 'dpu id', 'dpuid', 'id') for r in server.get('dpus', [])}

    def kind_of(mid, default):
        t = types.get(mid, '')
        if t.startswith('dpu') or mid in dpu_ids:
            return 'dpu'
        if t.startswith('host'):
            return 'host'
        return LABELS[mid][0] if mid in LABELS else default

    by_serial = {}
    for r in server.get('endpoints', []):
        addr = col(r, 'address')
        serial = col(r, 'serial number', 'serialnumber', 'serial')
        if serial:
            by_serial[serial] = addr
        mid = col(r, 'machineid', 'machine id', 'machine')
        if mid:
            LABELS[mid] = (kind_of(mid, 'dpu'), addr)
        # The report fills MachineId for a DPU BMC from the moment it is
        # explored and leaves it empty for a host BMC, so the kind is known
        # long before any machine exists. An unexplored endpoint (no vendor
        # yet) keeps whatever was known.
        if mid:
            ENDPOINT_KIND[addr] = 'dpu'
        elif col(r, 'vendor') or col(r, 'type'):
            ENDPOINT_KIND.setdefault(addr, 'host')
    for mid, serial in server.get('host_serials', {}).items():
        if serial in by_serial:
            LABELS[mid] = (kind_of(mid, 'host'), by_serial[serial])
    for mid, (kind, addr) in list(LABELS.items()):
        if kind_of(mid, kind) != kind:
            LABELS[mid] = (kind_of(mid, kind), addr)


def label(kind, ident, width=44):
    """`<id> [host 11.140.2.6]` for a machine id with a known endpoint,
    `11.140.2.6 [host]` for an endpoint whose kind is known, else the id."""
    if kind == 'endpoint' and ident in ENDPOINT_KIND:
        return short(ident, width) + f' [{ENDPOINT_KIND[ident]}]'
    tag = LABELS.get(ident) if kind == 'machine' else None
    if not tag:
        return short(ident, width)
    suffix = f' [{tag[0]} {tag[1]}]'
    return short(ident, max(8, width - len(suffix))) + suffix


def matches(kind, ident):
    if not FILTER or kind == 'reset':
        return True
    f = FILTER.lower()
    tag = LABELS.get(ident, ('', ''))
    ep_kind = ENDPOINT_KIND.get(ident, '') if kind == 'endpoint' else ''
    return f in kind.lower() or f in ident.lower() or f in tag[0] or f in tag[1] or (bool(ep_kind) and f in ep_kind)


def filtered_events():
    return [e for e in HISTORY.events if matches(e[1], e[2])]


def run_start():
    """Time of the last fleet-reset marker, so per-run views leave the previous
    run out; None when no reset has been recorded."""
    resets = [e[0] for e in HISTORY.events if e[1] == 'reset']
    return resets[-1] if resets else None


def filter_note():
    return f'   filter: "{FILTER}" (/ to change, empty clears)' if FILTER else ''


def sec_history(width, limit=400):
    events = filtered_events()
    L = [[('HISTORY', SECTION),
          (f'   {len(events)} transitions' + (f' of {len(HISTORY.events)}' if FILTER else '') + ', newest last'
           + (f' ({HISTORY.loaded} read back from the file at start)' if HISTORY.loaded else '')
           + (f'   file: {HISTORY.path}' if HISTORY.path else '   (not written to a file)'), PLAIN),
          (filter_note(), NUM)]]
    if HISTORY.error:
        L.append([(f'  ! {HISTORY.error}', BAD)])
    if not events:
        L.append([('  (nothing observed yet — the first refresh records every object as (new))' if not HISTORY.events
                   else f'  (nothing matches "{FILTER}")', HDR)])
        return L
    L.append([(f'  {"time":<9} {"kind":<8} {"id":<44} from -> to', HDR)])
    for now, kind, ident, old, new in events[-limit:]:
        if kind == 'reset':
            L.append([(f'  {now[11:19]:<9} ---- fleet reset (reset-mat-state.py) — a new run starts here ' + '-' * max(0, width - 80), SECTION)])
            continue
        L.append([(f'  {now[11:19]:<9} {kind:<8} {label(kind, ident, 44):<44} ', PLAIN),
                  (f'{short(old if old is not None else "(new)", 40)}', HDR), (' -> ', PLAIN),
                  (short(new, max(20, width - 110)), state_style(new))])
    return L


KIND_ORDER = {'machine': 0, 'endpoint': 1, 'dpu': 2, 'dpf': 3}


def parse_ts(ts):
    """UTC datetime from `2026-09-30T20:30:21Z` or `…21.836924Z` (any number of
    fraction digits; the fraction is dropped, the pages show whole seconds)."""
    return datetime.strptime(re.sub(r'\.\d+', '', ts.rstrip('Z')), '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc)


# ── expected hold per state ──────────────────────────────────────────────────
# How long an object normally stays in a state, so "so far 17m" can be read as
# on time or stalled. Two kinds of time add up: MAT's simulated hardware
# (host reboot, BMC reset, firmware task; the GB200 profile at real time, scaled
# by acceleration_factor and the timing_overrides in mat-config.toml) and
# NICo's own cadences (pre-ingestion pass and explorer refresh every 30 s,
# machine controller passes), which do not scale. Values are the GB200 profile
# plus what nico-dev runs have shown; they are expectations, not limits.
SITE_DIR = ''
MAT_TIMING = {'factor': 1.0, 'reboot': 600, 'bmc_reset': 90, 'firmware_upgrade': 2, 'source': 'GB200 profile defaults'}
NICO_PASS = 30          # pre-ingestion manager / explorer cadence, seconds
EXPLORER_REFRESH = 120  # a fresh exploration report after a change, observed

_DUR_RE = re.compile(r'^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h)?\s*$')


def _dur_secs(text):
    m = _DUR_RE.match(text.strip().strip('"\''))
    if not m:
        return None
    v, unit = float(m.group(1)), m.group(2) or 's'
    return v * {'ms': 0.001, 's': 1, 'm': 60, 'h': 3600}[unit]


def load_mat_timing(site_dir):
    """acceleration_factor and the host timing_overrides from
    <site>/mat/mat-config.toml, by regex (no toml module on older Pythons).
    Missing file or keys leave the GB200 defaults."""
    path = os.path.join(site_dir, 'mat', 'mat-config.toml')
    try:
        text = open(path).read()
    except OSError:
        return
    m = re.search(r'^\s*acceleration_factor\s*=\s*([0-9.]+)', text, re.M)
    if m:
        MAT_TIMING['factor'] = float(m.group(1))
    for key in ('reboot', 'bmc_reset', 'firmware_upgrade'):
        m = re.search(rf'^\s*host\.{key}\s*=\s*("[^"]*"|\S+)', text, re.M)
        secs = _dur_secs(m.group(1)) if m else None
        if secs is not None:
            MAT_TIMING[key] = secs
    MAT_TIMING['source'] = f'{os.path.basename(path)} (factor {MAT_TIMING["factor"]:g})'


def _mat(key):
    return MAT_TIMING[key] * MAT_TIMING['factor']


# (kind, pattern on the state as displayed, expected seconds as a function).
# First match wins; states without a row have no expectation (Ready, Complete).
EXPECTATIONS = [
    # pre-ingestion (endpoint): the site explorer's Pre-ingestion State column
    ('endpoint', r'^Initial\b', lambda: NICO_PASS),
    ('endpoint', r'InitialBMCReset.*Start', lambda: NICO_PASS),
    ('endpoint', r'InitialBMCReset.*WaitForBmc', lambda: _mat('bmc_reset') + NICO_PASS),
    ('endpoint', r'InitialBMCReset.*WaitForExplorer', lambda: EXPLORER_REFRESH),
    ('endpoint', r'^SetNtpServers|^TimeSyncReset', lambda: 2 * NICO_PASS),
    ('endpoint', r'^UpgradeFirmwareWait', lambda: _mat('firmware_upgrade') + NICO_PASS),
    ('endpoint', r'^NewFirmwareReportedWait.*Uefi', lambda: _mat('reboot') + EXPLORER_REFRESH),
    ('endpoint', r'^NewFirmwareReportedWait', lambda: _mat('bmc_reset') + EXPLORER_REFRESH),
    ('endpoint', r'^ResetForNewFirmware.*Uefi', lambda: _mat('reboot') + EXPLORER_REFRESH),
    ('endpoint', r'^ResetForNewFirmware', lambda: _mat('bmc_reset') + EXPLORER_REFRESH),
    ('endpoint', r'^RecheckVersions', lambda: NICO_PASS),
    # machines (NICo state history, compact or display form)
    ('machine', r'(?i)^created$|configureastra|^initializing$|^configuring$', lambda: NICO_PASS),
    ('machine', r'(?i)dpudiscovering', lambda: 2 * NICO_PASS),
    ('machine', r'(?i)handlereboot', lambda: _mat('reboot') + NICO_PASS),
    ('machine', r'(?i)dpuinit.*(dpfstates|provisioning|waitingforready)', lambda: 5 * NICO_PASS),
    ('machine', r'(?i)waitingfornetworkconfig', lambda: 2 * NICO_PASS),
    ('machine', r'(?i)waitingfordiscovery|discovered', lambda: _mat('reboot') + 2 * NICO_PASS),
    ('machine', r'(?i)hostcleanup|waitingforcleanup', lambda: 2 * NICO_PASS),
    ('machine', r'(?i)waitingforlockdown', lambda: _mat('reboot') + 2 * NICO_PASS),
    ('machine', r'(?i)machinevalidat', lambda: _mat('reboot') + 2 * NICO_PASS),
    ('machine', r'(?i)hostinit|enableipmi|setbootorder|uefisetup|spdm|bomvalidat|^validation', lambda: 3 * NICO_PASS),
    ('machine', r'(?i)hostreprovision.*waitingforfirmwareupgrade', lambda: _mat('firmware_upgrade') + NICO_PASS),
    ('machine', r'(?i)hostreprovision.*newfirmwarereportedwait', lambda: _mat('bmc_reset') + EXPLORER_REFRESH),
    ('machine', r'(?i)hostreprovision.*resetfornewfirmware', lambda: _mat('reboot') + EXPLORER_REFRESH),
    ('machine', r'(?i)hostreprovision', lambda: 3 * NICO_PASS),
    # DPF resource phases (simulator dwell is short; reboots are MAT's)
    ('dpf', r'(?i)reboot', lambda: _mat('reboot') + NICO_PASS),
    ('dpf', r'(?i)initializ|pending|provision|config|os ?install|dpuclusterconfig', lambda: 5 * NICO_PASS),
]
_EXPECT_COMPILED = [(k, re.compile(p), f) for k, p, f in EXPECTATIONS]


def expected_for(kind, state):
    """Expected hold in seconds for (kind, state), or None for a terminal or
    unknown state."""
    for k, rx, f in _EXPECT_COMPILED:
        if k == kind and rx.search(state or ''):
            return int(f())
    return None


def hold_style(held, expect):
    """OK within the expectation, WIP up to 1.5x, BAD beyond."""
    if expect is None:
        return HDR
    if held <= expect:
        return OK
    return WIP if held <= 1.5 * expect else BAD


def timeline_block(L, header, steps, width, now_ts, kind=''):
    """One object's block: header line, then each state with the time it was
    entered and how long it was held (the current state: so far, with the
    expected hold and a colour: on time, over, well over). Consecutive
    identical states are folded into one line with a count."""
    folded = []
    for ts, state in steps:
        if folded and folded[-1][1] == state:
            folded[-1][2] += 1
        else:
            folded.append([ts, state, 1])
    L.append([])
    L.append(header)
    for i, (ts, state, n) in enumerate(folded):
        current = i + 1 == len(folded)
        end = now_ts if current else parse_ts(folded[i + 1][0])
        held = int((end - parse_ts(ts)).total_seconds())
        expect = expected_for(kind, state)
        tail = f'so far {fmt_age(held)}' if current else f'held {fmt_age(held)}'
        if expect is not None:
            tail += f' (expect ~{fmt_age(expect)})'
        if n > 1:
            tail += f'  (x{n})'
        sty = hold_style(held, expect) if current else HDR
        L.append([(f'    {ts[11:19]}  ', PLAIN), (f'{short(state, max(30, width - 60)):<{max(30, width - 60)}}', state_style(state)), (f'  {tail}', sty)])


def sec_timeline(width):
    """Per object, its states in order with how long each was held. Machines
    (hosts and DPUs) come from NICo's own state history, `machine show <id>
    -c 250`: complete, exactly timed, independent of when the monitor started,
    and per run, since a fleet reset deletes the machine and its history.
    Endpoints and DPF resources have no history in NICo, so they come from the
    poll diary, restricted to the current run (after the last fleet-reset
    marker)."""
    L = [[('TIMELINE', SECTION),
          (f'   per object, oldest first; "held" = time in that state (current state: so far); '
           f'machines from NICo\'s state history, endpoints and DPF from the poll diary (current run)', PLAIN),
          (filter_note(), NUM)],
         [(f'   expected holds from {MAT_TIMING["source"]}: host reboot {fmt_age(int(_mat("reboot")))}, '
           f'BMC reset {fmt_age(int(_mat("bmc_reset")))}, firmware task {fmt_age(int(_mat("firmware_upgrade")))}, '
           f'plus NICo\'s {NICO_PASS} s passes; ', PLAIN),
          ('on time', OK), (' / ', PLAIN), ('over', WIP), (' / ', PLAIN), ('well over (1.5x)', BAD)]]
    now_ts = datetime.now(timezone.utc)
    shown = 0

    for mid in sorted(MACHINE_HISTORY, key=lambda m: (LABELS.get(m, ('zz',))[0] != 'host', m)):
        if not matches('machine', mid):
            continue
        entry = MACHINE_HISTORY[mid]
        steps = [(ts, state) for ts, state, _ in entry['rows']]
        if not steps:
            continue
        shown += 1
        now = '(gone)' if entry['gone'] else entry['state']
        header = [(f'  machine {label("machine", mid, 72)}', HDR),
                  (f'   {short(entry["type"], 16)}, {len(steps)} state(s) in NICo, now: ', PLAIN),
                  (short(now, 40), state_style(now))]
        timeline_block(L, header, steps, width, now_ts, kind='machine')

    # endpoints and DPF phases: the diary, current run only
    events = filtered_events()
    resets = [i for i, e in enumerate(events) if e[1] == 'reset']
    events = events[resets[-1] + 1:] if resets else events
    by_obj = OrderedDict()
    for now, kind, ident, old, new in events:
        if kind in ('endpoint', 'dpf'):
            by_obj.setdefault((kind, ident), []).append((now, new))
    for (kind, ident) in sorted(by_obj, key=lambda k: (KIND_ORDER.get(k[0], 9), k[1])):
        steps = by_obj[(kind, ident)]
        shown += 1
        header = [(f'  {kind} {label(kind, ident, 72)}', HDR), (f'   {len(steps)} state(s) observed, now: ', PLAIN),
                  (short(steps[-1][1], 40), state_style(steps[-1][1]))]
        timeline_block(L, header, steps, width, now_ts, kind=kind)
        # nico-api's firmware decisions for this BMC, from the pod log, in order
        if kind == 'endpoint' and APILOG:
            for ts, _, msg, fields in APILOG.for_addr(ident, run_start()):
                text, style = APILOG.decision(msg, fields) or (msg, 'plain')
                L.append([(f'    {ts[11:19]}  ', PLAIN), ('nico-api: ', HDR), (short(text, max(30, width - 30)), STYLE_BY_NAME[style])])

    if not shown:
        L.append([('  (nothing yet)' if not (MACHINE_HISTORY or HISTORY.events) else f'  (nothing matches "{FILTER}")', HDR)])
    return L


def collapsed(name, server, dpf, logs):
    """The one-line stand-in for a section hidden on the overview page."""
    key = {n: k for n, k, _ in SECTIONS}[name]
    if name == 'endpoints':
        txt = f'  {len(server.get("endpoints", []))}'
    elif name == 'machines':
        txt = f'  {len(server.get("machines", []))}'
    elif name == 'dpus':
        txt = f'  {len(server.get("dpus", []))}'
    elif name == 'dpf':
        if dpf is None:
            return None
        txt = f'  {len(dpf["dpus"])} DPUs, {sum(1 for d in dpf["dpus"] if d["phase"] == "Ready")} Ready'
    elif name == 'history':
        txt = f'  {len(HISTORY.events)} transitions'
    elif name == 'timeline':
        txt = f'  {len(MACHINE_HISTORY)} machines, {len({(k, i) for _, k, i, _, _ in HISTORY.events if k in ("endpoint", "dpf")})} endpoints/DPF'
    else:
        if not logs:
            return None
        txt = f'  {len(logs)} log(s)'
    return [(name.upper() if name != 'dpus' else 'DPUS (NICo)', SECTION), (f'{txt} (hidden — {key} to show, {PAGES.index(name)} for its page)', HDR)]


def render_section(name, server, logs, dpf, width):
    if name == 'endpoints':
        return sec_endpoints(server, width)
    if name == 'machines':
        return sec_machines(server, width)
    if name == 'dpus':
        return sec_dpus(server, width)
    if name == 'dpf':
        return sec_dpf(dpf, width)
    if name == 'history':
        return sec_history(width)
    if name == 'timeline':
        return sec_timeline(width)
    return sec_mat(logs, width)


def render(server, logs, admin_cli, interval, width=120, dpf=None, show=None, page='all'):
    """Lines for one page. 'all' is the overview: every section, the hidden ones
    collapsed to a line. Any other page is that section alone, in full."""
    show = show if show is not None else DEFAULT_SHOW
    L = render_header(server, admin_cli, interval, page)
    if page != 'all':
        L.extend(render_section(page, server, logs, dpf, width))
        return L
    for name, _, _ in SECTIONS:
        if name == 'dpf' and dpf is None:
            continue
        if name == 'mat' and not logs:
            L.extend(sec_mat(logs, width))
            continue
        if name in show:
            L.extend(render_section(name, server, logs, dpf, width))
        else:
            line = collapsed(name, server, dpf, logs)
            if line is None:
                continue
            L.append(line)
        L.append([])
    return L


HELP = [
    ('PAGES', [
        ('0', 'overview: every section expanded except MAT, which is collapsed to a count line'),
        ('1', 'endpoints — the site explorer\'s BMC endpoints, pre-ingestion state, the machine each became; under each BMC, the latest firmware decision nico-api logged for it (from the pod log via kubectl)'),
        ('2', 'machines — every machine as NICo reports it, its lifecycle milestone and milestones to go; ids tagged [host <BMC address>] / [dpu <BMC address>]'),
        ('3', 'DPUs (NICo) — dpu status and dpf show: health, firmware version status, DPF enablement'),
        ('4', 'DPF (kubectl) — DPUNodes, DPUDevices, every DPU resource\'s phase on the simulator\'s happy path'),
        ('5', 'MAT — MAT\'s own view from its log: FSM state, API state it last saw, booted OS, last timer'),
        ('6', 'history — every state change seen since the monitor started (machines, endpoints, DPUs, DPF phases), with the poll time; also appended to the history file for reading after the run'),
        ('7', 'timeline — per object, its states in order and how long it held each: machines (hosts and DPUs) from NICo\'s own state history (machine show -c 250: complete, exact times, per run), endpoints and DPF phases from the poll diary since the last fleet reset. The current state shows its expected hold (GB200 profile x acceleration_factor from mat-config.toml, plus NICo\'s 30 s passes) and is coloured on time / over / well over. Under each endpoint, nico-api\'s firmware decisions for that BMC in order (check, upgrade, satisfied, not checked, complete), read from the pod log'),
        ('? h', 'this help; any page key, ?, h or 0 returns'),
    ]),
    ('MOVING', [
        ('0-7', 'go to that page'),
        ('→ Tab n', 'next page'), ('← Shift-Tab p', 'previous page'),
        ('↑ ↓ j k', 'scroll one line (pages 1-7)'), ('PgUp PgDn Space', 'scroll one screen'), ('Home End', 'top / bottom'),
    ]),
    ('OVERVIEW (page 0)', [
        ('e m u d l t y', 'collapse or expand endpoints / machines / DPUs / DPF / MAT / history / timeline'),
    ]),
    ('MAT PAGE (5)', [
        ('[ ]', 'previous / next MAT log when several were given (the newest is shown first)'),
        ('a', 'show every MAT log at once, or back to one'),
    ]),
    ('HISTORY / TIMELINE PAGES (6, 7)', [
        ('/', 'filter to one object: type part of a BMC address (matches the endpoint and the machine tagged with it), a machine id or a kind (machine, endpoint, dpu, dpf); empty clears. From another page, / jumps to the timeline'),
    ]),
    ('ALWAYS', [
        ('r', 'refresh now'), ('q', 'quit'),
    ]),
]


def render_help(server, admin_cli, interval):
    L = render_header(server, admin_cli, interval, 'help')
    L.append([('HELP', SECTION), ('   the monitor polls the admin CLI, kubectl and the MAT log on the refresh interval; keys act at once', PLAIN)])
    for title, rows in HELP:
        L.append([])
        L.append([(f'  {title}', HDR)])
        for key, what in rows:
            L.append([(f'    {key:<16}', NUM), (what, PLAIN)])
    L.append([])
    L.append([('  Columns', HDR)])
    L.append([('    to-go            ', NUM), ('milestones left before Ready (a range when the optional ones may be skipped); ✓ Ready, ✗ Failed', PLAIN)])
    L.append([('    in phase         ', NUM), ('how long the DPU resource has been in its current DPF phase', PLAIN)])
    L.append([('    API state        ', NUM), ('the machine state MAT last read from NICo; Unknown until MAT has discovered the machine', PLAIN)])
    L.append([('    last timer       ', NUM), ('the FSM timer MAT armed last (MachineOn = reboot, OsReady = power_on_os_ready, …) and when', PLAIN)])
    return L


def plain(lines):
    return '\n'.join(''.join(t for t, _ in line) for line in lines)


def run_plain(admin_cli, logs, interval, once, dpf_cfg=None):
    while True:
        server = fetch_server(admin_cli)
        dpf = fetch_dpf(*dpf_cfg) if dpf_cfg else None
        for log in logs:
            log.refresh()
        HISTORY.observe(server, dpf)
        if APILOG:
            APILOG.refresh()
        print(plain(render(server, logs, admin_cli, interval, dpf=dpf, show={n for n, _, _ in SECTIONS})))   # plain text: everything
        if once:
            return
        print('-' * 100, flush=True)
        time.sleep(interval)


def run_tui(admin_cli, logs, interval, dpf_cfg=None, default_log=None):
    # logs: every MAT log given; default_log: index of the one the MAT page
    # opens with (the most recently written), or None to open with all.
    # nothing may write to the terminal behind curses' back: Python warnings
    # and any stray stderr go to a file instead
    warnings.simplefilter('ignore')
    err_path = os.path.join(os.environ.get('TMPDIR', '/tmp'), 'monitor-mat.stderr')
    sys.stderr = open(err_path, 'a')

    # Fetching (admin CLI + kubectl, several seconds on the VM) runs in a
    # background thread so the screen keeps answering keys and resizes.
    data = {'server': {'errors': ['loading…']}, 'dpf': None, 'at': 0.0, 'busy': False}
    lock = threading.Lock()
    wake = threading.Event()
    stop = threading.Event()

    def fetcher():
        while not stop.is_set():
            with lock:
                data['busy'] = True
            server = fetch_server(admin_cli)
            dpf = fetch_dpf(*dpf_cfg) if dpf_cfg else None
            for log in logs:
                log.refresh()
            HISTORY.observe(server, dpf)
            if APILOG:
                APILOG.refresh()
            with lock:
                data.update(server=server, dpf=dpf, at=time.time(), busy=False)
            wake.wait(interval)
            wake.clear()

    def main(stdscr):
        global FILTER
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.keypad(True)
        # Arrow keys arrive as ESC sequences; with nodelay a slow terminal
        # can deliver the ESC alone, so Esc is not a quit key (q is) and the
        # sequence timeout is short.
        if hasattr(curses, 'set_escdelay'):
            curses.set_escdelay(50)
        show = set(DEFAULT_SHOW)
        keys = {k: name for name, k, _ in SECTIONS}
        threading.Thread(target=fetcher, daemon=True).start()
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
        page = 0      # index into PAGES; 0 = overview
        top = 0       # first body line on screen (scrolling); the header stays
        helping = False
        # Which MAT log the MAT page shows: an index into logs, or None for all.
        sel = default_log if len(logs) > 1 else (0 if logs else None)
        while True:
            with lock:
                server, dpf, at, busy = data['server'], data['dpf'], data['at'], data['busy']
            h, w = stdscr.getmaxyx()
            view_logs = logs if sel is None else [logs[sel]]
            if helping:
                lines = render_help(server, admin_cli, interval)
            else:
                lines = render(server, view_logs, admin_cli, interval, width=w, dpf=dpf, show=show, page=PAGES[page])
            n_head = 1 + 1 + len(server.get('errors', [])) + 1     # title, counts, errors, blank
            head, body = lines[:n_head], lines[n_head:]
            room = max(1, h - 2 - len(head))
            top = max(0, min(top, len(body) - room))
            stdscr.erase()
            for y, line in enumerate(head + body[top:top + room]):
                x = 0
                for text, style in line:
                    if x >= w - 1:
                        break
                    try:
                        stdscr.addnstr(y, x, text, w - 1 - x, attrs.get(style, curses.A_NORMAL))
                    except curses.error:
                        pass
                    x += len(text)
            remaining = max(0, int(interval - (time.time() - at))) if at else 0
            status = 'refreshing…' if busy else f'next in {remaining}s'
            pages = '  '.join(f'{i}:{name}' + ('*' if i == page and not helping else '') for i, name in enumerate(PAGES))
            if helping:
                extra = '   ? h 0 back'
            elif page == 0:
                extra = '   toggle: ' + '  '.join(f'{k}:{name}{"" if name in show else "(off)"}' for name, k, _ in SECTIONS)
            else:
                shown = f'{top + 1}-{min(len(body), top + room)}/{len(body)}' if len(body) > room else 'all'
                extra = f'   ↑↓ PgUp PgDn scroll ({shown})'
                if PAGES[page] == 'mat' and len(logs) > 1:
                    which = 'all logs' if sel is None else f'log {sel + 1}/{len(logs)} {os.path.basename(logs[sel].path)}'
                    extra += f'   [ ] a: {which}'
            if FILTER:
                extra += f'   filter: {FILTER}'
            try:
                stdscr.addnstr(h - 1, 0, f'q quit  r refresh  ? help  / filter  {status}   page ←→/Tab: {pages}{extra}', w - 1, curses.A_REVERSE)
            except curses.error:
                pass
            stdscr.refresh()
            ch = stdscr.getch()
            if ch in (ord('q'), ord('Q')):
                stop.set(); wake.set()
                return
            if ch in (ord('r'), ord('R')):
                wake.set()
            elif ch == curses.KEY_RESIZE:
                curses.update_lines_cols()
                stdscr.clear()
            elif ch in (ord('?'), ord('h'), ord('H')):
                helping, top = not helping, 0
                stdscr.clear()
            elif ch == ord('/'):
                # filter for the history and timeline pages: a substring of an
                # address, a machine id or a kind (machine/endpoint/dpu/dpf)
                stdscr.nodelay(False)
                curses.echo()
                try:
                    stdscr.addnstr(h - 1, 0, ' ' * (w - 1), w - 1, curses.A_REVERSE)
                    stdscr.addnstr(h - 1, 0, 'filter (id / address / kind, empty clears): ', w - 1, curses.A_REVERSE)
                    stdscr.refresh()
                    FILTER = stdscr.getstr(h - 1, 45, max(1, w - 47)).decode('utf-8', 'replace').strip()
                except curses.error:
                    pass
                curses.noecho()
                stdscr.nodelay(True)
                if PAGES[page] not in ('history', 'timeline'):
                    page = PAGES.index('timeline')
                top, helping = 0, False
                stdscr.clear()
            elif ch in (curses.KEY_RIGHT, ord('\t'), ord('n'), ord('N')):
                page, top, helping = (page + 1) % len(PAGES), 0, False
                stdscr.clear()
            elif ch in (curses.KEY_LEFT, curses.KEY_BTAB, ord('p'), ord('P')):
                page, top, helping = (page - 1) % len(PAGES), 0, False
                stdscr.clear()
            elif ord('0') <= ch <= ord('9') and ch - ord('0') < len(PAGES):
                page, top, helping = ch - ord('0'), 0, False
                stdscr.clear()
            elif ch in (ord('['), ord(']')) and len(logs) > 1:
                step = 1 if ch == ord(']') else -1
                sel, top = ((sel if sel is not None else (-1 if step > 0 else 0)) + step) % len(logs), 0
                stdscr.clear()
            elif ch in (ord('a'), ord('A')) and len(logs) > 1:
                sel, top = (None if sel is not None else default_log), 0
                stdscr.clear()
            elif ch in (curses.KEY_DOWN, ord('j')):
                top += 1
            elif ch in (curses.KEY_UP, ord('k')):
                top -= 1
            elif ch == curses.KEY_NPAGE or ch == ord(' '):
                top += room
            elif ch == curses.KEY_PPAGE:
                top -= room
            elif ch == curses.KEY_HOME:
                top = 0
            elif ch == curses.KEY_END:
                top = len(body)
            elif page == 0 and 0 <= ch < 256 and chr(ch).lower() in keys:
                name = keys[chr(ch).lower()]
                show.symmetric_difference_update({name})
                stdscr.clear()
            curses.napms(150)
    curses.wrapper(main)


def main():
    p = argparse.ArgumentParser(description='Watch a MAT run from NICo\'s and MAT\'s side',
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument('--admin-cli', required=True, help='the site\'s run-admin-cli.sh (or a configured nico-admin-cli)')
    p.add_argument('--mat-log', action='append', default=[], metavar='FILE',
                   help='MAT log file (repeatable, optional). With several, only the most recently '
                        'modified one is shown unless --all-logs is given')
    p.add_argument('--all-logs', action='store_true', help='show every --mat-log, not just the newest')
    p.add_argument('--interval', type=int, default=10, help='seconds between refreshes (default 10; also the resolution of the history)')
    p.add_argument('--history', default=None, metavar='FILE',
                   help='append every observed state change to this file (default: monitor-mat-history.log next to the admin CLI wrapper, i.e. in the site folder)')
    p.add_argument('--no-history', action='store_true', help='keep the history in memory only')
    p.add_argument('--filter', default='', metavar='SUBSTR',
                   help='history and timeline pages: only objects whose id, address or kind contains this (also "/" in the full-screen view)')
    p.add_argument('--once', action='store_true', help='one plain-text refresh, then exit')
    p.add_argument('--no-tui', action='store_true', help='plain text instead of the full-screen view')
    p.add_argument('--kubeconfig', default=None, help='for the DPF section (default: *.kubeconfig.yaml next to the admin CLI wrapper)')
    p.add_argument('--dpf-namespace', default='dpf-operator-system')
    p.add_argument('--no-dpf', action='store_true', help='skip the DPF (kubectl) section')
    p.add_argument('--no-api-log', action='store_true',
                   help='do not read nico-api\'s pre-ingestion firmware decisions from the pod log (kubectl logs deploy/nico-api)')
    p.add_argument('--api-namespace', default='nico-system', help='namespace of the nico-api Deployment (default nico-system)')
    a = p.parse_args()
    global HISTORY, APILOG, FILTER, SITE_DIR
    SITE_DIR = os.path.dirname(os.path.abspath(a.admin_cli))
    load_mat_timing(SITE_DIR)
    FILTER = a.filter.strip()
    if not a.no_history:
        HISTORY = Transitions(a.history or os.path.join(os.path.dirname(os.path.abspath(a.admin_cli)), 'monitor-mat-history.log'))
    mat_logs = a.mat_log
    # The launcher passes every log it finds (base, dev, plain); the run in
    # progress is the one written most recently. Showing the others as well
    # put yesterday's fleet on screen above today's (20260922-#2), so plain
    # output shows only the newest unless --all-logs, and the full-screen MAT
    # page opens on the newest but can switch ([ ] a) to the others.
    newest = None
    if len(mat_logs) > 1:
        present = [f for f in mat_logs if os.path.exists(f)]
        if present:
            newest = mat_logs.index(max(present, key=os.path.getmtime))
    logs = [MatLog(f) for f in mat_logs]
    plain_logs = logs if (a.all_logs or newest is None) else [logs[newest]]
    default_log = None if a.all_logs else newest
    kc = a.kubeconfig
    if not kc:
        found = sorted(f for f in os.listdir(SITE_DIR) if f.endswith('.kubeconfig.yaml')) if os.path.isdir(SITE_DIR) else []
        kc = os.path.join(SITE_DIR, found[0]) if found else None
    dpf_cfg = (kc, a.dpf_namespace) if not a.no_dpf else None
    if not a.no_api_log:
        APILOG = ApiLog(kc, a.api_namespace, HISTORY.path if not a.no_history else None)
    if a.once or a.no_tui or not sys.stdout.isatty():
        try:
            run_plain(a.admin_cli, plain_logs, a.interval, a.once, dpf_cfg)
        except KeyboardInterrupt:
            pass
    else:
        run_tui(a.admin_cli, logs, a.interval, dpf_cfg, default_log)


if __name__ == '__main__':
    main()
