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
    source:        dict with any of kind/registry/tag/core_image/token_env, plus
                   an optional 'tags' {group: ngc tag} (images.source.tags)
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
        t_start, t_end, end = _ensure_subblock(lines, start + 1, end, 2, 'tags')
        for g in GROUPS:
            if deployed_tags.get(g) is not None:
                new_end = _set_key(lines, t_start, t_end, 4, g, deployed_tags[g])
                end += new_end - t_end
                t_end = new_end
    if source:
        s_start, s_end, end = _ensure_subblock(lines, start + 1, end, 2, 'source')
        for k in ('kind', 'registry', 'tag', 'core_image', 'token_env'):
            if k in source and source[k] is not None:
                new_end = _set_key(lines, s_start, s_end, 4, k, source[k])
                end += new_end - s_end
                s_end = new_end
        if source.get('tags'):
            t_start, t_end, new_s_end = _ensure_subblock(lines, s_start, s_end, 4, 'tags')
            end += new_s_end - s_end
            s_end = new_s_end
            for g in GROUPS:
                if source['tags'].get(g) is not None:
                    new_end = _set_key(lines, t_start, t_end, 6, g, source['tags'][g])
                    end += new_end - t_end
                    s_end += new_end - t_end
                    t_end = new_end
    # names: always present so "all images" is spelled out once
    if not any(re.match(r'^  names:\s*$', l) for l in lines[start:end]):
        lines[end:end] = ['  names:',
                          f'    core: {IMAGE_NAMES["core"]}',
                          f'    rest: [{", ".join(IMAGE_NAMES["rest"])}]',
                          f'    flow: [{", ".join(IMAGE_NAMES["flow"])}]']
    path.write_text('\n'.join(lines) + '\n')
