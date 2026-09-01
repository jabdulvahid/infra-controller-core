#!/usr/bin/env python3
"""
nico-dev — Helm values generator for Nico system deployment

Reads nico-dev.yaml and generates Helm values YAML files for every chart
that deploy-dev-nico.py needs to install.  All Nico customisation comes
from nico-dev.yaml — nothing is hardcoded here.

Based on nico-sim/generate-sim-values.py. The nico-api siteConfig TOML is
intentionally identical to nico-sim — nico-dev is a fabric/cluster downgrade,
not a Nico API feature downgrade.

Differences from generate-sim-values.py:
  - lo-ip pool: skips 1 IP (single dev DPU) instead of num_vms
  - collect_deny_prefixes: no OOB/mgmt sections (nico-dev has none)
  - gen_nico image: reads registry.host/port/nico_tag (not nico-system.image)
  - gen_nico kea: adds ARM64 hook library path
  - gen_nico_prereqs: adds synchronousMode for single-instance postgres
  - gen_zalando_postgres_op: overrides image to ghcr.io (ARM64-compatible)
  - No sim_validate dependency

Sources:
  fabric.*                  — IP prefixes, ASNs, VNIs (fabric topology)
  registry.*                — Mac Docker registry (host, port, nico_tag)
  nico-system.*             — dev infrastructure (vault mode, postgres config)
  nico-system.helm-values.* — Helm chart values and siteConfig TOML content

Output (default: {site_folder}/dev-values/):
  cert-manager.yaml         — jetstack/cert-manager
  vault.yaml                — hashicorp/vault
  eso.yaml                  — external-secrets/external-secrets
  zalando-postgres-op.yaml  — sais/postgres-operator
  nico-prereqs.yaml         — infra-controller-core/helm-prereqs
  nico.yaml                 — infra-controller-core/helm

Usage:
  python3 generate-dev-values.py <site> [--output-dir ./dev-values]
"""

import argparse
import platform
import ipaddress
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print('Error: pyyaml not installed. Run: sudo apt install python3-yaml')
    sys.exit(1)


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description='Generate Helm values files for nico-dev deployment',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument('site', help='Site folder or yaml file')
    p.add_argument('--output-dir', default=None,
                   help='Override output directory (default: {site_folder}/dev-values/)')
    return p.parse_args()


def resolve_site(arg):
    p = Path(arg).expanduser()
    if p.is_dir():
        yamls = [f for f in p.glob('*.yaml')
                 if not f.stem.endswith('-mac') and '.kubeconfig' not in f.name]
        if not yamls:
            print(f'Error: no site yaml in {p}', file=sys.stderr); sys.exit(1)
        if len(yamls) > 1:
            print(f'Error: multiple yamls in {p}', file=sys.stderr); sys.exit(1)
        return str(yamls[0])
    return str(p)


def load_cfg(path):
    with open(path) as f:
        return yaml.safe_load(f)


# ── IP helpers ────────────────────────────────────────────────────────────────

def prefix_to_range(prefix_str):
    net   = ipaddress.IPv4Network(prefix_str, strict=False)
    hosts = list(net.hosts())
    return str(hosts[0]), str(hosts[-1])


def collect_deny_prefixes(cfg):
    """Collect prefixes that Nico should deny for tenant traffic.

    nico-dev has no OOB/mgmt networks (no libvirt, no ContainerLab mgmt net).
    We deny the named nico-system networks (admin, underlay) — same as the
    last step of nico-sim's collect_deny_prefixes.
    """
    deny = []
    hv = cfg.get('nico-system', {}).get('helm-values', {})
    for net_cfg in hv.get('networks', {}).values():
        if net_cfg and net_cfg.get('prefix'):
            deny.append(net_cfg['prefix'])
    return deny


# ── cert-manager values ───────────────────────────────────────────────────────

def gen_cert_manager(cfg):
    return {
        'installCRDs': True,
        'replicaCount': 1,
    }


# ── Vault values ──────────────────────────────────────────────────────────────

