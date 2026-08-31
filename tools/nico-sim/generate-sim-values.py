#!/usr/bin/env python3
"""
DC Simulation — Helm values generator for Nico system deployment

Reads nico-sim.yaml and generates Helm values YAML files for every chart
that deploy-nico-system.py needs to install.  All Nico customisation
comes from nico-sim.yaml — nothing is hardcoded here.

Sources:
  fabric.*                  — IP prefixes, ASNs, VNIs (fabric topology)
  nico-system.*             — simulation infrastructure (vault mode, image source)
  nico-system.helm-values.* — Helm chart values and siteConfig TOML content

Output (default: ./sim-values/):
  cert-manager.yaml         — jetstack/cert-manager
  vault.yaml                — hashicorp/vault
  eso.yaml                  — external-secrets/external-secrets
  zalando-postgres-op.yaml  — sais/postgres-operator
  nico-prereqs.yaml         — infra-controller-core/helm-prereqs  (nico-prereqs chart)
  nico.yaml                 — infra-controller-core/helm           (nico umbrella chart)

Usage:
  python3 generate-sim-values.py nico-sim.yaml [--output-dir ./sim-values]
"""

import argparse
import ipaddress
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from sim_validate import validate_sim

try:
    import yaml
except ImportError:
    print('Error: pyyaml not installed. Run: sudo apt install python3-yaml')
    sys.exit(1)


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description='Generate Helm values files for Nico system deployment',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument('site', help='Site folder or yaml file')
    p.add_argument('--output-dir', default=None,
                   help='Override output directory (default: '
                        '{infra_controller_repo}/nico-simulation/sim-values/)')
    return p.parse_args()



def resolve_site(arg):
    from pathlib import Path as _P
    import sys as _sys
    p = _P(arg).expanduser()
    if p.is_dir():
        yamls = [f for f in p.glob('*.yaml') if not f.stem.endswith('-mac') and '.kubeconfig' not in f.name]
        if not yamls:
            print(f'Error: no site yaml in {p}', file=_sys.stderr); _sys.exit(1)
        if len(yamls) > 1:
            print(f'Error: multiple yamls in {p}', file=_sys.stderr); _sys.exit(1)
        return str(yamls[0])
    return str(p)


def load_sim(path):
    with open(path) as f:
        return yaml.safe_load(f)


# ── IP helpers ────────────────────────────────────────────────────────────────

def prefix_to_range(prefix_str):
    """Return (first_host_ip, last_host_ip) strings from a prefix."""
    net   = ipaddress.IPv4Network(prefix_str, strict=False)
    hosts = list(net.hosts())
    return str(hosts[0]), str(hosts[-1])


def prefix_first_host(prefix_str):
    net = ipaddress.IPv4Network(prefix_str, strict=False)
    return str(list(net.hosts())[0])


def collect_deny_prefixes(sim):
    """Collect OOB + mgmt + underlay prefixes that Nico should deny for tenant traffic.

    Tenant instances must not route into:
    - Libvirt OOB networks (CP, utility, MH management)
    - ContainerLab management network
    - Underlay/BMC discovery subnets (rack-leaf-4, rack-leaf-5, rack-mat-hosts)
      These are the giaddr source networks — not tenant fabric space.
    - Admin network (host↔DPU internal links)
    """
    deny = []
    cp = sim.get('control_plane', {})
    if cp.get('oob_network', {}).get('prefix'):
        deny.append(cp['oob_network']['prefix'])
    util = sim.get('utility', {})
    if util.get('oob_network', {}).get('prefix'):
        deny.append(util['oob_network']['prefix'])
    mh = sim.get('managed_hosts', {})
    if mh.get('oob_network', {}).get('prefix'):
        deny.append(mh['oob_network']['prefix'])
    mgmt = sim['fabric']['prefixes'].get('mgmt')
    if mgmt:
        deny.append(mgmt)
    # All named networks from nico-system.helm-values.networks go into deny_prefixes.
    # site_fabric_prefixes covers the overlay (7.133.0.0/16); underlay and admin
    # networks are management-plane and must be blocked from tenant traffic.
    hv = sim.get('nico-system', {}).get('helm-values', {})
    for net_cfg in hv.get('networks', {}).values():
        if net_cfg and net_cfg.get('prefix'):
            deny.append(net_cfg['prefix'])
    return deny


