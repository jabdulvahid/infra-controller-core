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
  --kubeconfig  for the DPF section (default: the *.kubeconfig.yaml next to the
                admin CLI wrapper, i.e. the site folder). --no-dpf skips it.
  --dpf-namespace  where the DPF resources live (default dpf-operator-system).

What it shows
  1. Expected machines registered with NICo (count; MAT registers its hosts).
  2. Site-explorer endpoints: address, type, vendor, pre-ingestion state, the
     machine each became, the last exploration error.
  3. Machines: state as NICo reports it, the lifecycle milestone that state
     belongs to, how many milestones remain to Ready, and Ready/Failed marks.
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

Pages (full-screen mode). Page 0 is the overview: every section, the ones
toggled off collapsed to a count line (e m u d l toggle them). Pages 1-5 show
one section alone, in full, and scroll: 1 endpoints, 2 machines, 3 DPUs
(NICo), 4 DPF, 5 MAT. Keys: the digit, or ←/→, Tab/Shift-Tab, n/p to step;
↑/↓ (j/k), PgUp/PgDn (Space), Home/End to scroll; r refreshes now; q quits.
Plain-text mode (--once, --no-tui) prints every section, as before.

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
# sections, their toggle key, and whether they show by default
SECTIONS = [('endpoints', 'e', False), ('machines', 'm', True), ('dpus', 'u', False), ('dpf', 'd', True), ('mat', 'l', True)]
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


def render_header(server, admin_cli, interval, page='all'):
    now = datetime.now().strftime('%H:%M:%S')
    machines = server.get('machines', [])
    ready = sum(1 for r in machines if col(r, 'state').lower().startswith('ready'))
    failed = sum(1 for r in machines if col(r, 'state').lower().startswith('failed'))
    hosts = sum(1 for r in machines if 'dpu' not in col(r, 'type').lower())
    where = '' if page == 'all' else f'   page {PAGES.index(page)}/{len(PAGES) - 1}: {page}'
    L = [[('MAT run monitor', TITLE), (f'  {now}  refresh {interval}s{where}   admin-cli: {short(admin_cli, 60)}', PLAIN)],
         [('expected machines: ', PLAIN), (str(len(server.get('expected', []))), NUM),
          ('    endpoints: ', PLAIN), (str(len(server.get('endpoints', []))), NUM),
          ('    machines: ', PLAIN), (str(len(machines)), NUM), (f' ({hosts} hosts, {len(machines) - hosts} DPUs)', PLAIN),
          ('    Ready: ', PLAIN), (f'{ready}/{len(machines)}', OK if machines and ready == len(machines) else NUM),
          ('    Failed: ', PLAIN), (str(failed), BAD if failed else PLAIN)]]
    for e in server.get('errors', []):
        L.append([(f'  ! {e}', BAD)])
    L.append([])
    return L


def sec_endpoints(server, width):
    L = [[('ENDPOINTS (site explorer)', SECTION)],
         [(f'  {"address":<14} {"type":<5} {"vendor":<8} {"pre-ingestion":<14} {"machine":<44} last error', HDR)]]
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
        L.append([(f'  {short(col(r, "id"), 44):<44} {short(col(r, "type"), 6):<6} {short(m, 17):<17} ', PLAIN),
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


def plain(lines):
    return '\n'.join(''.join(t for t, _ in line) for line in lines)


def run_plain(admin_cli, logs, interval, once, dpf_cfg=None):
    while True:
        server = fetch_server(admin_cli)
        dpf = fetch_dpf(*dpf_cfg) if dpf_cfg else None
        for log in logs:
            log.refresh()
        print(plain(render(server, logs, admin_cli, interval, dpf=dpf, show={n for n, _, _ in SECTIONS})))   # plain text: everything
        if once:
            return
        print('-' * 100, flush=True)
        time.sleep(interval)


def run_tui(admin_cli, logs, interval, dpf_cfg=None):
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
            with lock:
                data.update(server=server, dpf=dpf, at=time.time(), busy=False)
            wake.wait(interval)
            wake.clear()

    def main(stdscr):
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
        while True:
            with lock:
                server, dpf, at, busy = data['server'], data['dpf'], data['at'], data['busy']
            h, w = stdscr.getmaxyx()
            lines = render(server, logs, admin_cli, interval, width=w, dpf=dpf, show=show, page=PAGES[page])
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
            pages = '  '.join(f'{i}:{name}' + ('*' if i == page else '') for i, name in enumerate(PAGES))
            if page == 0:
                extra = '   toggle: ' + '  '.join(f'{k}:{name}{"" if name in show else "(off)"}' for name, k, _ in SECTIONS)
            else:
                shown = f'{top + 1}-{min(len(body), top + room)}/{len(body)}' if len(body) > room else 'all'
                extra = f'   ↑↓ PgUp PgDn scroll ({shown})'
            try:
                stdscr.addnstr(h - 1, 0, f'q quit  r refresh  {status}   page ←→/Tab: {pages}{extra}', w - 1, curses.A_REVERSE)
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
            elif ch in (curses.KEY_RIGHT, ord('\t'), ord('n'), ord('N')):
                page, top = (page + 1) % len(PAGES), 0
                stdscr.clear()
            elif ch in (curses.KEY_LEFT, curses.KEY_BTAB, ord('p'), ord('P')):
                page, top = (page - 1) % len(PAGES), 0
                stdscr.clear()
            elif ord('0') <= ch <= ord('9') and ch - ord('0') < len(PAGES):
                page, top = ch - ord('0'), 0
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
    p.add_argument('--interval', type=int, default=30, help='seconds between refreshes (default 30)')
    p.add_argument('--once', action='store_true', help='one plain-text refresh, then exit')
    p.add_argument('--no-tui', action='store_true', help='plain text instead of the full-screen view')
    p.add_argument('--kubeconfig', default=None, help='for the DPF section (default: *.kubeconfig.yaml next to the admin CLI wrapper)')
    p.add_argument('--dpf-namespace', default='dpf-operator-system')
    p.add_argument('--no-dpf', action='store_true', help='skip the DPF (kubectl) section')
    a = p.parse_args()
    mat_logs = a.mat_log
    if len(mat_logs) > 1 and not a.all_logs:
        # The launcher passes every log it finds (base, dev, plain); the run in
        # progress is the one written most recently. Showing the others as well
        # put yesterday's fleet on screen above today's (20260922-#2).
        present = [f for f in mat_logs if os.path.exists(f)]
        if present:
            mat_logs = [max(present, key=os.path.getmtime)]
    logs = [MatLog(f) for f in mat_logs]
    dpf_cfg = None
    if not a.no_dpf:
        kc = a.kubeconfig
        if not kc:
            site_dir = os.path.dirname(os.path.abspath(a.admin_cli))
            found = sorted(f for f in os.listdir(site_dir) if f.endswith('.kubeconfig.yaml')) if os.path.isdir(site_dir) else []
            kc = os.path.join(site_dir, found[0]) if found else None
        dpf_cfg = (kc, a.dpf_namespace)
    if a.once or a.no_tui or not sys.stdout.isatty():
        try:
            run_plain(a.admin_cli, logs, a.interval, a.once, dpf_cfg)
        except KeyboardInterrupt:
            pass
    else:
        run_tui(a.admin_cli, logs, a.interval, dpf_cfg)


if __name__ == '__main__':
    main()
