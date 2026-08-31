"""
nico-sim — shared SSH key resolution.

Every nico-sim script that SSHes into VMs uses BatchMode (no prompts), so a
passphrase-protected private key fails every probe silently — historically
surfacing as bogus downstream errors ("ip_forward not 1", "cloud-init timed
out", unreachable registry). This module picks the first key that actually
WORKS non-interactively, not merely the first that exists.

Discovery order (SUDO_USER's home first when running under sudo, then ~):
  id_nico_sim, id_ed25519, id_rsa, id_ecdsa

id_nico_sim is the dedicated-key name our tooling recommends creating when a
personal key is passphrase-protected:
  ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_nico_sim
"""

import os
import subprocess
from pathlib import Path

KEY_NAMES = ['id_nico_sim', 'id_ed25519', 'id_rsa', 'id_ecdsa']


def key_is_usable(priv_path):
    """True if the private key can be used non-interactively (no passphrase)."""
    r = subprocess.run(['ssh-keygen', '-y', '-P', '', '-f', str(priv_path)],
                       capture_output=True, text=True)
    return r.returncode == 0 and bool(r.stdout.strip())


def _candidates():
    homes = []
    sudo_user = os.environ.get('SUDO_USER')
    if sudo_user:
        homes.append(Path(f'/home/{sudo_user}'))
    homes.append(Path('~').expanduser())
    seen, out = set(), []
    for home in homes:
        for name in KEY_NAMES:
            p = home / '.ssh' / name
            if p.exists() and p not in seen:
                seen.add(p)
                out.append(p)
    return out


def find_priv_key(required=True):
    """Return the path (str) of the first usable private key.

    Skips passphrase-protected keys. If nothing usable is found:
    raises RuntimeError with remediation when required=True, else returns None
    (for collectors/status tools that degrade gracefully).
    """
    candidates = _candidates()
    unusable = []
    for priv in candidates:
        if key_is_usable(priv):
            return str(priv)
        unusable.append(str(priv))

    if not required:
        return None

    detail = ''.join(f'  {p}  (passphrase-protected or unreadable)\n' for p in unusable) \
             or '  (no keys found in ~/.ssh)\n'
    raise RuntimeError(
        'No usable SSH private key — BatchMode SSH cannot prompt for passphrases:\n'
        + detail +
        'Create a dedicated unencrypted key (used automatically once it exists):\n'
        '  ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_nico_sim')
