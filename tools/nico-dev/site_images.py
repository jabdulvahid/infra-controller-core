"""
nico-dev — the image model of a site, in one place.

Every NICo image the dev site runs lives in ONE registry at ONE tag, under a
FIXED set of names. This module spells the set out (instead of each script
carrying its own list) and reads/writes the `images:` section of the site
yaml, which is the source of truth for what the cluster runs and where it
came from:

  images:
    registry: 192.168.64.1:5000      # where the cluster pulls from
    tag: ngc-v2.2.0-…                # what is DEPLOYED (written by the deploy scripts)
    source:
      kind: ngc | build              # how the images were produced
      registry: nvcr.io/<org>/<team> # NGC base — every image lives here (ngc)
      tag: v2.2.0-…                  # the NGC tag (ngc) / the built tag (build)
      core_image: nvmetal-carbide    # NGC's name for the core image (local: nico)
      token_env: NGC_API_KEY         # env var NAME of the NGC key (never the value)
    names:
      core: nico
      rest: [nico-rest-api, …]
      flow: [nico-flow, nico-psm, nico-nsm]

Writes are line-oriented edits inside the `images:` block so the yaml's
comments survive (yaml.dump would strip them). Older site yamls without the
block get one appended. Readers fall back to the legacy `registry:` block.
"""

import re
from pathlib import Path

# The fixed set. NGC publishes REST and Flow under exactly these names at the
# same tag as the core image; only the core is named differently on NGC
# (nvmetal-carbide) and locally (nico).
IMAGE_NAMES = {
    'core': 'nico',
    'rest': ['nico-rest-api', 'nico-rest-workflow', 'nico-rest-site-manager',
             'nico-rest-site-agent', 'nico-rest-db', 'nico-rest-cert-manager'],
    # Flow: one container since upstream #5325 (2026-08-31) removed PSM/NSM
    # from the flow pod; nico-psm / nico-nsm are still published but no
    # longer deployed. deploy-flow.py reads the checkout's chart for the
    # actual set, so older checkouts keep working.
    'flow': ['nico-flow'],
}
NGC_CORE_IMAGE_DEFAULT = 'nvmetal-carbide'


def all_names(groups=('core', 'rest', 'flow')):
    out = []
    for g in groups:
        v = IMAGE_NAMES[g]
        out += v if isinstance(v, list) else [v]
    return out


def read(cfg):
    """Return {'registry','tag','source':{...},'names':{...}} from a loaded
    site yaml dict, falling back to the legacy registry: block."""
    reg = cfg.get('registry', {}) or {}
    legacy_registry = f'{reg.get("host", "192.168.64.1")}:{reg.get("port", 5000)}'
    img = dict(cfg.get('images', {}) or {})
    img.setdefault('registry', legacy_registry)
    img.setdefault('tag', reg.get('nico_tag', ''))
    img['source'] = dict(img.get('source', {}) or {})
    img['names'] = img.get('names') or IMAGE_NAMES
    return img


def split_ngc_image(ngc_image):
    """'nvcr.io/org/team/nvmetal-carbide' → ('nvcr.io/org/team', 'nvmetal-carbide')."""
    base, _, name = ngc_image.rpartition('/')
    return base, name


def _block_span(lines):
    """(start, end) line indexes of the top-level images: block, or None."""
    start = None
    for i, l in enumerate(lines):
        if start is None:
            if re.match(r'^images:\s*(#.*)?$', l):
                start = i
        elif l and not l.startswith((' ', '\t', '#')):
            return start, i
    return (start, len(lines)) if start is not None else None


def _set_key(lines, start, end, indent, key, value):
    """Set `key: value` at the given indent inside lines[start:end]; append
    to the block if absent. Returns the (possibly grown) end index."""
    pat = re.compile(rf'^({" " * indent}{re.escape(key)}:\s*)(\S.*?)?(\s+#.*)?$')
    for i in range(start, end):
        m = pat.match(lines[i])
        if m:
            lines[i] = f'{m.group(1)}{value}{m.group(3) or ""}'
            return end
    lines.insert(end, f'{" " * indent}{key}: {value}')
    return end + 1


def _ensure_subblock(lines, start, end, indent, key):
    pat = re.compile(rf'^{" " * indent}{re.escape(key)}:\s*(#.*)?$')
    for i in range(start, end):
        if pat.match(lines[i]):
            j = i + 1
            while j < end and (lines[j].startswith(' ' * (indent + 1)) or not lines[j].strip()
                               or lines[j].lstrip().startswith('#')):
                j += 1
            # trim trailing blank lines from the sub-block span
            while j > i + 1 and not lines[j - 1].strip():
                j -= 1
            return i + 1, j, end
    lines.insert(end, f'{" " * indent}{key}:')
    return end + 1, end + 1, end + 1


def record(site_yaml, deployed_tag=None, source=None, registry=None):
    """Update the images: block of the site yaml in place.

    deployed_tag: what the cluster now runs (images.tag)
    source:       dict with any of kind/registry/tag/core_image/token_env
    registry:     where the cluster pulls from (images.registry)
    """
    path = Path(site_yaml)
    lines = path.read_text().splitlines()
    span = _block_span(lines)
    if span is None:
        lines += ['', '# Images — what the cluster runs and where it came from '
                  '(maintained by the nico-dev scripts)', 'images:']
        span = (len(lines) - 1, len(lines))
    start, end = span
    if registry is not None:
        end = _set_key(lines, start + 1, end, 2, 'registry', registry)
    if deployed_tag is not None:
        end = _set_key(lines, start + 1, end, 2, 'tag', deployed_tag)
    if source:
        s_start, s_end, end = _ensure_subblock(lines, start + 1, end, 2, 'source')
        for k in ('kind', 'registry', 'tag', 'core_image', 'token_env'):
            if k in source and source[k] is not None:
                new_end = _set_key(lines, s_start, s_end, 4, k, source[k])
                end += new_end - s_end
                s_end = new_end
    # names: always present so "all images" is spelled out once
    if not any(re.match(r'^  names:\s*$', l) for l in lines[start:end]):
        lines[end:end] = ['  names:',
                          f'    core: {IMAGE_NAMES["core"]}',
                          f'    rest: [{", ".join(IMAGE_NAMES["rest"])}]',
                          f'    flow: [{", ".join(IMAGE_NAMES["flow"])}]']
    path.write_text('\n'.join(lines) + '\n')