def gen_vault(cfg):
    ns   = cfg['nico-system']
    mode = ns['vault'].get('mode', 'file')

    if mode == 'dev':
        token = ns['vault'].get('dev_root_token', 'root')
        return {
            'server': {
                'dev': {'enabled': True, 'devRootToken': token},
                'standalone': {'enabled': False},
                'dataStorage': {'enabled': False},
            },
            'injector': {'enabled': False},
            'ui':       {'enabled': False},
        }

    if mode == 'file':
        hcl = (
            'ui = true\n'
            'listener "tcp" {\n'
            '  address = "[::]:8200"\n'
            '  cluster_address = "[::]:8201"\n'
            '  tls_disable = true\n'
            '}\n'
            'storage "file" {\n'
            '  path = "/vault/data"\n'
            '}\n'
        )
        # Sidecar that unseals Vault whenever it finds the pod sealed.
        # The vault-init-keys secret is created by deploy-dev-nico.py on first init
        # and persists in etcd across reboots.
        unseal_script = (
            'while true; do\n'
            '  if [ -f /vault/init-keys/unseal_key ]; then\n'
            '    vault status 2>/dev/null | grep -q "^Sealed.*true" && \\\n'
            '      vault operator unseal "$(cat /vault/init-keys/unseal_key)"\n'
            '  fi\n'
            '  sleep 10\n'
            'done\n'
        )
        return {
            'server': {
                'dev': {'enabled': False},
                'standalone': {'enabled': True, 'config': hcl},
                'dataStorage': {'enabled': False},
                'statefulSet': {
                    'securityContext': {
                        'pod': {'fsGroup': 1000},
                    },
                },
                'volumes': [
                    {
                        'name': 'vault-data',
                        'hostPath': {
                            'path': '/var/lib/vault',
                            'type': 'DirectoryOrCreate',
                        },
                    },
                    {
                        'name': 'vault-init-keys',
                        'secret': {
                            'secretName': 'vault-init-keys',
                            'optional': True,
                        },
                    },
                ],
                'volumeMounts': [
                    {'name': 'vault-data',      'mountPath': '/vault/data'},
                    {'name': 'vault-init-keys', 'mountPath': '/vault/init-keys', 'readOnly': True},
                ],
                # kubelet creates the hostPath dir root:root 0755 and k8s does
                # NOT apply fsGroup to hostPath volumes — vault (uid 100) then
                # fails with 'mkdir /vault/data/core: permission denied'.
                # Chown it as root before vault starts (same image as the
                # unsealer sidecar — no extra pull).
                'extraInitContainers': [
                    {
                        'name':    'fix-data-perms',
                        'image':   'hashicorp/vault:1.17',
                        'command': ['/bin/sh', '-c',
                                    'chown -R vault:vault /vault/data'],
                        'securityContext': {'runAsUser': 0},
                        'volumeMounts': [
                            {'name': 'vault-data', 'mountPath': '/vault/data'},
                        ],
                    },
                ],
                'extraContainers': [
                    {
                        'name':    'vault-unsealer',
                        'image':   'hashicorp/vault:1.17',
                        'command': ['/bin/sh', '-c', unseal_script],
                        'env':     [{'name': 'VAULT_ADDR', 'value': 'http://127.0.0.1:8200'}],
                        'volumeMounts': [
                            {'name': 'vault-init-keys', 'mountPath': '/vault/init-keys', 'readOnly': True},
                        ],
                    },
                ],
            },
            'injector': {'enabled': False},
            'ui':       {'enabled': False},
        }

    raise ValueError(f'Unsupported vault.mode: {mode!r}. Use "file" (recommended) or "dev" (legacy).')


# ── ESO values ────────────────────────────────────────────────────────────────

def gen_eso(cfg):
    return {
        'installCRDs': True,
        'replicaCount': 1,
    }


# ── Zalando postgres-operator values ─────────────────────────────────────────

