#!/usr/bin/env python3
"""
ndev — nico-dev interactive CLI

Inspect a nico-dev site: kubeadm cluster on host + DPU as ContainerLab container.
No virsh VMs — everything runs in a single Linux VM or as clab containers.

Usage:
  ndev <site>                           # overall summary
  ndev <site> info                      # same as above

  ndev <site> fabric info               # ContainerLab switches + BGP summary
  ndev <site> fabric info --detail      # + per-switch BGP tables
  ndev <site> fabric verify             # full health check
  ndev <site> fabric verify --no-ping   # skip loopback pings
  ndev <site> fabric verify --switch-only  # non-switch peers not counted as failures
  ndev <site> fabric shell              # list switches (VM only)
  ndev <site> fabric shell spine-1      # vtysh on a switch — explore BGP/EVPN

  ndev <site> bgp info                  # BGP session summary across fabric
  ndev <site> bgp info --detail         # per-node, per-peer, per-AF breakdown

  ndev <site> dpu info                  # DPU stand-in container status
  ndev <site> cluster info               # k8s node status (aliases: k8s, k3s)

  ndev <site> registry verify           # list images/tags; on VM also checks containerd config

On a golden-image VM, ndev is preinstalled (/usr/local/bin/ndev) and the
<site> argument is optional — it defaults to the site in /etc/nico-dev/env,
so plain `ndev` or `ndev fabric verify` works out of the box.

Install as ndev (Mac):
  ln -sf <share>/<nico-dev-folder>/ndev.py ~/.local/bin/ndev
  chmod +x ~/.local/bin/ndev

All contexts support --json (machine-readable output for scripting / web UI).
"""

import json
import sys
from pathlib import Path

# Allow running from anywhere, including via symlink.
# Insert the nico-dev/ directory itself so 'collectors' and 'renderers'
# are importable as top-level packages (the directory name has a dash,
# which is not a valid Python identifier).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from collectors import site          as site_col
from collectors import fabric        as fabric_col
from collectors import bgp           as bgp_col
from collectors import dpu           as dpu_col
from collectors import cluster       as cluster_col
from collectors import registry      as registry_col
from collectors import verify_fabric as verify_col
from renderers  import table         as tbl


CONTEXT_ALIASES = {
    'network': 'fabric', 'net': 'fabric',
    'node':    'cluster', 'k3s': 'cluster', 'k8s': 'cluster',
}

CONTEXTS = ['fabric', 'bgp', 'dpu', 'cluster', 'registry']


def parse_args(argv):
    """
    Returns: (site_arg, context, subcommand, detail, json_out)
    """
    # Golden-image convenience: when the first arg is missing or is a context
    # name rather than a site path, fall back to the site recorded in
    # /etc/nico-dev/env (written by first-boot.sh / deploy-dev-fabric.py) —
    # so on a provisioned VM plain `ndev` or `ndev fabric verify` just works.
    def _env_site():
        env = Path('/etc/nico-dev/env')
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith('NICO_DEV_SITE='):
                    return line.split('=', 1)[1].strip()
        return None

    ctx_words = set(CONTEXTS) | set(CONTEXT_ALIASES) | {'info'}
    if len(argv) < 2:
        fallback = _env_site()
        if fallback:
            argv = [argv[0], fallback]
        else:
            print(__doc__)
            sys.exit(0)
    elif argv[1] in ctx_words:
        fallback = _env_site()
        if fallback:
            argv = [argv[0], fallback] + argv[1:]
        else:
            print(f'Error: first argument must be a site path (no /etc/nico-dev/env '
                  f'fallback found on this machine)', file=sys.stderr)
            sys.exit(1)

    site_arg = argv[1]

    if site_arg in ('-h', '--help', 'help'):
        print(__doc__)
        sys.exit(0)

    ctx     = 'info'
    subcmd  = 'info'
    detail  = '--detail' in argv or '-d' in argv
    json_out = '--json' in argv

    clean = [a for a in argv[2:] if a not in ('--detail', '-d', '--json')]

    if len(clean) == 0:
        ctx, subcmd = 'info', 'info'
    elif len(clean) == 1:
        arg = CONTEXT_ALIASES.get(clean[0], clean[0])
        if arg in CONTEXTS:
            ctx, subcmd = arg, 'info'
        elif arg == 'info':
            ctx, subcmd = 'info', 'info'
        else:
            ctx, subcmd = arg, 'info'
    elif len(clean) >= 2:
        ctx    = CONTEXT_ALIASES.get(clean[0], clean[0])
        subcmd = clean[1]

    return site_arg, ctx, subcmd, detail, json_out


