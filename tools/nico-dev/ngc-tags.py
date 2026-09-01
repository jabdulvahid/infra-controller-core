#!/usr/bin/env python3
"""
nico-dev — List deployable NGC image tags (pick one for ngc.nico_tag).

  ngc-tags.py                              # env defaults (see below)
  ngc-tags.py --config devup-mysite.yaml   # read image/token from a config
  ngc-tags.py -n 20                        # more PR builds

Defaults: image from $NICO_NGC_IMAGE, key from $NGC_API_KEY (names only —
values are never printed). Shows the newest PR builds (what tracks main)
with arm64 availability, and the newest release tags.
"""

import argparse
import base64
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

PR_RE = re.compile(r'^v?(\d+)\.(\d+)\.(\d+)-pr-(\d+)-g[0-9a-f]+$')
REL_RE = re.compile(r'^v(\d+)\.(\d+)\.(\d+)$')


def fetch_tags(image, key):
    host, path = image.split('/', 1)
    basic = base64.b64encode(f'$oauthtoken:{key}'.encode()).decode()
    req = urllib.request.Request(
        f'https://{host}/proxy_auth?scope=repository:{path}:pull',
        headers={'Authorization': f'Basic {basic}'})
    token = json.load(urllib.request.urlopen(req)).get('token', '')
    if not token:
        raise SystemExit('Error: could not get a registry token — check the '
                         'key (needs registry-read on the image org/team).')
    req = urllib.request.Request(
        f'https://{host}/v2/{path}/tags/list',
        headers={'Authorization': f'Bearer {token}'})
    return json.load(urllib.request.urlopen(req)).get('tags', [])


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

    tags = set(fetch_tags(args.ngc_image, key))

    prs = sorted(
        (t for t in tags if PR_RE.match(t)),
        key=lambda t: tuple(int(x) for x in PR_RE.match(t).groups()))
    rels = sorted(
        (t for t in tags if REL_RE.match(t)),
        key=lambda t: tuple(int(x) for x in REL_RE.match(t).groups()))

    print(f'{args.ngc_image}\n')
    print(f'Latest {min(args.n, len(prs))} PR builds (newest last — these '
          f'track main):')
    for t in prs[-args.n:]:
        arm = '✓ arm64' if (f'{t}-arm64' in tags or t.endswith('-arm64')) \
              else '? arm64 unverified (docker manifest inspect to confirm)'
        print(f'  {t:42s} {arm}')
    if rels:
        print(f'\nLatest release: {rels[-1]}'
              + (f'   (previous: {", ".join(rels[-4:-1][::-1])})'
                 if len(rels) > 1 else ''))
    print('\nPick a tag → ngc.nico_tag in your devup yaml. Note: nico does '
          'not\nsupport schema downgrades — on an EXISTING site, never '
          'deploy a tag\nolder than what is running.')


if __name__ == '__main__':
    main()