def utility_address(sim):
    """Return the registry VM address (fabric-ip:port)."""
    util_prefix = sim['fabric'].get('registry_link',
                    sim['fabric'].get('utility_link', {})).get('prefix', '7.132.0.4/30')
    util_net    = ipaddress.IPv4Network(util_prefix, strict=False)
    util_ip     = str(list(util_net.hosts())[1])   # .2 = utility VM fabric IP
    port        = sim.get('nico_container_registry', sim.get('utility', {})).get('port', 5000)
    return f'{util_ip}:{port}'


# ── cert-manager values ───────────────────────────────────────────────────────

def gen_cert_manager(sim):
    return {
        'installCRDs': True,
        'replicaCount': 1,
    }


# ── Vault values ──────────────────────────────────────────────────────────────

def gen_vault(sim):
    ns    = sim['nico-system']
    mode  = ns['vault']['mode']   # 'dev' | 'production'

    if mode == 'dev':
        token = ns['vault'].get('dev_root_token', 'root')
        return {
            'server': {
                'dev': {
                    'enabled':       True,
                    'devRootToken':  token,
                },
                'standalone': {'enabled': False},
                'dataStorage': {'enabled': False},
            },
            'injector': {'enabled': False},
            'ui':       {'enabled': False},
        }
    else:
        sc   = ns['vault']['storage_class']
        size = ns['vault']['volume_size']
        node = ns['vault'].get('node', 'cp-1')
        return {
            'server': {
                'dev': {'enabled': False},
                'standalone': {'enabled': True},
                'dataStorage': {
                    'enabled':      True,
                    'storageClass': sc,
                    'size':         size,
                },
                'nodeSelector': {'kubernetes.io/hostname': node},
                'affinity': '',
            },
            'injector': {'enabled': False},
            'ui':       {'enabled': False},
        }


# ── ESO values ────────────────────────────────────────────────────────────────

def gen_eso(sim):
    return {
        'installCRDs': True,
        'replicaCount': 1,
    }


# ── Zalando postgres-operator values ─────────────────────────────────────────

def gen_zalando_postgres_op(sim):
    return {
        'configKubernetes': {
            'enable_pod_disruption_budget': False,
        },
    }


# ── nico-prereqs values ───────────────────────────────────────────────────────

def gen_nico_prereqs(sim):
    ns    = sim['nico-system']
    hv    = ns['helm-values']
    pg    = ns['postgresql']
    fab   = sim['fabric']
    pfx   = fab['prefixes']

    # VIP assignments — from nico-system.helm-values.net-plan
    np       = hv.get('net-plan', {})
    dhcp_vip = np.get('dhcp_vip', '')
    pxe_vip  = np.get('pxe_vip', '')
    api_vip  = np.get('api_vip', '')
    if not dhcp_vip:
        raise ValueError('nico-system.helm-values.net-plan.dhcp_vip is required in nico-sim.yaml')
    if not pxe_vip:
        raise ValueError('nico-system.helm-values.net-plan.pxe_vip is required in nico-sim.yaml')

    return {
        'siteName':              hv['sitename'],
        'namespace':             'nico-system',
        'certManagerNamespace':  'cert-manager',
        'vaultNamespace':        'vault',
        'esoNamespace':          'external-secrets',
        'postgresNamespace':     'postgres',
        'clusterApiServer':      '',   # auto-detect

        'vault': {
            # dev mode Vault runs HTTP (no TLS); production uses HTTPS
            'address':  ('http' if ns['vault']['mode'] == 'dev' else 'https')
                        + '://vault.vault.svc.cluster.local:8200',
            'token':    '',    # injected by deploy-nico-system.py after vault init
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
            'siteRoot':              {'create': True},
            'vaultAuthServiceAccount': {'create': True},
            'vaultIssuer':           {'create': True},
        },

        'externalSecrets': {'enabled': True},

        'sshHostKey': {'create': True},

        'azureSSOClientSecret':       '',
        'azureSSOClientSecretSecret': {'create': True},

        'imagePullSecrets': {
            'ngcCarbidePull': '',
            'ngcNvidianKey':  '',
        },

        # Disable optional modules not needed in simulation
        'rest': {'enabled': False, 'namespace': 'nico-rest'},
        'flow': {'enabled': False, 'namespace': 'flow'},

        'postgresql': {
            'enabled':       True,
            'instances':     pg['instances'],
            'volumeSize':    pg['volume_size'],
            'storageClass':  pg['storage_class'],
            'host':          'nico-pg-cluster.postgres.svc.cluster.local',
            'resources': {
                'limits':   {'cpu': '2', 'memory': '2Gi'},
                'requests': {'cpu': '250m', 'memory': '512Mi'},
            },
        },
    }