def run_context(site_data, ctx, subcmd, detail, json_out):
    """Run a single context collector and render."""

    if ctx == 'fabric':
        if subcmd in ('shell', 'vtysh'):
            _fabric_shell(site_data, sys.argv)
            return
        if subcmd == 'verify':
            no_ping     = '--no-ping'     in sys.argv
            switch_only = '--switch-only' in sys.argv
            dns_host    = next((sys.argv[sys.argv.index('--dns-host') + 1]
                                for _ in ['x'] if '--dns-host' in sys.argv),
                               'archive.ubuntu.com')
            healthy = verify_col.run_checks(site_data, no_ping=no_ping,
                                            switch_only=switch_only, dns_host=dns_host,
                                            json_out=json_out)
            sys.exit(0 if healthy else 1)
        data = fabric_col.collect_detail(site_data) if detail else fabric_col.collect(site_data)
        if json_out:
            print(json.dumps({k: v for k, v in data.items() if not k.startswith('_')},
                              indent=2, default=str))
        else:
            print(tbl.render_fabric(data, detail=detail))

    elif ctx == 'bgp':
        data = bgp_col.collect(site_data)
        if json_out:
            print(json.dumps({k: v for k, v in data.items() if not k.startswith('_')},
                              indent=2, default=str))
        else:
            print(tbl.render_bgp(data, detail=detail))

    elif ctx == 'dpu':
        data = dpu_col.collect(site_data)
        if json_out:
            print(json.dumps({k: v for k, v in data.items() if not k.startswith('_')},
                              indent=2, default=str))
        else:
            print(tbl.render_dpu(data, detail=detail))

    elif ctx == 'cluster':
        data = cluster_col.collect(site_data)
        if json_out:
            print(json.dumps({k: v for k, v in data.items() if not k.startswith('_')},
                              indent=2, default=str))
        else:
            print(tbl.render_cluster(data, detail=detail))

    elif ctx == 'registry':
        data = registry_col.collect(site_data)
        if json_out:
            print(json.dumps({k: v for k, v in data.items() if not k.startswith('_')},
                              indent=2, default=str))
        else:
            print(tbl.render_registry(data))
        if not data['reachable']:
            sys.exit(1)

    else:
        print(f'Unknown context: {ctx}. Available: {", ".join(CONTEXTS)}', file=sys.stderr)
        sys.exit(1)


def _fabric_shell(site_data, argv):
    """Drop into vtysh on a fabric switch: ndev <site> fabric shell [node].

    Bare 'fabric shell' lists the nodes. Runs only on the VM (needs the
    local Docker daemon that hosts the clab containers).
    """
    import os
    import subprocess
    lab = site_data.get('clab_name') or site_data.get('dc_name', 'dev')

    # Node list from the generated topology (bridges excluded)
    topo_path = Path(site_data.get('_site_folder', '')) / 'fabric' / 'topo.clab.yml'
    nodes = []
    if topo_path.exists():
        try:
            import yaml as _yaml
            topo = _yaml.safe_load(topo_path.read_text())
            nodes = [n for n, spec in topo['topology']['nodes'].items()
                     if (spec or {}).get('kind') != 'bridge']
        except Exception:
            pass

    # Node name = the token right after 'shell'/'vtysh' (position-independent:
    # the site argument may have been injected by the /etc/nico-dev/env fallback)
    tail = [a for a in argv[1:] if not a.startswith('-')]
    target = None
    for i, a in enumerate(tail):
        if a in ('shell', 'vtysh'):
            target = tail[i + 1] if len(tail) > i + 1 else None
            break

    if not target:
        print('Fabric nodes (vtysh shells into FRR):')
        for n in nodes or ['(topology not found — run deploy-dev-fabric.py first)']:
            print(f'  {n}')
        print(f'\nUsage: ndev <site> fabric shell <node>')
        print("Try:   show bgp summary / show ip route / show bgp l2vpn evpn")
        print('The fabric is a SAFE sandbox: config changes are ephemeral —')
        print('sudo systemctl restart nico-dev-fabric restores everything.')
        return

    if nodes and target not in nodes:
        print(f"Unknown node '{target}'. Available: {', '.join(nodes)}", file=sys.stderr)
        sys.exit(1)

    cname = f'clab-{lab}-{target}'
    print(f'→ vtysh on {cname}  (exit with: exit / Ctrl-D)')
    try:
        os.execvp('docker', ['docker', 'exec', '-it', cname, 'vtysh'])
    except FileNotFoundError:
        print('Error: docker not found — fabric shell only works on the VM.',
              file=sys.stderr)
        sys.exit(1)


def run_info(site_data, detail, json_out):
    """Run all collectors and show overall summary."""
    fabric_data = fabric_col.collect(site_data)
    dpu_data    = dpu_col.collect(site_data)
    cluster_data = cluster_col.collect(site_data)
    bgp_data    = bgp_col.collect(site_data)

    if json_out:
        out = {
            'site':    {k: v for k, v in site_data.items() if not k.startswith('_')},
            'fabric':  {k: v for k, v in fabric_data.items() if not k.startswith('_')},
            'dpu':     {k: v for k, v in dpu_data.items() if not k.startswith('_')},
            'cluster': {k: v for k, v in cluster_data.items() if not k.startswith('_')},
            'bgp':     {k: v for k, v in bgp_data.items() if not k.startswith('_')},
        }
        print(json.dumps(out, indent=2, default=str))
        return

    print(tbl.render_info(site_data, fabric=fabric_data, dpu=dpu_data,
                          cluster=cluster_data, bgp=bgp_data))

    if detail:
        print(tbl.render_fabric(fabric_data, detail=True))
        print(tbl.render_dpu(dpu_data, detail=True))
        print(tbl.render_cluster(cluster_data, detail=True))
        print(tbl.render_bgp(bgp_data, detail=True))


def main():
    site_arg, ctx, subcmd, detail, json_out = parse_args(sys.argv)

    try:
        site_data = site_col.collect(site_arg)
    except Exception as e:
        print(f'Error loading site: {e}', file=sys.stderr)
        sys.exit(1)

    if ctx in ('info', '') and subcmd in ('info', ''):
        run_info(site_data, detail, json_out)
    else:
        run_context(site_data, ctx, subcmd, detail, json_out)


if __name__ == '__main__':
    main()
