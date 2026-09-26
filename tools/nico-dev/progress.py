"""
nico-dev — bring-up progress events (writer side).

The bring-up runner and the scripts it drives append one JSON line per event
to a progress file, and `bring-up-status.py` renders that file in a second
terminal. The runner's own output stream is untouched.

  file      <share>/.bring-up/<vm-name>.jsonl — the share exists from the
            first step and the VM sees it too (/home/<user>/mac/.bring-up/…)
  who       bring-up_linux.py owns the file and writes the `plan`, one
            `start` and one `done`/`fail` per step, and `finished`.
            build-dev-nico.py and deploy-dev-nico.py add `stage` lines for
            what they are doing inside their step.
  how       the runner exports NICO_DEV_PROGRESS=<file> to the scripts it
            runs on the host, and passes it through `sudo env` for the one it
            runs in the VM. Without the variable emit() is a no-op, so every
            script behaves as before when run by hand.

One event per line, always `ts` (epoch seconds) and `kind`; the rest depends
on the kind:

  plan      steps [{key, where, desc}], name, ip, user, dc, site, site_dir,
            kubeconfig, admin_url, mode, resume_hint
  start     step, i, n
  stage     step, detail            (free text: "image 1/3 carbide-build")
  done      step, secs
  fail      step, secs, rc, resume
  finished  secs
"""

import json
import os
import time
from pathlib import Path

ENV = 'NICO_DEV_PROGRESS'


def path_for(share, name):
    """The progress file for VM `name` under `share` (host-side path)."""
    return Path(share).expanduser() / '.bring-up' / f'{name}.jsonl'


def vm_path_for(vm_share, name):
    """The same file as the VM sees it (vm_share = /home/<user>/mac)."""
    return f'{vm_share}/.bring-up/{name}.jsonl'


def emit(kind, **fields):
    """Append one event to $NICO_DEV_PROGRESS; silently do nothing without it.
    Never raises: progress must not be able to break a bring-up."""
    path = os.environ.get(ENV)
    if not path:
        return
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, 'a') as f:
            f.write(json.dumps({'ts': time.time(), 'kind': kind, **fields}) + '\n')
    except OSError:
        pass


def start_file(path, fresh):
    """Own the file for a run: truncate on a fresh run (from the first step),
    keep it on a resume so completed steps stay visible."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if fresh:
        p.write_text('')
    os.environ[ENV] = str(p)
