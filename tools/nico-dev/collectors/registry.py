"""
Registry collector — lists images/tags and (on VM) verifies cluster reachability.
"""

import json
import re
import socket
import subprocess
import urllib.request
import urllib.error
from pathlib import Path


def _is_on_vm():
    """True when running inside the nico-dev VM (not on Mac)."""
    return Path('/etc/kubernetes/admin.conf').exists()


def _http_get(url, timeout=5):
    """GET url, return (body_str, status_code) or (None, error_str)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read().decode(), r.status
    except urllib.error.HTTPError as e:
        return None, f'HTTP {e.code}'
    except Exception as e:
        return None, str(e)


def _list_catalog(registry):
    body, status = _http_get(f'http://{registry}/v2/_catalog')
    if body is None:
        return None, status
    try:
        return json.loads(body).get('repositories', []), None
    except Exception:
        return None, 'invalid JSON'


def _list_tags(registry, repo):
    body, status = _http_get(f'http://{registry}/v2/{repo}/tags/list')
    if body is None:
        return [], status
    try:
        return json.loads(body).get('tags') or [], None
    except Exception:
        return [], 'invalid JSON'


def _containerd_configured(registry):
    """Check containerd insecure registry trust for this host:port."""
    # 1. hosts.toml must exist and have the HTTP entry
    hosts_toml = Path(f'/etc/containerd/certs.d/{registry}/hosts.toml')
    if not hosts_toml.exists():
        return False, f'{hosts_toml} not found'
    content = hosts_toml.read_text()
    if f'http://{registry}' not in content:
        return False, 'hosts.toml exists but HTTP entry missing'

    # 2. config_path must be set to a non-empty value in config.toml or a drop-in.
    #    containerd config default writes config_path = '' (disabled).
    #    Check both config.toml and config.d/*.toml drop-ins.
    config_files = list(Path('/etc/containerd/config.d').glob('*.toml')) \
                   if Path('/etc/containerd/config.d').exists() else []
    config_files.append(Path('/etc/containerd/config.toml'))
    combined = '\n'.join(f.read_text() for f in config_files if f.exists())
    # Use \b word boundary so 'plugin_config_path' does not match
    matches = re.findall(r'(?<!\w)config_path\s*=\s*["\']([^"\']*)["\']', combined)
    active = next((v for v in reversed(matches) if v.strip()), None)
    if not active:
        return False, "config_path is empty or unset — containerd will ignore hosts.toml"

    return True, None


def collect(site_data):
    registry = site_data.get('registry', '192.168.64.1:5000')

    on_vm = _is_on_vm()

    # ── Catalog + tags ────────────────────────────────────────────────────────
    repos, err = _list_catalog(registry)
    images = []
    reachable = repos is not None
    if repos:
        for repo in sorted(repos):
            tags, _ = _list_tags(registry, repo)
            images.append({'repo': repo, 'tags': sorted(tags or [])})

    # ── VM-only checks ────────────────────────────────────────────────────────
    containerd_ok   = None
    containerd_err  = None
    if on_vm:
        containerd_ok, containerd_err = _containerd_configured(registry)

    return {
        'registry':       registry,
        'on_vm':          on_vm,
        'reachable':      reachable,
        'reach_error':    err,
        'images':         images,
        'containerd_ok':  containerd_ok,
        'containerd_err': containerd_err,
    }
