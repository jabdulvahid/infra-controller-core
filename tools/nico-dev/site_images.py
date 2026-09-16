"""
nico-dev — the image model of a site, in one place.

Every NICo image the dev site runs lives in ONE registry under a FIXED set of
names, in three GROUPS that each map to one Helm release: `core` (the nico
umbrella), `rest` (the nico-rest umbrella + site-agent), `flow` (the Flow
add-on). There is one default tag and an optional per-group override, because
each release takes exactly one tag. This module spells the set out (instead of
each script carrying its own list) and reads/writes the `images:` section of
the site yaml, which is the source of truth for what the cluster runs and
where it came from:

  images:
    registry: 192.168.64.1:5000      # where the cluster pulls from
    tag: ngc-v2.2.0-…                # default DEPLOYED tag (written by the deploy scripts)
    tags:                            # per group; each defaults to tag
      core: ngc-v2.2.0-…
      rest: ngc-v2.2.0-…
      flow: ngc-v2.2.0-…
    source:
      kind: ngc | build              # how the images were produced
      registry: nvcr.io/<org>/<team> # NGC base — every image lives here (ngc)
      tag: v2.2.0-…                  # default NGC tag (ngc) / the built tag (build)
      tags:                          # per group NGC tags (bringup.yaml ngc.tags)
        core: v2.2.0-…
        rest: v2.2.0-…
        flow: v2.2.0-…
      core_image: nvmetal-carbide    # NGC's name for the core image (local: nico)
      token_env: NGC_API_KEY         # env var NAME of the NGC key (never the value)
    names:
      core: nico
      rest: [nico-rest-api, …]
      flow: [nico-flow]

Writes are line-oriented edits inside the `images:` block so the yaml's
comments survive (yaml.dump would strip them). Older site yamls without the
block get one appended. Readers fall back to the legacy `registry:` block.
"""

import copy
import json
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
GROUPS = ('core', 'rest', 'flow')

# The names NGC publishes under, per LOCAL name. The local names are what the
# Helm charts expect in the local registry and are fixed by the charts (IMAGE_NAMES);
# the NGC names are configuration (bringup.yaml ngc.images) and default to the same
# name — only the core differs (nvmetal-carbide on NGC, nico locally).
NGC_NAMES_DEFAULT = {
    'core': NGC_CORE_IMAGE_DEFAULT,
    'rest': {n: n for n in IMAGE_NAMES['rest']},
    'flow': {n: n for n in IMAGE_NAMES['flow']},
}


def parse_names_arg(value):
    """JSON string (CLI) or dict (yaml) → {'core': str, 'rest': {local: ngc}, 'flow': {...}}
    with only the given keys. Local names must be chart names; None/'' → {}."""
    if not value:
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as e:
            raise SystemExit(f'Error: images: not valid JSON ({e})')
    if not isinstance(value, dict):
        raise SystemExit('Error: images: expected a map with keys core, rest, flow')
    bad = sorted(set(value) - set(GROUPS))
    if bad:
        raise SystemExit(f'Error: images: unknown image group(s) {", ".join(bad)} '
                         f'— valid: {", ".join(GROUPS)}')
    out = {}
    if value.get('core') not in (None, ''):
        if not isinstance(value['core'], str):
            raise SystemExit('Error: images.core must be the NGC name of the core image (a string)')
        out['core'] = value['core']
    for g in ('rest', 'flow'):
        m = value.get(g)
        if not m:
            continue
        if not isinstance(m, dict):
            raise SystemExit(f'Error: images.{g} must be a map of local chart name → NGC name')
        unknown = sorted(set(m) - set(IMAGE_NAMES[g]))
        if unknown:
            raise SystemExit(f'Error: images.{g}: {", ".join(unknown)} is not a local image name; '
                             f'the local names are fixed by the Helm charts: {", ".join(IMAGE_NAMES[g])}')
        out[g] = {k: str(v) for k, v in m.items() if v not in (None, '')}
    return out


def resolve_ngc_names(overrides=None):
    """Full {'core': str, 'rest': {local: ngc}, 'flow': {local: ngc}}: defaults with
    the overrides applied."""
    names = copy.deepcopy(NGC_NAMES_DEFAULT)
    ov = parse_names_arg(overrides)
    if 'core' in ov:
        names['core'] = ov['core']
    for g in ('rest', 'flow'):
        names[g].update(ov.get(g, {}))
    return names


def names_arg(names):
    """{...} → compact JSON for passing between scripts."""
    return json.dumps(names, separators=(',', ':'), sort_keys=True)


