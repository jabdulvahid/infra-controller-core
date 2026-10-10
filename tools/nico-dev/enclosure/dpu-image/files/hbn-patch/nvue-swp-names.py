#!/usr/bin/env python3
"""Let NVUE classify the Enclosure's ports as swp.

NVUE (HBN 3.4.0, cue_netlink_v1/translated.py, _check_link_kind) treats a
link as an `swp` port only when the kernel reports its parent device as an
mlx5_core.sf subfunction. The Enclosure DPU's ports are virtio NICs and a
veth that carry the SF names (p0_if, pf0hpf_if, pf0dpu1_if); without this
they match no kind, _create_translated_interface returns {} and every
interface query is a 500 (first child boot, 2026-10-10). Accept the SF
names as well. Kernel-side alternative, if other HBN components turn out to
read parentdev: a DKMS module that re-parents the netdevs under platform
devices named mlx5_core.sf.N.
"""
import re
import sys

PATH = "/usr/lib/python3/dist-packages/nos/funits/cue_netlink_v1/translated.py"
OLD = """    elif kind == 'swp':
        if 'parentdev' in link and "mlx5_core.sf" in link['parentdev']:
            return True
"""
NEW = OLD + """        # enclosure: software DPU ports carry SF names on virtio/veth netdevs
        if re.match(r'^(p\\d+|pf\\d+hpf|pf\\d+vf\\d+|pf\\d+dpu\\d+)_if$', link['ifname']):
            return True
"""

source = open(PATH).read()
if NEW in source:
    print("nvue-swp-names: already applied")
    sys.exit(0)
if OLD not in source:
    sys.exit("nvue-swp-names: _check_link_kind swp branch not found; HBN version changed?")
open(PATH, "w").write(source.replace(OLD, NEW, 1))
# Drop the stale bytecode so the change cannot be shadowed.
import pathlib
for pyc in pathlib.Path(PATH).parent.glob("__pycache__/translated.*.pyc"):
    pyc.unlink()
print("nvue-swp-names: applied")
