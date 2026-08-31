#!/usr/bin/env python3
"""
DC Simulation — cross-reference validation for nico-sim.yaml

Imported by all generator scripts. Call validate_sim(sim) early in main()
to catch configuration errors before any files are written or VMs created.
"""


def validate_sim(sim):
    """
    Validate all cross-references in nico-sim.yaml. Raises ValueError on the
    first problem found, with a message that names the offending keys.
    """
    errors = []

    fab      = sim.get('fabric', {})
    pfx      = fab.get('prefixes', {})
    ns       = sim.get('nico-system', {})
    hv       = ns.get('helm-values', {})
    networks = hv.get('networks', {})
    underlay = ns.get('underlay', {})
    mh       = sim.get('managed_hosts', {})
    num_vms  = mh.get('num_vms', {})

    num_leafs    = fab.get('num_leafs', 0)
    num_cp_leafs = fab.get('num_control_plane_leafs', 0)

    # Valid MH leaves: numbered leafs above CP range + any named leafs in underlay_leafs
    # (e.g. leaf-mat is a named special leaf, not in the numbered range)
    underlay_leafs = fab.get('underlay_leafs', {})
    named_leaves   = {v.get('leaf') for v in underlay_leafs.values() if isinstance(v, dict)}
    numbered_leaves = {f'leaf-{i}' for i in range(num_cp_leafs + 1, num_leafs + 1)}
    valid_mh_leaves = numbered_leaves | named_leaves

    # Underlays with relay: false use API mode (no VMs) — exempt from num_vms coverage check
    api_mode_underlays = {
        uname for uname, cfg in underlay_leafs.items()
        if isinstance(cfg, dict) and not cfg.get('relay', True)
    }

    # ── underlay: keys must exist in networks ─────────────────────────────────
    for uname, leaf in underlay.items():
        if uname not in networks:
            errors.append(
                f"nico-system.underlay['{uname}'] references an underlay network "
                f"that does not exist in nico-system.helm-values.networks "
                f"(defined: {sorted(networks)})"
            )
        if leaf not in valid_mh_leaves:
            errors.append(
                f"nico-system.underlay['{uname}']: '{leaf}' is not a valid MH leaf. "
                f"Valid leaves: {sorted(valid_mh_leaves)}"
            )

    # ── managed_hosts.num_vms: keys must exist in both networks and underlay ──
    if isinstance(num_vms, dict):
        for uname, count in num_vms.items():
            if uname not in networks:
                errors.append(
                    f"managed_hosts.num_vms['{uname}'] references an underlay network "
                    f"not in nico-system.helm-values.networks "
                    f"(defined: {sorted(networks)})"
                )
            if uname not in underlay:
                errors.append(
                    f"managed_hosts.num_vms['{uname}'] references an underlay not in "
                    f"nico-system.underlay. rack-mat-hosts must be omitted from underlay "
                    f"(MAT uses API mode). Defined in underlay: {sorted(underlay)}"
                )
            if not isinstance(count, int) or count < 0:
                errors.append(
                    f"managed_hosts.num_vms['{uname}']: count must be a non-negative "
                    f"integer, got {count!r}"
                )
    elif isinstance(num_vms, int):
        errors.append(
            "managed_hosts.num_vms must be a dict mapping underlay name → VM count, "
            "not a scalar integer. Example:\n"
            "  num_vms:\n"
            "    rack-leaf-4: 2\n"
            "    rack-leaf-5: 2"
        )

    # ── underlay coverage: every non-API-mode underlay should have num_vms ─────
    if isinstance(num_vms, dict):
        for uname in underlay:
            if uname in api_mode_underlays:
                continue   # MAT and other API-mode underlays have no VMs — skip
            if uname not in num_vms:
                errors.append(
                    f"nico-system.underlay defines '{uname}' → '{underlay[uname]}' "
                    f"but managed_hosts.num_vms has no entry for '{uname}'. "
                    f"Add '{uname}: 0' to suppress this error if intentional."
                )

    # ── chart-versions: all required keys must be present ────────────────────
    cv = sim.get('chart-versions', {})
    required_charts = [
        'metallb', 'local-path-provisioner', 'cert-manager',
        'vault', 'external-secrets', 'postgres-operator',
    ]
    missing = [k for k in required_charts if not cv.get(k)]
    if missing:
        errors.append(
            f"chart-versions is missing required entries: {missing}"
        )

    # ── registry_link: if enabled, prefix must be set ────────────────────────
    reg_link = fab.get('registry_link', {})
    if reg_link.get('enabled'):
        if not reg_link.get('prefix'):
            errors.append("fabric.registry_link.enabled=true but prefix is not set")

    # ── Report all errors together ────────────────────────────────────────────
    if errors:
        lines = '\n'.join(f'  • {e}' for e in errors)
        raise ValueError(
            f"nico-sim.yaml validation failed ({len(errors)} error(s)):\n{lines}"
        )