def parse_tags_arg(value):
    """'core=T1,rest=T2' (CLI) or {'core': 'T1'} (yaml) → {group: tag}.
    Only the three groups are accepted; None/'' → {}."""
    if not value:
        return {}
    if isinstance(value, dict):
        out = {str(k): str(v) for k, v in value.items() if v not in (None, '')}
    else:
        out = {}
        for part in str(value).split(','):
            part = part.strip()
            if not part:
                continue
            if '=' not in part:
                raise SystemExit(f'Error: tags: expected GROUP=TAG, got {part!r}')
            k, v = part.split('=', 1)
            out[k.strip()] = v.strip()
    bad = sorted(set(out) - set(GROUPS))
    if bad:
        raise SystemExit(f'Error: tags: unknown image group(s) {", ".join(bad)} '
                         f'— valid: {", ".join(GROUPS)}')
    return out


def resolve_group_tags(default, overrides=None):
    """{group: tag} with every group filled: the override when given, else default."""
    ov = parse_tags_arg(overrides)
    return {g: ov.get(g) or default for g in GROUPS}


def tags_arg(tags):
    """{group: tag} → 'core=T1,rest=T2' for passing between scripts."""
    return ','.join(f'{g}={t}' for g, t in tags.items() if t)


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
    # per-group tags: an explicit value wins, an absent/empty/"none" one means "the default"
    def _fill(default, given):
        given = given or {}
        return {g: (str(given.get(g)) if given.get(g) not in (None, '', 'none') else default)
                for g in GROUPS}
    img['tags'] = _fill(img['tag'], img.get('tags'))
    img['source']['tags'] = _fill(img['source'].get('tag', ''), img['source'].get('tags'))
    # NGC names: the recorded map, else legacy core_image, else the defaults
    given = dict(img['source'].get('names') or {})
    if 'core' not in given and img['source'].get('core_image'):
        given['core'] = img['source']['core_image']
    img['source']['names'] = resolve_ngc_names(given)
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
    lines.insert(_insert_at(lines, start, end), f'{" " * indent}{key}: {value}')
    return end + 1


def _insert_at(lines, start, end):
    """Where to append inside lines[start:end]: before any trailing blank lines,
    so a new key lands inside the block rather than after the gap that
    separates it from the next top-level key."""
    at = end
    while at > start and not lines[at - 1].strip():
        at -= 1
    return at


def _set_tree(lines, start, end, indent, mapping):
    """Set nested `key: value` pairs at `indent` inside lines[start:end]; a dict
    value becomes a sub-block. Returns the new end index of lines[start:end]."""
    for k, v in mapping.items():
        if v is None:
            continue
        if isinstance(v, dict):
            c_start, c_end, new_end = _ensure_subblock(lines, start, end, indent, k)
            end = new_end
            new_c_end = _set_tree(lines, c_start, c_end, indent + 2, v)
            end += new_c_end - c_end
        else:
            end = _set_key(lines, start, end, indent, k, v)
    return end


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
    at = _insert_at(lines, start, end)
    lines.insert(at, f'{" " * indent}{key}:')
    return at + 1, at + 1, end + 1


def record(site_yaml, deployed_tag=None, source=None, registry=None, deployed_tags=None):
    """Update the images: block of the site yaml in place.

    deployed_tag:  the default deployed tag (images.tag)
    deployed_tags: {group: tag} now running (images.tags.<group>)
    source:        dict with any of kind/registry/tag/core_image/token_env, plus the
                   optional maps 'tags' {group: ngc tag} (images.source.tags) and
                   'names' {'core': str, 'rest': {local: ngc}, 'flow': {...}}
                   (images.source.names)
    registry:      where the cluster pulls from (images.registry)
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
    if deployed_tags:
        end = _set_tree(lines, start + 1, end, 2,
                        {'tags': {g: deployed_tags[g] for g in GROUPS if deployed_tags.get(g) is not None}})
    if source:
        scalars = {k: source[k] for k in ('kind', 'registry', 'tag', 'core_image', 'token_env')
                   if source.get(k) is not None}
        maps = {}
        if source.get('tags'):
            maps['tags'] = {g: source['tags'][g] for g in GROUPS if source['tags'].get(g) is not None}
        if source.get('names'):
            maps['names'] = source['names']
        end = _set_tree(lines, start + 1, end, 2, {'source': {**scalars, **maps}})
    # names: always present so "all images" is spelled out once
    if not any(re.match(r'^  names:\s*$', l) for l in lines[start:end]):
        lines[end:end] = ['  names:',
                          f'    core: {IMAGE_NAMES["core"]}',
                          f'    rest: [{", ".join(IMAGE_NAMES["rest"])}]',
                          f'    flow: [{", ".join(IMAGE_NAMES["flow"])}]']
    path.write_text('\n'.join(lines) + '\n')
