#!/usr/bin/env python3
"""
nico-dev — List deployable NGC image tags (pick one for ngc.nico_tag).

  ngc-tags.py                              # env defaults (see below)
  ngc-tags.py --config devup-mysite.yaml   # read image/token from a config
  ngc-tags.py -n 20                        # more PR builds

Defaults: image from $NICO_NGC_IMAGE, key from $NGC_API_KEY (names only —
values are never printed). Shows the newest PR builds (what tracks main)
with host-arch availability, and the newest release tags.
"""

import argparse
import base64
import json
import os
import platform
import re
import sys
import urllib.request
from pathlib import Path

NEED_ARCH = 'arm64' if platform.machine() == 'arm64' else 'amd64'
PR_RE = re.compile(r'^v?(\d+)\.(\d+)\.(\d+)-pr-(\d+)-g[0-9a-f]+$')
REL_RE = re.compile(r'^v(\d+)\.(\d+)\.(\d+)$')


def get_token(host, path, key):
    basic = base64.b64encode(f'$oauthtoken:{key}'.encode()).decode()
    req = urllib.request.Request(
        f'https://{host}/proxy_auth?scope=repository:{path}:pull',
        headers={'Authorization': f'Basic {basic}'})
    token = json.load(urllib.request.urlopen(req)).get('token', '')
    if not token:
        raise SystemExit('Error: could not get a registry token — check the '
                         'key (needs registry-read on the image org/team).')
    return token


def fetch_tags(host, path, token):
    req = urllib.request.Request(
        f'https://{host}/v2/{path}/tags/list',
        headers={'Authorization': f'Bearer {token}'})
    return json.load(urllib.request.urlopen(req)).get('tags', [])


MANIFEST_ACCEPT = ', '.join([
    'application/vnd.oci.image.index.v1+json',
    'application/vnd.docker.distribution.manifest.list.v2+json',
    'application/vnd.oci.image.manifest.v1+json',
    'application/vnd.docker.distribution.manifest.v2+json',
])


def _get(host, path, kind, ref, token):
    req = urllib.request.Request(
        f'https://{host}/v2/{path}/{kind}/{ref}',
        headers={'Authorization': f'Bearer {token}',
                 'Accept': MANIFEST_ACCEPT})
    return json.load(urllib.request.urlopen(req))


def tag_info(host, path, tag, token):
    """(architectures, build date) for a tag, via manifest (index or
    single) → image config blob's `created` timestamp."""
    try:
        doc = _get(host, path, 'manifests', tag, token)
        archs, digest = [], None
        if 'manifests' in doc:                       # multi-arch index
            plats = {m.get('platform', {}).get('architecture', '?'):
                     m.get('digest') for m in doc['manifests']}
            plats.pop('unknown', None)               # attestation entries
            archs = sorted(plats)
            digest = plats.get(NEED_ARCH) or next(iter(plats.values()), None)
            if digest:
                doc = _get(host, path, 'manifests', digest, token)
        cfg_digest = doc.get('config', {}).get('digest')
        created = ''
        if cfg_digest:
            cfg = _get(host, path, 'blobs', cfg_digest, token)
            if not archs:
                archs = [cfg.get('architecture', '?')]
            created = (cfg.get('created') or '')[:10]
        return archs, created
    except Exception:
        return [], ''


def main():
    p = argparse.ArgumentParser(
        description='List deployable NGC tags for the nico image',
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument('--ngc-image', default=os.environ.get('NICO_NGC_IMAGE', ''),
                   metavar='REPO',
                   help='nvcr.io/<org>/<team>/<image> (default: $NICO_NGC_IMAGE)')
    p.add_argument('--token-env', default='NGC_API_KEY', metavar='VAR',
                   help='env var NAME holding the NGC key (default: NGC_API_KEY)')
    p.add_argument('--config', default=None, metavar='FILE',
                   help='devup yaml — reads ngc.nico_image / ngc.token_env')
    p.add_argument('-n', type=int, default=10,
                   help='how many PR builds to show (default 10)')
    p.add_argument('--before', metavar='TAG', default=None,
                   help='page back: show the window ending just BEFORE this '
                        'tag (exact or unique substring, e.g. pr-441)')
    args = p.parse_args()

    if args.config:
        import yaml
        cfg = yaml.safe_load(Path(args.config).expanduser().read_text()) or {}
        ngc = cfg.get('ngc') or {}
        args.ngc_image = ngc.get('nico_image', args.ngc_image)
        args.token_env = ngc.get('token_env', args.token_env)

    if not args.ngc_image:
        raise SystemExit('Error: no image. Pass --ngc-image, --config, or '
                         'export NICO_NGC_IMAGE.')
    key = os.environ.get(args.token_env, '')
    if not key:
        raise SystemExit(f'Error: env var {args.token_env} is empty or unset '
                         f'(the NGC API key).')

    host, path = args.ngc_image.split('/', 1)
    token = get_token(host, path, key)
    tags = set(fetch_tags(host, path, token))

    prs = sorted(
        (t for t in tags if PR_RE.match(t)),
        key=lambda t: tuple(int(x) for x in PR_RE.match(t).groups()))
    rels = sorted(
        (t for t in tags if REL_RE.match(t)),
        key=lambda t: tuple(int(x) for x in REL_RE.match(t).groups()))

    window = prs
    if args.before:
        hits = [i for i, t in enumerate(prs) if args.before in t]
        if not hits:
            raise SystemExit(f'Error: no PR build matches "{args.before}".')
        # Multiple matches: the window ends before the OLDEST match — so
        # `--before v2.3.0` means "before the v2.3.0 series began".
        window = prs[:min(hits)]
        if not window:
            raise SystemExit(f'Nothing older than {prs[min(hits)]}.')

    print(f'{args.ngc_image}\n')
    where = (f'PR builds before {prs[min(hits)]}' if args.before
             else 'Latest PR builds')
    print(f'{where} ({min(args.n, len(window))} of {len(prs)}, newest last; '
          f'{NEED_ARCH} required on this host; --before <tag> pages back):')
    for t in window[-args.n:]:
        a, created = tag_info(host, path, t, token)
        mark = (f'✓ {NEED_ARCH}' if NEED_ARCH in a
                else f'✗ {"/".join(a)} only' if a else '? manifest unreadable')
        print(f'  {t:42s} {created:10s}  {mark}')
    if rels:
        print(f'\nLatest release: {rels[-1]}'
              + (f'   (previous: {", ".join(rels[-4:-1][::-1])})'
                 if len(rels) > 1 else ''))
    print('\nPick a tag → ngc.nico_tag in your devup yaml. Note: nico does '
          'not\nsupport schema downgrades — on an EXISTING site, never '
          'deploy a tag\nolder than what is running.')


if __name__ == '__main__':
    main()
