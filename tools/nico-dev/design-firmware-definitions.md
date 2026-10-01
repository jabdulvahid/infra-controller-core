# Firmware definitions in nico-dev — design

Status: draft for review, 2026-09-30. Replaces the `firmware_sim` key.

## 1. Problem

nico-dev has one way to make nico-api upgrade host firmware: `firmware_sim: true`
in `bringup.yaml`. It does three unrelated things at once, one of which is
MAT-specific, and none of them is something a user can shape:

| what it does | where | user control |
|---|---|---|
| writes one firmware definition, vendor Nvidia model "GB200 NVL", into nico-api's firmware directory from a string literal in `generate_dev_values.py`, through a busybox init container on the nico-api pod | nico-api | the two version numbers only |
| sets `firmware_global.autoupdate = true` in nico-api's site config | nico-api | none, tied to the key |
| sets `host_firmware_versions` in the generated `mat-config.toml` | MAT | none, tied to the key |

A developer who wants nico-api to know a second model, a third component, a
different inventory regex or a real artifact has no hook: the only definition is
the mock's, and every comment about the feature talks about MAT. That ties
nico-dev's positioning to one use case. MAT is a consumer of nico-dev, not its
purpose.

## 2. What nico itself supports

nico-api builds its host firmware catalog from three sources, merged in this
order (`docs/operations/firmware-updates/configuration.md`):

1. static `host_models` configuration shipped with the deployment;
2. legacy `metadata.toml` files, one subdirectory each, under
   `firmware_global.firmware_directory` (`/opt/nico/firmware`), which the
   chart mounts as an empty `emptyDir` with no values hook;
3. the Host Firmware Config API,
   `PUT /v2/org/{org}/nico/firmware-config/host`, highest precedence, and the
   path upstream documents as the one new definitions should use.

An artifact is either a local `filename`, which has to exist inside the pod, or
a `url` with an optional `sha256`. nico-api downloads URL artifacts into
`firmware_global.firmware_download_cache_directory`
(`/mnt/persistence/fw/download-cache`, on the pod's persistence volume) and
verifies the digest when one is given (`crates/firmware/src/downloader.rs`,
`verify_sha256` returns Ok on an empty checksum).

So nico is generic. nico-dev only ever exposed the legacy path, and only for
one hard-coded file.

## 3. Design

Three pieces, all inside the cluster, MAT nowhere in them.

### 3.1 The site keeps definitions as files

```text
<site>/firmware/
├── gb200-mock/
│   ├── metadata.toml
│   ├── bmc-2.0.bin
│   └── uefi-2.0.bin
└── my-model/
    └── metadata.toml            # artifacts by URL, nothing local
```

One directory per definition, the same layout nico's legacy path uses, so an
existing `metadata.toml` from a real site drops in unchanged. Local artifacts sit
next to the file; a `filename` in `known_firmware` that names a sibling file is
served by 3.2 and rewritten to its URL by 3.3.

`bringup.yaml`:

```yaml
firmware:
  definitions: ./firmware        # directory copied to <site>/firmware/ by create-dev-site.py; optional
  autoupdate: false              # firmware_global.autoupdate; default false
```

Both keys optional. Without `definitions`, nico-api has no firmware definition
and the release in 3.2 is not deployed. `autoupdate` is its own key because it
is a nico-api site-config setting with its own meaning: whether drift alone
starts an update, before and after ingestion. The site yaml carries the same two
keys under `nico-system.firmware`.

### 3.2 A `firmware-repo` release serves the artifacts

A small chart under `tools/nico-dev/charts/nico-dev-firmware-repo/`:

- Deployment `nico-firmware-repo` in `nico-system`: one nginx container,
  `autoindex on`, serving a read-only hostPath volume at the VM path of
  `<site>/firmware/` (`<nico_vm_folder>/sites/<dc>/<site>/firmware`). The share
  is the source, so a file added on the Mac is served without a restart.
- Service `nico-firmware-repo`, ClusterIP, port 80.

Artifacts are then `http://nico-firmware-repo.nico-system.svc/<def>/<file>`.
No VIP, no fabric address, nothing outside the cluster. A developer testing real
firmware points `url` at their own repository and never uses this service.

`deploy-dev-nico.py` gets `firmware-repo` in `DEPLOY_ORDER` after `nico`, as a
Helm release like the others, so it is a step in the progress viewer and
`--only firmware-repo` works. It is skipped when the site has no
`firmware.definitions`.

### 3.3 A push step registers the definitions through the API

`push-firmware-definitions.py <site>`, run by bring-up after `nico` and
`firmware-repo` are up, and by hand after editing a definition:

1. reads every `<site>/firmware/*/metadata.toml`;
2. for each `known_firmware` entry with a `filename` that is a sibling file,
   replaces it with the service URL from 3.2 and adds the file's SHA-256;
   `url` entries pass through untouched;
3. converts the TOML to the API's JSON body (`vendor`, `model`, `components[]`
   with `type`, `currentVersionDetectionRegEx`, `preingestUpgradeWhenBelow`,
   `firmware[]` with `version`, `default`, `artifacts[]`; `ordering`,
   `explicitStartNeeded`) and PUTs it to
   `/v2/org/{org}/nico/firmware-config/host` with the Keycloak token
   `run-nicocli.sh` already obtains;
