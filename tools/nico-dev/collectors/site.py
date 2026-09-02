"""Site context — loads and derives all site-level facts from a nico-dev site yaml.

Key differences from nsim:
  - No control_plane section (no virsh VMs)
  - No managed_hosts section
  - dc_name from fabric.dc_name
  - clab_name == dc_name
  - Registry from registry.host:registry.port
  - kubeconfig from yaml 'kubeconfig' field (relative to site folder), or auto-detected
  - _ips loaded from fabric/fabric-ips.json if present
"""

import json
from pathlib import Path


def resolve_site(arg):
    """Return (site_yaml_path, site_folder_path) for a file or folder arg."""
    p = Path(arg).expanduser().resolve()
    if p.is_dir():
        yamls = [
            f for f in p.glob('*.yaml')
            if not f.stem.endswith('-mac') and '.kubeconfig' not in f.name
        ]
        if not yamls:
            raise ValueError(f'No site yaml found in {p}')
        if len(yamls) > 1:
            raise ValueError(f'Multiple site yamls in {p}: {[f.name for f in yamls]}')
        return str(yamls[0]), str(p)
    return str(p), str(p.parent)


def _find_kubeconfig(sim, site_folder):
    """Locate the kubeconfig for this site."""
    # 1. Explicit field in yaml
    kube_filename = sim.get('kubeconfig')
    if kube_filename and site_folder:
        candidate = Path(site_folder) / kube_filename
        if candidate.exists():
            return str(candidate)

    # 2. Any *.kubeconfig.yaml in the site folder
    if site_folder:
        kube_files = list(Path(site_folder).glob('*.kubeconfig.yaml'))
        if kube_files:
            return str(sorted(kube_files)[0])

    # 3. dc_name-sitename based legacy path
    fab      = sim.get('fabric', {})
    dc_name  = fab.get('dc_name', 'dev')
    ns       = sim.get('nico-system', {})
    hv       = ns.get('helm-values', {})
    sitename = hv.get('sitename', '')
    kube_id  = f'{dc_name}-{sitename}' if sitename else dc_name
    legacy = Path(f'~/.kube/config-{kube_id}').expanduser()
    if legacy.exists():
        return str(legacy)

    return None


def _load_ips(site_folder):
    """Load fabric-ips.json from site_folder/fabric/ if present."""
    if not site_folder:
        return {}
    ips_path = Path(site_folder) / 'fabric' / 'fabric-ips.json'
    if ips_path.exists():
        try:
            with open(ips_path) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _on_vm(site_folder, sim):
    """Which side is ndev running on? The site yaml records BOTH views of the
    share — nico_vm_folder (guest path) and nico_mac_folder (host path) — and
    the resolved site folder lives under exactly one of them. The platform is
    NOT the tell: a Linux host looks just like the VM, and with same-named
    leftovers in the host's docker it happily reported someone else's fabric
    (20260902-#8). Unknown layout → assume VM (the historical default)."""
    import os
    import sys
    sf = os.path.realpath(site_folder) + '/'
    for key, answer in (('nico_vm_folder', True), ('nico_mac_folder', False)):
        root = sim.get(key)
        if root and sf.startswith(os.path.realpath(os.path.expanduser(root)).rstrip('/') + '/'):
            return answer
    return sys.platform != 'darwin'


def collect(site_arg):
    """
    Return a dict with all site-level facts derived from the nico-dev site yaml.
    This is the base data every other collector builds on.
    """
    import yaml

    site_yaml, site_folder = resolve_site(site_arg)

    with open(site_yaml) as f:
        sim = yaml.safe_load(f)

    fab      = sim.get('fabric', {})
    ns       = sim.get('nico-system', {})
    hv       = ns.get('helm-values', {})
    np       = hv.get('net-plan', {})
    reg      = sim.get('registry', {})

    dc_name  = fab.get('dc_name', 'dev')
    sitename = hv.get('sitename', '')
    site_id  = f'{dc_name}-{sitename}' if sitename else dc_name

    registry = ''
    reg_host = reg.get('host', '')
    reg_port = reg.get('port', 5000)
    if reg_host:
        registry = f'{reg_host}:{reg_port}'

    return {
        '_sim':         sim,
        '_site_folder': site_folder,
        '_site_yaml':   site_yaml,
        '_kubeconfig':  _find_kubeconfig(sim, site_folder),
        '_ips':         _load_ips(site_folder),
        '_on_vm':       _on_vm(site_folder, sim),   # False = off-host (Mac/Linux host)

        'dc_name':      dc_name,
        'sitename':     sitename,
        'site_id':      site_id,
        'clab_name':    dc_name,   # ContainerLab lab name == dc_name

        'api_vip':      np.get('api_vip', ''),
        'registry':     registry,
    }