# ── siteConfig TOML ───────────────────────────────────────────────────────────

def gen_site_config_toml(sim):
    """
    Generate the nico-api siteConfig TOML string.

    Fields come from two sources:
      - nico-sim.yaml fabric.* (prefixes, ASNs, VNIs)
      - nico-sim.yaml nico-system.helm-values.*
    """
    fab   = sim['fabric']
    pfx   = fab['prefixes']
    hv    = sim['nico-system']['helm-values']
    pools = hv.get('pools', {})

    # ── auto-derived values ──────────────────────────────────────────────────
    # [pools.lo-ip] from dpu_loopbacks, skipping IPs already used by DPU stand-ins.
    # cp-dpu-1/2/3 hold the first num_vms hosts — pool starts after them.
    num_cp_vms = sim['control_plane']['num_vms']
    cp         = sim['control_plane']
    lo_net     = ipaddress.IPv4Network(cp.get('dpu_loopbacks', pfx.get('dpu_loopbacks', '7.130.2.0/24')), strict=False)
    lo_hosts   = list(lo_net.hosts())
    lo_start   = str(lo_hosts[num_cp_vms])   # skip .1/.2/.3
    lo_end     = str(lo_hosts[-1])

    # VIPs from net-plan
    np       = hv.get('net-plan', {})
    dhcp_vip = np.get('dhcp_vip', '')
    api_vip  = np.get('api_vip', '')

    # site_fabric_prefixes from overlay
    site_fabric = [pfx['overlay']]

    # deny_prefixes: OOB and management networks
    deny = collect_deny_prefixes(sim)

    # datacenter_asn from switch_asn_base
    datacenter_asn = fab.get('switch_asn_base', fab.get('asn_base', 4266050001))

    fnn_asn_pool  = pools.get('fnn_asn', {})
    fnn_asn_start = fnn_asn_pool.get('start', '')
    fnn_asn_end   = fnn_asn_pool.get('end', '')
    if not fnn_asn_start or not fnn_asn_end:
        raise ValueError('nico-system.helm-values.pools.fnn_asn is required in nico-sim.yaml')

    # VNI pool from evpn config
    evpn         = fab.get('evpn', {})
    cp_vni       = evpn.get('control_plane_vni', 60000)
    mh_vni       = evpn.get('managed_host_vni',  60100)

    # fnn config
    fnn_cfg        = hv.get('fnn', {})
    fnn_enabled    = fnn_cfg.get('enabled', True)
    admin_vpc_vni  = fnn_cfg.get('admin_vpc_vni', 61000)

    # networks section (admin + underlay segments) — iterated when writing TOML below

    # route servers
    enable_rs = hv.get('enable_route_servers', False)

    # ── user-provided values ─────────────────────────────────────────────────
    sitename    = hv.get('sitename',    'dc-sim')
    domain      = hv.get('domain',      f'{sitename}.example.com')
    np          = hv.get('net-plan',    {})
    dhcp_vip    = np.get('dhcp_vip',    '')
    api_vip     = np.get('api_vip',     '')
    bypass_rbac = hv.get('bypass_rbac', True)

    # Integer pools
    vlan_id      = pools.get('vlan_id',          {'start': 100,          'end': 501          })
    vni_pool     = pools.get('vni',              {'start': 1024500,      'end': 1024800      })
    vpc_vni      = pools.get('vpc_vni',          {'start': 60101,        'end': 60999        })
    vpc_dpu_lo   = pools.get('vpc_dpu_lo',       {'start': '10.255.247.3','end': '10.255.247.255'})
    ext_vpc_vni  = pools.get('external_vpc_vni', {'start': 51008,        'end': 51011        })

    # ── build TOML ───────────────────────────────────────────────────────────
    lines = []
    lines.append(f'sitename = {_toml_str(sitename)}')
    lines.append(f'initial_domain_name = {_toml_str(domain)}')
    lines.append(f'attestation_enabled = false')
    lines.append(f'bypass_rbac = {"true" if bypass_rbac else "false"}')
    lines.append(f'max_concurrent_machine_updates = 20')
    lines.append(f'enable_route_servers = {"true" if enable_rs else "false"}')
    lines.append(f'datacenter_asn = {datacenter_asn}')
    lines.append('')
    lines.append(f'dhcp_servers = [{_toml_str(dhcp_vip)}]')
    lines.append(f'route_servers = []')
    lines.append('')
    lines.append(f'site_fabric_prefixes = [{", ".join(_toml_str(p) for p in site_fabric)}]')
    lines.append('')
    if deny:
        deny_list = ', '.join(_toml_str(p) for p in deny)
        lines.append(f'deny_prefixes = [{deny_list}]')
    else:
        lines.append('deny_prefixes = []')
    lines.append('')

    # site_explorer
    lines.append('[site_explorer]')
    lines.append('run_interval = "30s"')
    lines.append('')

    # machine_validation
    lines.append('[machine_validation_config]')
    lines.append('enabled = true')
    lines.append('')

    # bom_validation
    lines.append('[bom_validation]')
    lines.append('enabled = true')
    lines.append('ignore_unassigned_machines = true')
    lines.append('')

    # pools — required
    lines.append('[pools.lo-ip]')
    lines.append('type = "ipv4"')
    lines.append(f'ranges = [{{ start = "{lo_start}", end = "{lo_end}" }}]')
    lines.append('')
    lines.append('[pools.vlan-id]')
    lines.append('type = "integer"')
    lines.append(f'ranges = [{{ start = "{vlan_id["start"]}", end = "{vlan_id["end"]}" }}]')
    lines.append('')
    lines.append('[pools.vni]')
    lines.append('type = "integer"')
    lines.append(f'ranges = [{{ start = "{vni_pool["start"]}", end = "{vni_pool["end"]}" }}]')
    lines.append('')
    lines.append('[pools.vpc-vni]')
    lines.append('type = "integer"')
    lines.append(f'ranges = [{{ start = "{vpc_vni["start"]}", end = "{vpc_vni["end"]}" }}]')
    lines.append('')

    # FNN pools (only when fnn.enabled)
    if fnn_enabled:
        lines.append('[pools.fnn-asn]')
        lines.append('type = "integer"')
        lines.append(f'ranges = [{{ start = "{fnn_asn_start}", end = "{fnn_asn_end}" }}]')
        lines.append('')
        lines.append('[pools.vpc-dpu-lo]')
        lines.append('type = "ipv4"')
        lines.append(f'ranges = [{{ start = "{vpc_dpu_lo["start"]}", end = "{vpc_dpu_lo["end"]}" }}]')
        lines.append('')
        lines.append('[pools.external-vpc-vni]')
        lines.append('type = "integer"')
        lines.append(f'ranges = [{{ start = "{ext_vpc_vni["start"]}", end = "{ext_vpc_vni["end"]}" }}]')
        lines.append('')

    # admin network (type=admin) + underlay networks (type=underlay)
    all_nets = hv.get('networks', {})
    for net_name, net_cfg in all_nets.items():
        if not net_cfg:
            continue
        # admin has type="admin"; everything else is type="underlay"
        net_type = 'admin' if net_name == 'admin' else 'underlay'
        lines.append(f'[networks.{net_name}]')
        lines.append(f'type = "{net_type}"')
        lines.append(f'prefix = "{net_cfg["prefix"]}"')
        lines.append(f'gateway = "{net_cfg["gateway"]}"')
        lines.append(f'mtu = {net_cfg.get("mtu", 1500 if net_type == "underlay" else 9000)}')
        lines.append(f'reserve_first = {net_cfg.get("reserve_first", 2 if net_type == "underlay" else 5)}')
        lines.append('')

    # IB disabled for simulation
    lines.append('[ib_config]')
    lines.append('enabled = false')
    lines.append('')

    # firmware global
    lines.append('[firmware_global]')
    lines.append('autoupdate = false')
    lines.append('no_reset_retries = true')
    lines.append('')

    # machine state controller
    lines.append('[machine_state_controller]')
    lines.append('failure_retry_time = "90m"')
    lines.append('')

    # FNN admin VPC and routing profiles
    if fnn_enabled:
        lines.append('[fnn.admin_vpc]')
        lines.append('enabled = true')
        lines.append(f'vpc_vni = {admin_vpc_vni}')
        lines.append('')

        # ── FNN routing profiles ────────────────────────────────────────────
        # Placeholder VNIs from nico-core.yaml with datacenter_asn substituted.
        # Syntactically required; inert without real FNN gateways.
        a = datacenter_asn

        lines.append('[fnn.routing_profiles.INTERNAL]')
        lines.append('internal = true')
        lines.append('tenant_leak_communities_accepted = true')
        lines.append(f'route_target_imports = [{{ asn = {a}, vni = 11 }}, {{ asn = {a}, vni = 101 }}, {{ asn = {a}, vni = 50100 }}]')
        lines.append(f'route_targets_on_exports = [{{ asn = {a}, vni = 50200 }}]')
        lines.append('')

        lines.append('[fnn.routing_profiles.MAINTENANCE]')
        lines.append('internal = true')
        lines.append(f'route_target_imports = [{{ asn = {a}, vni = 10 }}, {{ asn = {a}, vni = 101 }}, {{ asn = {a}, vni = 50100 }}, {{ asn = {a}, vni = 121 }}]')
        lines.append(f'route_targets_on_exports = [{{ asn = {a}, vni = 50300 }}]')
        lines.append('')

        lines.append('[fnn.routing_profiles.EXTERNAL]')
        lines.append('internal = false')
        lines.append(f'route_target_imports = [{{ asn = {a}, vni = 50100 }}]')
        lines.append(f'route_targets_on_exports = [{{ asn = {a}, vni = 50500 }}]')
        lines.append('')

        lines.append('[fnn.routing_profiles.PRIVILEGED_INTERNAL]')
        lines.append('internal = true')
        lines.append(f'route_target_imports = [{{ asn = {a}, vni = 11 }}, {{ asn = {a}, vni = 101 }}, {{ asn = {a}, vni = 50100 }}, {{ asn = {a}, vni = 900 }}, {{ asn = {a}, vni = 1002 }}, {{ asn = {a}, vni = 1003 }}]')
        lines.append(f'route_targets_on_exports = [{{ asn = {a}, vni = 50100 }}, {{ asn = {a}, vni = 50200 }}]')
        lines.append('')

        lines.append('[fnn.admin_vpc.routing_profile]')
        lines.append('internal = true')
        lines.append(f'route_target_imports = [{{ asn = {a}, vni = 10 }}, {{ asn = {a}, vni = 101 }}, {{ asn = {a}, vni = 50100 }}]')
        lines.append(f'route_targets_on_exports = [{{ asn = {a}, vni = 50400 }}]')
        lines.append('')

    return '\n'.join(lines)