def gen_zalando_postgres_op(cfg):
    # Override image to ghcr.io — registry.opensource.zalan.do only has x86_64.
    return {
        'configKubernetes': {
            'enable_pod_disruption_budget': False,
        },
        'image': {
            'registry':   'ghcr.io',
            'repository': 'zalando/postgres-operator',
        },
    }


# ── nico-prereqs values ───────────────────────────────────────────────────────

def gen_nico_prereqs(cfg):
    ns  = cfg['nico-system']
    hv  = ns['helm-values']
    pg  = ns['postgresql']
    np  = hv.get('net-plan', {})

    return {
        'siteName':             hv['sitename'],
        'namespace':            'nico-system',
        'certManagerNamespace': 'cert-manager',
        'vaultNamespace':       'vault',
        'esoNamespace':         'external-secrets',
        'postgresNamespace':    'postgres',
        'clusterApiServer':     '',

        'vault': {
            'address':   'http://vault.vault.svc.cluster.local:8200',
            'token':     '',   # injected by deploy-dev-nico.py via --set
            'tokenSecret':   {'create': True},
            'approleSecret': {'create': True},
            'configJob': {
                'enabled':      True,
                'vaultImage':   'hashicorp/vault:1.17',
                'kubectlImage': 'bitnami/kubectl:latest',
            },
            'tls':      {'create': True},
            'pkiMount': 'nicoca',
            'pkiRole':  'nico-cluster',
            'nicoCliClientRole': {
                'enabled':      True,
                'name':         'nico-cli-client',
                'ou':           'Invalid',
                'organization': '',
            },
            'nicoApiK8sAuth': {
                'enabled':            True,
                'serviceAccountName': 'nico-api',
                'tokenTTL':           '1h',
            },
            'kvMount': 'secrets',
            'kvSeeds': [
                {
                    'path': 'machines/all_dpus/factory_default/bmc-metadata-items/root',
                    'data': {'UsernamePassword': {'username': 'root', 'password': '0penBmc'}},
                },
                {
                    'path': 'machines/all_dpus/factory_default/uefi-metadata-items/auth',
                    'data': {'UsernamePassword': {'username': '', 'password': 'bluefield'}},
                },
                {
                    'path': 'machines/all_dpus/site_default/uefi-metadata-items/auth',
                    'data': {'UsernamePassword': {'username': '', 'password': ''}},
                },
                {
                    'path': 'machines/all_hosts/site_default/uefi-metadata-items/auth',
                    'data': {'UsernamePassword': {'username': '', 'password': ''}},
                },
            ],
        },

        'certManager': {
            'siteRoot':                {'create': True},
            'vaultAuthServiceAccount': {'create': True},
            'vaultIssuer':             {'create': True},
        },

        'externalSecrets': {'enabled': True},

        # Pre-created by deploy-dev-nico.py in OpenSSH format (helm genPrivateKey
        # produces PKCS#8, which ssh-console-rs rejects).
        'sshHostKey': {'create': False},

        'azureSSOClientSecret':       '',
        'azureSSOClientSecretSecret': {'create': True},

        'imagePullSecrets': {
            'ngcCarbidePull': '',
            'ngcNvidianKey':  '',
        },

        # REST is a base feature in nico-dev (all four dev surfaces): enables the
        # nico_rest DB on nico-pg-cluster + the ESO credential sync.
        'rest': {'enabled': cfg.get('nico-system', {}).get('rest', {}).get('enabled', True),
                 'namespace': 'nico-rest'},
        'flow': {'enabled': False, 'namespace': 'flow'},

        'postgresql': {
            'enabled':         True,
            'instances':       pg['instances'],
            'synchronousMode': pg.get('synchronous_mode', True),
            'volumeSize':      pg['volume_size'],
            'storageClass':    pg['storage_class'],
            'host':            'nico-pg-cluster.postgres.svc.cluster.local',
            'resources': {
                'limits':   {'cpu': '2',    'memory': '2Gi'},
                'requests': {'cpu': '250m', 'memory': '512Mi'},
            },
        },
    }


# ── siteConfig TOML ───────────────────────────────────────────────────────────

