"""DPU stand-in context — ContainerLab container clab-{dc_name}-dpu-1.

In nico-dev the DPU is not a virsh VM — it is a ContainerLab FRR container.
We inspect it directly via docker exec instead of SSH.
"""

import re
import subprocess
import sys


def _docker_exec(container, cmd, timeout=8):
    r = subprocess.run(
        ['docker', 'exec', container] + cmd,
        capture_output=True, text=True, timeout=timeout,
    )
    return r.stdout, r.returncode


def _is_running(container):
    r = subprocess.run(
        ['docker', 'inspect', '--format', '{{.State.Running}}', container],
        capture_output=True, text=True,
    )
    return r.returncode == 0 and r.stdout.strip() == 'true'


def _bgp_summary(container):
    """Return (established, total) BGP peers from vtysh."""
    out, rc = _docker_exec(container, ['vtysh', '-c', 'show bgp summary'], timeout=6)
    if rc != 0:
        return 0, 0
    total = 0
    established = 0
    for line in out.splitlines():
        m = re.match(
            r'\S+\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+\S+\s+(\S+)',
            line.strip(),
        )
        if m:
            total += 1
            if m.group(1).isdigit():
                established += 1
    return established, total


def _ip_forward(container):
    """Return ip_forward value (0 or 1, or None on error)."""
    out, rc = _docker_exec(container, ['cat', '/proc/sys/net/ipv4/ip_forward'], timeout=4)
    if rc != 0:
        return None
    val = out.strip()
    try:
        return int(val)
    except ValueError:
        return None


def collect(site):
    """Collect DPU stand-in container status."""
    dc_name   = site['dc_name']
    clab_name = site['clab_name']
    ips       = site.get('_ips', {})

    container_name = f'clab-{clab_name}-dpu-1'

    # Off-host: the clab containers live in the VM's docker, not the host's
    # — "not found" here means "can't see", not "stopped" (20260826-#5), and
    # a same-named leftover on a Linux host would be the WRONG container
    # (20260902-#8). The site collector decides which side we are on.
    if not site.get('_on_vm', True):
        return {
            'name': container_name, 'running': None,
            'fabric_ip': ips.get('dpu_lcp_ip', ''),
            'cp_link_ip': ips.get('dpu_cp_ip', ''),
            'bgp_estab': None, 'bgp_total': None, 'ip_forward': None,
        }

    running = _is_running(container_name)

    bgp_estab  = 0
    bgp_total  = 0
    ip_fwd     = None

    if running:
        bgp_estab, bgp_total = _bgp_summary(container_name)
        ip_fwd = _ip_forward(container_name)

    # IPs come from fabric-ips.json if present
    fabric_ip  = ips.get('dpu_lcp_ip', '')   # DPU fabric-side IP (toward leaf-cp)
    cp_link_ip = ips.get('dpu_cp_ip', '')    # DPU CP-link IP (toward CP host)

    return {
        'name':        container_name,
        'running':     running,
        'fabric_ip':   fabric_ip,
        'cp_link_ip':  cp_link_ip,
        'bgp_estab':   bgp_estab,
        'bgp_total':   bgp_total,
        'ip_forward':  ip_fwd,
    }