4. prints what it registered and the effective defaults from the admin CLI.

Rerunnable: PUT is create-or-update. Deleting a definition is
`DELETE /v2/org/{org}/nico/firmware-config/host` with vendor and model; the
script gets a `--delete <dir>` for it.

The init container in `generate_dev_values.py` goes away. The nico-api pod is
untouched by this feature.

### 3.4 The GB200 mock definition becomes an example

`tools/nico-dev/examples/firmware/gb200-mock/metadata.toml`, the file the init
container writes today, with its two dummy artifacts and a comment explaining
the mock's vendor, model and inventory ids. The MAT how-to section says: copy
this directory into `bringup.yaml`'s `firmware.definitions`, set
`autoupdate: true`, and set `host_firmware_versions` in `mat-config.toml` below
the example's versions. `configure-clis.py` gets `--host-firmware-versions
bmc=1.0,uefi=1.0` so that last step is a flag rather than a hand edit. That is
the only place MAT and firmware meet.

### 3.5 Migration

- `firmware_sim` is removed from `bringup.yaml`, the site yaml template,
  `create-dev-site.py`, both bring-up scripts, `generate_dev_values.py` and
  `configure-clis.py`. No alias: nothing is tagged validated yet and the only
  known user is `dc1/feature1`.
- feature1 is converted by hand: `firmware:` block, example directory copied,
  `bring-up.py --from nico --until firmware-repo`, then the push step.
- `how-to-mac.md` Step 11 and `how-to-linux.md` Step 11 are rewritten around the
  example, `bringup-example.yaml` gets the `firmware:` block with a comment that
  does not mention MAT.

## 4. Open items, to verify before or during implementation

1. **URL download from the in-cluster service.** nico-api has never downloaded
   an artifact on a dev site. Hand-PUT a definition against feature1 whose
   artifact is a plain-http URL and confirm the download lands in
   `/mnt/persistence/fw/download-cache` and the upload proceeds. This also
   confirms plain http is accepted (the downloader uses a reqwest client with
   no scheme restriction visible in the code; the test URLs are https).
2. **siteId.** The PUT body requires `siteId`. Find where the REST stack
   records the site's id on a dev deployment (site-agent values or
   `GET .../sites`) so the push step can read it rather than ask.
3. **API to Core propagation.** The REST API hands the definition to Core; the
   `HostFirmwareConfigCoreUnavailable` response exists. Confirm a definition
   pushed before MAT starts is in the catalog when preingestion first checks,
   and what the failure looks like if nico-api is not yet reachable.
4. **hostPath over the 9p share.** MAT's runtime files had to be staged
   VM-local because a 0600 key is unreadable across the share. Artifacts are
   world-readable, so nginx as an unprivileged user should read them; if not,
   the release gets a copy step into a VM-local directory and the share stops
   being the live source.
5. **nicocli for the body.** `nicocli host-firmware-config update` exists, but
   the CLI maps body properties to flags, which is awkward for a nested
   `components[]` list. The push step calls the REST endpoint directly; nicocli
   stays the manual inspection tool.
6. **Vendor and model mapping.** The API body carries no inventory regex;
   Core derives `current_version_reported_as` from its built-in vendor and
   model mapping and rejects a pair it does not know
   (`docs/operations/firmware-updates/configuration.md`). Confirm Nvidia /
   "GB200 NVL" with BMC and UEFI components is in that mapping and yields
   `^FW_BMC_0$` and `^HGX_FW_CPU_0$`; if not, the mock example cannot use the
   API route and the design needs the legacy directory as a fallback.
7. **Definitions without artifacts.** A component entry with
   `preingest_upgrade_when_below` but no `known_firmware` cannot upload; decide
   whether the push step rejects it or lets nico-api report the gap.

## 5. Out of scope

- DPU firmware baselines and the rack or tray firmware operations; this covers
  host definitions only.
- Serving the firmware directory to anything outside the cluster.
- The two defects found on 2026-09-30 (bmc-mock BMC activation, preingestion
  gate); this design does not change how nico-api or the mock behave once a
  definition exists.