def gen_site_config_toml(cfg):
    """
    Generate the nico-api siteConfig TOML string.

    Intentionally identical to nico-sim's gen_site_config_toml except for
    the lo-ip pool derivation (1 dev DPU instead of num_vms CP DPUs).
    """
    fab   = cfg['fabric']
    pfx   = fab['prefixes']
    hv    = cfg['nico-system']['helm-values']
    pools = hv.get('pools', {})

    # lo-ip pool: dpu_loopbacks prefix, skip first IP (used by single dev DPU).
    lo_net   = ipaddress.IPv4Network(pfx['dpu_loopbacks'], strict=False)
    lo_hosts = list(lo_net.hosts())
    lo_start = str(lo_hosts[1])   # skip .1 (dev DPU loopback)
    lo_end   = str(lo_hosts[-1])

    # VIPs from net-plan
    np       = hv.get('net-plan', {})
    dhcp_vip = np.get('dhcp_vip', '')
    api_vip  = np.get('api_vip', '')

    # site_fabric_prefixes from overlay prefix
    site_fabric = [pfx['overlay']]

    # deny_prefixes
    deny = collect_deny_prefixes(cfg)

    # datacenter_asn
    datacenter_asn = fab.get('switch_asn_base', 4266050001)

    # FNN ASN pool
    fnn_asn_pool  = pools.get('fnn_asn', {})
    fnn_asn_start = fnn_asn_pool.get('start', '')
    fnn_asn_end   = fnn_asn_pool.get('end', '')
    if not fnn_asn_start or not fnn_asn_end:
        raise ValueError('nico-system.helm-values.pools.fnn_asn is required in nico-dev.yaml')

    # VNI pool from evpn config
    evpn   = fab.get('evpn', {})
    cp_vni = evpn.get('control_plane_vni', 60000)
    mh_vni = evpn.get('managed_host_vni',  60100)

    # fnn config
    fnn_cfg       = hv.get('fnn', {})
    fnn_enabled   = fnn_cfg.get('enabled', True)
    admin_vpc_vni = fnn_cfg.get('admin_vpc_vni', 61000)

    # route servers
    enable_rs = hv.get('enable_route_servers', False)

    # user-provided values
    sitename    = hv.get('sitename',    'dev')
    domain      = hv.get('domain',      f'{sitename}.example.com')
    bypass_rbac = hv.get('bypass_rbac', True)

    # Integer/IP pools
    vlan_id     = pools.get('vlan_id',          {'start': 100,             'end': 501              })
    vni_pool    = pools.get('vni',              {'start': 1024500,         'end': 1024800          })
    vpc_vni     = pools.get('vpc_vni',          {'start': 60101,           'end': 60999            })
    vpc_dpu_lo  = pools.get('vpc_dpu_lo',       {'start': '10.255.247.3',  'end': '10.255.247.255' })
    ext_vpc_vni = pools.get('external_vpc_vni', {'start': 51008,           'end': 51011            })

    # ── build TOML ───────────────────────────────────────────────────────────
    L = []
    L.append(f'sitename = {_toml_str(sitename)}')
    L.append(f'initial_domain_name = {_toml_str(domain)}')
    L.append(f'attestation_enabled = false')
    L.append(f'bypass_rbac = {"true" if bypass_rbac else "false"}')
    L.append(f'max_concurrent_machine_updates = 20')
    L.append(f'enable_route_servers = {"true" if enable_rs else "false"}')
    L.append(f'datacenter_asn = {datacenter_asn}')
    L.append('')
    L.append(f'dhcp_servers = [{_toml_str(dhcp_vip)}]')
    L.append(f'route_servers = []')
    L.append('')
    L.append(f'site_fabric_prefixes = [{", ".join(_toml_str(p) for p in site_fabric)}]')
    L.append('')
    if deny:
        L.append(f'deny_prefixes = [{", ".join(_toml_str(p) for p in deny)}]')
    else:
        L.append('deny_prefixes = []')
    L.append('')

    L.append('[site_explorer]')
    L.append('run_interval = "30s"')
    L.append('')

    L.append('[machine_validation_config]')
    L.append('enabled = true')
    L.append('')

    L.append('[bom_validation]')
    L.append('enabled = true')
    L.append('ignore_unassigned_machines = true')
    L.append('')

    L.append('[pools.lo-ip]')
    L.append('type = "ipv4"')
    L.append(f'ranges = [{{ start = "{lo_start}", end = "{lo_end}" }}]')
    L.append('')
    L.append('[pools.vlan-id]')
    L.append('type = "integer"')
    L.append(f'ranges = [{{ start = "{vlan_id["start"]}", end = "{vlan_id["end"]}" }}]')
    L.append('')
    L.append('[pools.vni]')
    L.append('type = "integer"')
    L.append(f'ranges = [{{ start = "{vni_pool["start"]}", end = "{vni_pool["end"]}" }}]')
    L.append('')
    L.append('[pools.vpc-vni]')
    L.append('type = "integer"')
    L.append(f'ranges = [{{ start = "{vpc_vni["start"]}", end = "{vpc_vni["end"]}" }}]')
    L.append('')

    if fnn_enabled:
        L.append('[pools.fnn-asn]')
        L.append('type = "integer"')
        L.append(f'ranges = [{{ start = "{fnn_asn_start}", end = "{fnn_asn_end}" }}]')
        L.append('')
        L.append('[pools.vpc-dpu-lo]')
        L.append('type = "ipv4"')
        L.append(f'ranges = [{{ start = "{vpc_dpu_lo["start"]}", end = "{vpc_dpu_lo["end"]}" }}]')
        L.append('')
        L.append('[pools.external-vpc-vni]')
        L.append('type = "integer"')
        L.append(f'ranges = [{{ start = "{ext_vpc_vni["start"]}", end = "{ext_vpc_vni["end"]}" }}]')
        L.append('')

    for net_name, net_cfg in hv.get('networks', {}).items():
        if not net_cfg:
            continue
        net_type = 'admin' if net_name == 'admin' else 'underlay'
        L.append(f'[networks.{net_name}]')
        L.append(f'type = "{net_type}"')
        L.append(f'prefix = "{net_cfg["prefix"]}"')
        L.append(f'gateway = "{net_cfg["gateway"]}"')
        L.append(f'mtu = {net_cfg.get("mtu", 1500 if net_type == "underlay" else 9000)}')
        L.append(f'reserve_first = {net_cfg.get("reserve_first", 2 if net_type == "underlay" else 5)}')
        L.append('')

    L.append('[ib_config]')
    L.append('enabled = false')
    L.append('')

    L.append('[firmware_global]')
    L.append('autoupdate = false')
    L.append('no_reset_retries = true')
    L.append('')

    L.append('[machine_state_controller]')
    L.append('failure_retry_time = "90m"')
    L.append('')

    if fnn_enabled:
        L.append('[fnn.admin_vpc]')
        L.append('enabled = true')
        L.append(f'vpc_vni = {admin_vpc_vni}')
        L.append('')

        a = datacenter_asn

        L.append('[fnn.routing_profiles.INTERNAL]')
        L.append('internal = true')
        L.append('tenant_leak_communities_accepted = true')
        L.append(f'route_target_imports = [{{ asn = {a}, vni = 11 }}, {{ asn = {a}, vni = 101 }}, {{ asn = {a}, vni = 50100 }}]')
        L.append(f'route_targets_on_exports = [{{ asn = {a}, vni = 50200 }}]')
        L.append('')

        L.append('[fnn.routing_profiles.MAINTENANCE]')
        L.append('internal = true')
        L.append(f'route_target_imports = [{{ asn = {a}, vni = 10 }}, {{ asn = {a}, vni = 101 }}, {{ asn = {a}, vni = 50100 }}, {{ asn = {a}, vni = 121 }}]')
        L.append(f'route_targets_on_exports = [{{ asn = {a}, vni = 50300 }}]')
        L.append('')

        L.append('[fnn.routing_profiles.EXTERNAL]')
        L.append('internal = false')
        L.append(f'route_target_imports = [{{ asn = {a}, vni = 50100 }}]')
        L.append(f'route_targets_on_exports = [{{ asn = {a}, vni = 50500 }}]')
        L.append('')

        L.append('[fnn.routing_profiles.PRIVILEGED_INTERNAL]')
        L.append('internal = true')
        L.append(f'route_target_imports = [{{ asn = {a}, vni = 11 }}, {{ asn = {a}, vni = 101 }}, {{ asn = {a}, vni = 50100 }}, {{ asn = {a}, vni = 900 }}, {{ asn = {a}, vni = 1002 }}, {{ asn = {a}, vni = 1003 }}]')
        L.append(f'route_targets_on_exports = [{{ asn = {a}, vni = 50100 }}, {{ asn = {a}, vni = 50200 }}]')
        L.append('')

        L.append('[fnn.admin_vpc.routing_profile]')
        L.append('internal = true')
        L.append(f'route_target_imports = [{{ asn = {a}, vni = 10 }}, {{ asn = {a}, vni = 101 }}, {{ asn = {a}, vni = 50100 }}]')
        L.append(f'route_targets_on_exports = [{{ asn = {a}, vni = 50400 }}]')
        L.append('')

    return '\n'.join(L)


