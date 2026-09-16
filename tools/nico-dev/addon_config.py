"""
nico-dev — the per-add-on configuration file (flow.yaml, rms.yaml, …), in one place.

Ruling (Jasmeer, 2026-09-16): bringup.yaml and the base image model cover only
what every site needs — core, REST, DPF. Optional charts are a growing list
with their own release cadence, so each one is deployed by its own script,
at the user's discretion, after bring-up, from its OWN yaml. That yaml is
STANDALONE: nothing is inherited from the site yaml, so a site deployed from
NGC can run a locally built add-on and vice versa, and a change to the base
image model can never reach an add-on. Add-ons write nothing into the site
yaml either — Helm already records what is installed (helm list / helm get
values), and cleanup is the chart's own uninstall.

What an add-on needs from the site, and where it comes from without reading
the site yaml:
  - the cluster:  the kubeconfig in the site FOLDER (<dc>-<site>.kubeconfig.yaml)
  - the chart:    the checkout the tools are grafted into, or build.repo
  - the registry: `registry:` in the add-on yaml (nico-dev's convention as default)
  - the NGC key:  `ngc.token_env` in the add-on yaml

The file, same shape for every add-on:

  source: ngc                          # ngc | build            (required)
  registry: 192.168.64.1:5000          # where the cluster pulls images (default shown)
  ngc:                                 # required when source is ngc
    registry: nvcr.io/<org>/<team>     #   NGC base holding the add-on's images
    tag: v2.3.0-pr-512-gabcdef0        #   NGC tag
    images: {nico-flow: nico-flow}     #   local chart name: NGC name (default: same name)
    token_env: NGC_API_KEY             #   env var NAME holding the key (default shown)
  build:                               # required when source is build
    tag: flow-20260916                 #   image label in the local registry
    repo: ~/path/to/checkout           #   default: the checkout holding these tools
  chart:                               # this chart's own knobs, passed through by its script
    flowEnv: development

Missing required fields are errors that name the field; nothing is guessed.
"""

import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print('Error: pyyaml not installed', file=sys.stderr)
    sys.exit(1)

HERE = Path(__file__).resolve().parent
DEFAULT_REGISTRY = '192.168.64.1:5000'     # the host as the VM sees it (nico-dev convention)
TOP_KEYS = {'source', 'registry', 'ngc', 'build', 'chart'}


class AddonConfig:
    def __init__(self, name, path, data, local_images):
        self.name, self.path = name, path
        self.source = data['source']
        self.registry = data['registry']                       # as the cluster pulls
        self.push_reg = f'localhost:{self.registry.rsplit(":", 1)[-1]}'   # as this host pushes
        self.ngc = data.get('ngc') or {}
        self.build = data.get('build') or {}
        self.chart = data.get('chart') or {}
        self.local_images = list(local_images)
        # {local name: NGC name}, every local name present
        given = dict(self.ngc.get('images') or {})
        self.ngc_names = {n: str(given.get(n, n)) for n in local_images}
        self.local_tag = f'ngc-{self.ngc["tag"]}' if self.source == 'ngc' else str(self.build['tag'])

    @property
    def repo(self):
        if self.source == 'build' and self.build.get('repo'):
            return Path(self.build['repo']).expanduser().resolve()
        return tools_checkout()

    def describe(self):
        lines = [f'  config     : {self.path}',
                 f'  source     : {self.source}',
                 f'  registry   : {self.registry}  (push via {self.push_reg})',
                 f'  local tag  : {self.local_tag}']
        if self.source == 'ngc':
            lines.append(f'  NGC        : {self.ngc["registry"]} @ {self.ngc["tag"]}  (key in ${self.ngc["token_env"]})')
            renamed = {k: v for k, v in self.ngc_names.items() if k != v}
            if renamed:
                lines.append(f'  NGC names  : {renamed}')
        else:
            lines.append(f'  build repo : {self.repo}')
        if self.chart:
            lines.append(f'  chart      : {self.chart}')
        return '\n'.join(lines)


def tools_checkout():
    """The repo checkout these tools are grafted into (None when not in a checkout)."""
    r = subprocess.run(['git', '-C', str(HERE), 'rev-parse', '--show-toplevel'],
                       capture_output=True, text=True)
    return Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None


def find_kubeconfig(site_folder):
    """The site's kubeconfig, by name pattern in the site folder — no site yaml read."""
    folder = Path(site_folder).expanduser()
    if folder.is_file():
        folder = folder.parent
    if not folder.is_dir():
        sys.exit(f'Error: site folder not found: {folder}')
    kcs = sorted(folder.glob('*.kubeconfig.yaml'))
    if len(kcs) != 1:
        sys.exit(f'Error: expected exactly one *.kubeconfig.yaml in {folder}, found {len(kcs)} '
                 f'— is the site deployed?')
    return kcs[0]


def load(path, local_images, name):
    """Load and validate <name>.yaml. local_images = the chart's fixed local image names."""
    if not path:
        sys.exit(f'Error: {name} needs its own config: --config {name}.yaml '
                 f'(start from {name}-example.yaml next to this script)')
    p = Path(path).expanduser()
    if not p.is_file():
        sys.exit(f'Error: config not found: {p}')
    data = yaml.safe_load(p.read_text()) or {}
    if not isinstance(data, dict):
        sys.exit(f'Error: {p}: expected a yaml map')
    unknown = sorted(set(data) - TOP_KEYS)
    if unknown:
        sys.exit(f'Error: {p}: unknown key(s) {", ".join(unknown)} — valid: {", ".join(sorted(TOP_KEYS))}')
    src = data.get('source')
    if src not in ('ngc', 'build'):
        sys.exit(f'Error: {p}: source must be ngc or build (got {src!r})')
    data['registry'] = str(data.get('registry') or DEFAULT_REGISTRY)
    if src == 'ngc':
        ngc = data.get('ngc') or {}
        for k in ('registry', 'tag'):
            if not ngc.get(k):
                sys.exit(f'Error: {p}: ngc.{k} is required when source is ngc')
        ngc['tag'] = str(ngc['tag'])
        ngc['token_env'] = str(ngc.get('token_env') or 'NGC_API_KEY')
        imgs = ngc.get('images') or {}
        if not isinstance(imgs, dict):
            sys.exit(f'Error: {p}: ngc.images must be a map of local chart name → NGC name')
        bad = sorted(set(imgs) - set(local_images))
        if bad:
            sys.exit(f'Error: {p}: ngc.images: {", ".join(bad)} is not one of this chart\'s images '
                     f'(fixed by the chart): {", ".join(local_images)}')
        data['ngc'] = ngc
    else:
        b = data.get('build') or {}
        if not b.get('tag'):
            sys.exit(f'Error: {p}: build.tag is required when source is build')
        if b.get('repo') and not Path(b['repo']).expanduser().is_dir():
            sys.exit(f'Error: {p}: build.repo does not exist: {b["repo"]}')
        if not b.get('repo') and tools_checkout() is None:
            sys.exit(f'Error: {p}: build.repo is required (these tools are not inside a checkout)')
        data['build'] = b
    if data.get('chart') is not None and not isinstance(data['chart'], dict):
        sys.exit(f'Error: {p}: chart must be a map')
    return AddonConfig(name, p, data, local_images)