def _toml_str(s):
    return f'"{s}"'


# ── nico umbrella values ──────────────────────────────────────────────────────

def gen_nico(sim):
    ns    = sim['nico-system']
    hv    = ns['helm-values']
    img   = ns['image']
    comps = hv.get('components', {})
    fab   = sim['fabric']
    pfx   = fab['prefixes']

    # Determine image repository and tag.
    # The nico chart renders images as "{repository}:{tag}" (no image name appended),
    # so repository must include the image name: e.g. "7.130.4.2:5000/nico".
    if img['source'] == 'local':
        repository = utility_address(sim) + '/nico'
        nico_tag   = img.get('local_nico_tag', 'latest')
    else:
        repository = img.get('nvcr_repository', '')
        nico_tag   = img.get('nvcr_nico_tag', '')

    # VIP assignments from nico-system.helm-values.net-plan
    np       = hv.get('net-plan', {})
    dhcp_vip = np.get('dhcp_vip', '')
    pxe_vip  = np.get('pxe_vip', '')
    api_vip  = np.get('api_vip', '')
    dc_name  = fab.get('dc_name', 'nico-sim')
    sitename = hv.get('sitename', dc_name)
    api_host = f'nico-api.{dc_name}-{sitename}'

    # Build siteConfig TOML
    site_toml = gen_site_config_toml(sim)

    values = {
        'global': {
            'image': {
                'repository':  repository,
                'tag':         nico_tag,
                'pullPolicy':  'IfNotPresent',
            },
            'imagePullSecrets': [],
            'certificate': {
                'duration':    '720h0m0s',
                'renewBefore': '360h0m0s',
                'privateKey':  {'algorithm': 'ECDSA', 'size': 384},
                'issuerRef':   {
                    'kind':  'ClusterIssuer',
                    'name':  'vault-nico-issuer',
                    'group': 'cert-manager.io',
                },
            },
            'spiffe': {'trustDomain': 'nico.local'},
        },

        # siteConfig — the TOML injected at startup
        'nico-api': {
            'certificate': {
                'extraDnsNames': [api_host],
            },
            'siteConfig': {
                'enabled':           True,
                'nicoApiSiteConfig': site_toml,
            },
            # forge-system doesn't pre-exist in simulation (it's a pre-rename
            # namespace that production inherits). Tell helm to create it so the
            # legacy ExternalName Service for carbide-api can be installed.
            # Must provide the full alias object — helm replaces lists, not merges.
            'legacyAlias': {
                'aliases': [{
                    'name':            'carbide-api',
                    'namespace':       'forge-system',
                    'createNamespace': True,
                }],
            },
            # Reduce resource requests for simulation — CP VMs have 8Gi RAM each
            # and production defaults (8Gi request, 32Gi limit) won't schedule.
            'resources': {
                'requests': {'cpu': '500m',  'memory': '1Gi'},
                'limits':   {'cpu': '2',     'memory': '4Gi'},
            },
            'externalService': {
                'enabled':     True,
                'annotations': {'metallb.universe.tf/loadBalancerIPs': np.get('api_vip', '')},
            },
            # Disable web auth in simulation — MAT and other gRPC clients connect
            # on the same port (443→1079) as the web UI. The web auth middleware
            # intercepts gRPC requests before they reach the gRPC handler, returning
            # HTTP 403. In simulation bypass_rbac=true handles authorization.
            'webAuth': {'mode': 'none'},
            # Allow any cert issued by 'site-root' (our Vault PKI CA) to authenticate
            # as ForgeAdminCLI (admin CLI principal). Without this, additional_issuer_cns=[]
            # and nico-admin-cli can never get ForgeAdminCLI access.
            'auth': {'additionalIssuerCns': ['site-root']},
        },

        # Subchart enables — defaults from chart are true; we only override false ones
        'nico-pxe':                   {'enabled': comps.get('nico_pxe', False)},
        'nico-dsx-exchange-consumer': {'enabled': comps.get('nico_dsx_exchange_consumer', False)},
        'nico-flow':                  {'enabled': False},
        'nico-machine-a-tron':        {'enabled': False},
        'unbound':                    {'enabled': False},
        'grafanaDashboards':          {'enabled': False},

        # DHCP hook parameters + MetalLB VIP
        'nico-dhcp': {
            'config': {
                'kea': {
                    'hookParameters': {
                        # PXE VIP from nico-sim.yaml — used as kea next-server even when
                        # nico_pxe is disabled. Must be a valid IP or kea-dhcp4 refuses to start.
                        'provisioningServer': pxe_vip,
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

    return values


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


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    sim  = load_sim(resolve_site(args.site))
    validate_sim(sim)

    # Resolve output directory:
    # sim-values are written to {site_folder}/sim-values/ — co-located with the
    # site yaml, not inside the nico repo. This keeps site-specific generated
    # files out of the code repo and works for any user (incl. root).
    if args.output_dir:
        outdir = Path(args.output_dir)
    else:
        site_yaml_path = resolve_site(args.site)
        site_folder = Path(site_yaml_path).parent
        outdir = site_folder / 'sim-values'
        outdir.mkdir(parents=True, exist_ok=True)

    print(f'DC Simulation — Helm values generator')
    print(f'  sim config : {resolve_site(args.site)}')
    print(f'  output     : {outdir}')
    sitename = sim['nico-system']['helm-values'].get('sitename', 'dc-sim')
    src      = sim['nico-system']['image']['source']
    print(f'  sitename   : {sitename}')
    print(f'  image src  : {src}'
          + (f'  ({utility_address(sim)})' if src == 'local' else ''))
    print()

    hdr = f'Generated by generate-sim-values.py from {resolve_site(args.site)}\nDo not edit manually.'

    write_yaml(outdir / 'cert-manager.yaml',
               gen_cert_manager(sim), hdr)

    write_yaml(outdir / 'vault.yaml',
               gen_vault(sim), hdr)

    write_yaml(outdir / 'eso.yaml',
               gen_eso(sim), hdr)

    write_yaml(outdir / 'zalando-postgres-op.yaml',
               gen_zalando_postgres_op(sim), hdr)

    write_yaml(outdir / 'nico-prereqs.yaml',
               gen_nico_prereqs(sim), hdr)

    write_yaml(outdir / 'nico.yaml',
               gen_nico(sim), hdr)

    print()
    print('Next: deploy-nico-system.py nico-sim.yaml --values-dir', outdir)


if __name__ == '__main__':
    main()