def _toml_str(s):
    return f'"{s}"'


# ── nico umbrella values ──────────────────────────────────────────────────────

def gen_nico(cfg):
    ns    = cfg['nico-system']
    hv    = ns['helm-values']
    comps = hv.get('components', {})
    fab   = cfg['fabric']
    reg   = cfg['registry']

    reg_host = reg['host']
    reg_port = reg['port']
    nico_tag = reg.get('nico_tag', 'latest')
    repository = f'{reg_host}:{reg_port}/nico'

    np       = hv.get('net-plan', {})
    dc_name  = fab.get('dc_name', 'dev')
    sitename = hv.get('sitename', dc_name)
    api_host = f'nico-api.{dc_name}-{sitename}'

    site_toml = gen_site_config_toml(cfg)

    return {
        'global': {
            'image': {
                'repository': repository,
                'tag':        nico_tag,
                'pullPolicy': 'IfNotPresent',
            },
            'imagePullSecrets': [],
            'certificate': {
                'duration':    '720h0m0s',
                'renewBefore': '360h0m0s',
                'privateKey':  {'algorithm': 'ECDSA', 'size': 384},
                'issuerRef': {
                    'kind':  'ClusterIssuer',
                    'name':  'vault-nico-issuer',
                    'group': 'cert-manager.io',
                },
            },
            'spiffe': {'trustDomain': 'nico.local'},
        },

        'nico-api': {
            'certificate': {
                'extraDnsNames': [api_host],
            },
            'siteConfig': {
                'enabled':           True,
                'nicoApiSiteConfig': site_toml,
            },
            'legacyAlias': {
                'aliases': [{
                    'name':            'carbide-api',
                    'namespace':       'forge-system',
                    'createNamespace': True,
                }],
            },
            'resources': {
                'requests': {'cpu': '500m', 'memory': '1Gi'},
                'limits':   {'cpu': '2',    'memory': '4Gi'},
            },
            'externalService': {
                'enabled':     True,
                'annotations': {'metallb.universe.tf/loadBalancerIPs': np.get('api_vip', '')},
            },
            'webAuth': {'mode': 'none'},
            'auth':    {'additionalIssuerCns': ['site-root']},
        },

        'nico-pxe':                   {'enabled': comps.get('nico_pxe', False)},
        'nico-dsx-exchange-consumer': {'enabled': comps.get('nico_dsx_exchange_consumer', False)},
        'nico-flow':                  {'enabled': False},
        'nico-machine-a-tron':        {'enabled': False},
        'unbound':                    {'enabled': False},
        'grafanaDashboards':          {'enabled': False},

        'nico-dhcp': {
            'config': {
                'kea': {
                    # per-arch hook path (this script runs on the VM;
                    # platform.machine() = aarch64 | x86_64 there)
                    'hookLibraryPath': f'/usr/lib/{platform.machine()}-linux-gnu/kea/hooks/libdhcp.so',
                    'hookParameters': {
                        'provisioningServer': np.get('pxe_vip', ''),
                    },
                },
            },
            'externalService': {
                'enabled':     True,
                'annotations': {'metallb.universe.tf/loadBalancerIPs': np.get('dhcp_vip', '')},
            },
        },

        'nico-dns': {
            'externalService': {
                'enabled': True,
                'perPodAnnotations': [
                    {'metallb.universe.tf/loadBalancerIPs': np.get('dns_vip_0', '')},
                    {'metallb.universe.tf/loadBalancerIPs': np.get('dns_vip_1', '')},
                ],
            },
        },

        'nico-ntp': {
            'externalService': {
                'enabled': True,
                'perPodAnnotations': [
                    {'metallb.universe.tf/loadBalancerIPs': np.get('ntp_vip_0', '')},
                    {'metallb.universe.tf/loadBalancerIPs': np.get('ntp_vip_1', '')},
                    {'metallb.universe.tf/loadBalancerIPs': np.get('ntp_vip_2', '')},
                ],
            },
        },

        'nico-ssh-console-rs': {
            'externalService': {
                'enabled':     True,
                'annotations': {'metallb.universe.tf/loadBalancerIPs': np.get('ssh_console_vip', '')},
            },
        },
    }


# ── YAML writer ───────────────────────────────────────────────────────────────

def write_yaml(path, data, header=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = ''
    if header:
        text = ''.join(f'# {line}\n' for line in header.splitlines())
        text += '\n'
    text += yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)
    path.write_text(text)
    print(f'  [ok] {path}')


def generate(cfg, outdir):
    """Generate all values files into outdir. Called by deploy-dev-nico.py."""
    outdir = Path(outdir)
    hdr = f'Generated by generate-dev-values.py\nDo not edit manually.'

    write_yaml(outdir / 'cert-manager.yaml',        gen_cert_manager(cfg),        hdr)
    write_yaml(outdir / 'vault.yaml',               gen_vault(cfg),               hdr)
    write_yaml(outdir / 'eso.yaml',                 gen_eso(cfg),                 hdr)
    write_yaml(outdir / 'zalando-postgres-op.yaml', gen_zalando_postgres_op(cfg), hdr)
    write_yaml(outdir / 'nico-prereqs.yaml',        gen_nico_prereqs(cfg),        hdr)
    write_yaml(outdir / 'nico.yaml',                gen_nico(cfg),                hdr)
    write_yaml(outdir / 'nico-rest-dev.yaml',       gen_rest_overrides(cfg),      hdr)


def gen_rest_overrides(_cfg):
    """Dev overrides layered ON TOP of the reference
    helm-prereqs/values/nico-rest.yaml (which stays authoritative for auth,
    NodePort 30388, keycloak endpoints): single replicas everywhere — the
    reference runs 3 of each, oversized for a 16GB single-node dev VM."""
    return {
        'nico-rest-api':          {'replicaCount': 1},
        'nico-rest-cert-manager': {'replicaCount': 1},
        'nico-rest-site-manager': {'replicaCount': 1},
        'nico-rest-workflow': {
            'cloudWorker': {'replicaCount': 1},
            'siteWorker':  {'replicaCount': 1},
        },
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args   = parse_args()
    site   = resolve_site(args.site)
    cfg    = load_cfg(site)

    if args.output_dir:
        outdir = Path(args.output_dir)
    else:
        outdir = Path(site).parent / 'dev-values'

    print('nico-dev — Helm values generator')
    print(f'  site config : {site}')
    print(f'  output      : {outdir}')
    print()

    generate(cfg, outdir)

    print()
    print('Next: deploy-dev-nico.py', args.site)


if __name__ == '__main__':
    main()
