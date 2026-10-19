#!/usr/bin/env python3
"""
nico-dev — drive the pre-ingestion firmware check through incomplete exploration
reports, hands-free, using bmc-mock's built-in injection rules.

Runs on the site VM next to MAT. It polls `run-admin-cli.sh site-explorer
get-report all`, assigns one scenario to each host BMC endpoint as it appears,
arms bmc-mock injection rules on that BMC (POST https://<bmc>/Injection/rules),
and releases or tightens them when the endpoint reaches the states the scenario
keys on. Every state change is logged with a timestamp; at the end each host
gets a verdict against its scenario's expectation.

The rules only rewrite what the mock answers, so NICo sees exactly what a real
BMC with a partial Redfish tree would give it. Nothing touches the database.

Scenarios (the firmware definition must list the inventory ids with a
pre-ingestion minimum; nico-dev's `firmware_sim` lists FW_BMC_0 and
HGX_FW_CPU_0, both reported below the minimum):

  good                 no rule. Expect: no refresh wait, upgrade starts.
  blank-version        both inventory members answer with Version null until the
                       endpoint parks in FirmwareCheckRefreshWait; then the rule
                       is removed. Expect: one wait, then upgrade.
  blank-version-still  same rule, never removed. Expect: one wait, then Complete
                       with no upgrade (the refreshed report is decided as is).
  drop-entry           both members answer 404, so the explorer leaves them out
                       of the report; released at the wait. Expect: wait, upgrade.
  drop-entry-still     never released. Expect: wait, then Complete, no upgrade.
  blank-model          Systems/* and Chassis/* answer Model null; released at the
                       wait. Expect: wait, then upgrade.
  blank-model-still    never released. Expect: wait, then Complete, no upgrade
                       (no host model, so no firmware definition matches).
  refresh-fails        blank-version until the wait; then every GET under
                       /redfish answers 503 for --outage-seconds, so the refresh
                       the deferral asked for fails. Expect: the endpoint stays
                       in FirmwareCheckRefreshWait with a last exploration error
                       for the whole outage, then one wait total and upgrade.

Both inventory members are blanked or dropped together on purpose: a component
that is below its minimum starts the upload before the deferral is considered,
so blanking only one of two below-minimum components would never park.

Usage on the VM (site dir is the one holding run-admin-cli.sh):

  preingest-perturb.py --site ~/mac/sites/dc1/f7023 \
      --scenarios good,blank-version,blank-version-still,drop-entry,refresh-fails

Start it before `run-mat.sh` (or right after `reset-mat-state.py`), so the
rules are armed before the first exploration. Hosts are assigned scenarios in
the order their BMC endpoints appear; `--assign <ip>=<scenario>` pins one.
"""

import argparse
import base64
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

WAIT = "FirmwareCheckRefreshWait"
UPGRADE_STATES = ("UpgradeFirmwareWait", "ResetForNewFirmware", "RecheckVersions",
                  "NewFirmwareReportedWait")
COMPLETE = "Complete"

SCENARIOS = {
    "good":                {"arm": None,          "release": False, "expect": "upgrade",  "waits": 0},
    "blank-version":       {"arm": "blank",       "release": True,  "expect": "upgrade",  "waits": 1},
    "blank-version-still": {"arm": "blank",       "release": False, "expect": "complete", "waits": 1},
    "drop-entry":          {"arm": "drop",        "release": True,  "expect": "upgrade",  "waits": 1},
    "drop-entry-still":    {"arm": "drop",        "release": False, "expect": "complete", "waits": 1},
    "blank-model":         {"arm": "model",       "release": True,  "expect": "upgrade",  "waits": 1},
    "blank-model-still":   {"arm": "model",       "release": False, "expect": "complete", "waits": 1},
    "refresh-fails":       {"arm": "blank",       "release": True,  "expect": "upgrade",  "waits": 1,
                            "outage": True},
}

RULE_ARM = "pp-arm"
RULE_OUTAGE = "pp-outage"


def now():
    return datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S")


class Log:
    def __init__(self, path):
        self.f = open(path, "a") if path else None

    def __call__(self, msg):
        line = f"{now()} {msg}"
        print(line, flush=True)
        if self.f:
            self.f.write(line + "\n")
            self.f.flush()


def field(d, name):
    """proto field by snake_case or camelCase name."""
    if name in d:
        return d[name]
    parts = name.split("_")
    camel = parts[0] + "".join(p.title() for p in parts[1:])
    return d.get(camel)


def state_name(s):
    """`UpgradeFirmwareWait { .. }` -> `UpgradeFirmwareWait`."""
    return (s or "").split(" ")[0].split("{")[0].strip()


class Bmc:
    """Injection-rule client for one mock BMC."""

    def __init__(self, ip, user, password, log, dry_run):
        self.base = f"https://{ip}/Injection/rules"
        self.auth = None
        if user:
            token = base64.b64encode(f"{user}:{password}".encode()).decode()
            self.auth = f"Basic {token}"
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE
        self.log = log
        self.dry_run = dry_run

    def _call(self, method, url, body=None):
        if self.dry_run:
            self.log(f"  dry-run {method} {url} {json.dumps(body) if body else ''}")
            return 200, b""
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("content-type", "application/json")
        if self.auth:
            req.add_header("authorization", self.auth)
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except (urllib.error.URLError, OSError) as e:
            return 0, str(e).encode()

    def add(self, rule):
        status, body = self._call("POST", self.base, rule)
        ok = 200 <= status < 300
        self.log(f"  rule {rule['id']} add -> {status}{'' if ok else ' ' + body[:200].decode(errors='replace')}")
        return ok

    def delete(self, rule_id):
        status, body = self._call("DELETE", f"{self.base}/{rule_id}")
        ok = 200 <= status < 300 or status == 404
        self.log(f"  rule {rule_id} delete -> {status}{'' if ok else ' ' + body[:200].decode(errors='replace')}")
        return ok

    def list(self):
        status, body = self._call("GET", self.base)
        if 200 <= status < 300 and body:
            try:
                return json.loads(body)
            except ValueError:
                return None
        return None


def member_selector(inventory_ids):
    return {"Any": [{"OdataId": f"/redfish/v1/UpdateService/FirmwareInventory/{i}"}
                    for i in inventory_ids]}


def arm_rule(kind, inventory_ids):
    if kind == "blank":
        return {"id": RULE_ARM, "selector": member_selector(inventory_ids),
                "action": {"JsonMerge": {"Version": None}}}
    if kind == "drop":
        return {"id": RULE_ARM, "selector": member_selector(inventory_ids),
                "action": {"Status": 404}}
    if kind == "model":
        return {"id": RULE_ARM,
                "selector": {"Any": [{"OdataId": "/redfish/v1/Systems/*"},
                                     {"OdataId": "/redfish/v1/Chassis/*"}]},
                "action": {"JsonMerge": {"Model": None}}}
    raise ValueError(kind)


OUTAGE_RULE = {"id": RULE_OUTAGE,
               "selector": {"Path": {"method": "GET", "glob": "/redfish/**"}},
               "action": {"Status": 503}}


class Host:
    def __init__(self, ip, scenario, bmc):
        self.ip = ip
        self.scenario = scenario
        self.spec = SCENARIOS[scenario]
        self.bmc = bmc
        self.state = None
        self.waits = 0            # entries into FirmwareCheckRefreshWait
        self.armed = False
        self.released = False
        self.outage_started = None
        self.outage_error_seen = False
        self.outage_state_broke = False
        self.saw_upgrade = False
        self.saw_complete = False
        self.done = False
        self.history = []

    def arm(self, inventory_ids, log):
        if self.spec["arm"] is None:
            self.armed = True
            return
        log(f"{self.ip} [{self.scenario}] arming {self.spec['arm']} rule")
        self.armed = self.bmc.add(arm_rule(self.spec["arm"], inventory_ids))

    def observe(self, ep, outage_seconds, log):
        """One poll of this host's endpoint row."""
        raw = field(ep, "preingestion_state") or ""
        st = state_name(raw)
        report = field(ep, "report") or {}
        err = field(report, "last_exploration_error")
        requested = bool(field(ep, "exploration_requested"))

        if st != self.state:
            log(f"{self.ip} [{self.scenario}] {self.state or '-'} -> {raw}"
                f"{' (exploration requested)' if requested else ''}"
                f"{' error=' + str(err)[:80] if err else ''}")
            self.history.append((now(), st))
            if st == WAIT:
                self.waits += 1
                self.on_wait(log)
            if st in UPGRADE_STATES:
                self.saw_upgrade = True
            if st == COMPLETE:
                self.saw_complete = True
            self.state = st

        if self.outage_started is not None:
            if err:
                self.outage_error_seen = True
            if st != WAIT:
                self.outage_state_broke = True
            if time.monotonic() - self.outage_started >= outage_seconds:
                log(f"{self.ip} [{self.scenario}] outage over "
                    f"(error seen: {self.outage_error_seen}, state held: {not self.outage_state_broke}); "
                    f"lifting 503")
                self.bmc.delete(RULE_OUTAGE)
                self.outage_started = None
                self.outage_lifted = True

        if (self.saw_complete or self.saw_upgrade) and self.outage_started is None:
            self.done = True

    def on_wait(self, log):
        if self.waits > 1:
            log(f"{self.ip} [{self.scenario}] WARNING: parked a second time")
            return
        if self.spec.get("outage"):
            log(f"{self.ip} [{self.scenario}] parked; releasing arm rule and starting 503 outage")
            self.bmc.delete(RULE_ARM)
            self.released = True
            if self.bmc.add(OUTAGE_RULE):
                self.outage_started = time.monotonic()
        elif self.spec["release"]:
            log(f"{self.ip} [{self.scenario}] parked; releasing arm rule")
            self.released = self.bmc.delete(RULE_ARM)
        else:
            log(f"{self.ip} [{self.scenario}] parked; rule stays")

    def verdict(self):
        problems = []
        if self.waits != self.spec["waits"]:
            problems.append(f"waited {self.waits}x, expected {self.spec['waits']}")
        if self.spec["expect"] == "upgrade" and not self.saw_upgrade:
            problems.append("no upgrade state seen")
        if self.spec["expect"] == "complete" and (self.saw_upgrade or not self.saw_complete):
            problems.append("expected Complete without an upgrade")
        if self.spec.get("outage"):
            if not getattr(self, "outage_lifted", False):
                problems.append("outage never ran")
            if not self.outage_error_seen:
                problems.append("no exploration error recorded during the outage")
            if self.outage_state_broke:
                problems.append("left FirmwareCheckRefreshWait during the outage")
        if not self.done:
            problems.append("did not finish")
        return problems


def get_report(admin_cli):
    out = subprocess.run([admin_cli, "site-explorer", "get-report", "all"],
                         capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(f"get-report failed ({out.returncode}): {out.stderr.strip()[:300]}")
    text = out.stdout
    start = text.find("{")
    if start < 0:
        raise RuntimeError("get-report printed no JSON")
    return json.loads(text[start:])


def host_endpoints(report, vendor_match):
    """Host BMC endpoints: vendor matches (DPU BMCs are Nvidia, hosts are not)."""
    found = []
    for ep in report.get("endpoints", []):
        rep = field(ep, "report") or {}
        vendor = (field(rep, "vendor") or "").lower()
        if vendor_match in vendor:
            found.append(ep)
    return found


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--site", help="site dir holding run-admin-cli.sh")
    p.add_argument("--admin-cli", help="override: path to run-admin-cli.sh (default <site>/run-admin-cli.sh)")
    p.add_argument("--scenarios", default="good,blank-version",
                   help="comma list, assigned to host endpoints in order of appearance; "
                        "repeat a name to assign it to several hosts")
    p.add_argument("--assign", action="append", default=[], metavar="IP=SCENARIO",
                   help="pin a scenario to one BMC IP (may repeat)")
    p.add_argument("--vendor", default="wiwynn", help="host BMC vendor substring (lowercase) [wiwynn]")
    p.add_argument("--inventory-ids", default="FW_BMC_0,HGX_FW_CPU_0",
                   help="firmware inventory members with a pre-ingestion minimum")
    p.add_argument("--interval", type=float, default=10, help="poll interval seconds [10]")
    p.add_argument("--outage-seconds", type=int, default=300,
                   help="refresh-fails: how long GETs answer 503 [300]")
    p.add_argument("--timeout", type=int, default=5400, help="give up after this many seconds [5400]")
    p.add_argument("--bmc-user", default=os.environ.get("PP_BMC_USER"),
                   help="only if the mocks require Redfish auth (env PP_BMC_USER)")
    p.add_argument("--bmc-password", default=os.environ.get("PP_BMC_PASSWORD"), help="env PP_BMC_PASSWORD")
    p.add_argument("--log", default="preingest-perturb.log", help="append log here")
    p.add_argument("--dry-run", action="store_true", help="poll and log, never touch a BMC")
    a = p.parse_args()

    admin_cli = a.admin_cli or (os.path.join(os.path.expanduser(a.site), "run-admin-cli.sh") if a.site else None)
    if not admin_cli or not os.access(admin_cli, os.X_OK):
        p.error("--site <dir> with an executable run-admin-cli.sh, or --admin-cli PATH")
    queue = [s.strip() for s in a.scenarios.split(",") if s.strip()]
    pinned = {}
    for item in a.assign:
        ip, _, sc = item.partition("=")
        pinned[ip.strip()] = sc.strip()
    for sc in queue + list(pinned.values()):
        if sc not in SCENARIOS:
            p.error(f"unknown scenario {sc!r}; choose from {', '.join(SCENARIOS)}")
    inventory_ids = [i.strip() for i in a.inventory_ids.split(",") if i.strip()]

    log = Log(a.log)
    log(f"start: admin-cli={admin_cli} scenarios={queue} pinned={pinned} "
        f"members={inventory_ids} outage={a.outage_seconds}s dry_run={a.dry_run}")

    hosts = {}
    deadline = time.monotonic() + a.timeout
    while time.monotonic() < deadline:
        try:
            report = get_report(admin_cli)
        except Exception as e:  # keep polling; the API may be busy
            log(f"poll failed: {e}")
            time.sleep(a.interval)
            continue

        for ep in host_endpoints(report, a.vendor.lower()):
            ip = field(ep, "address")
            if ip not in hosts:
                scenario = pinned.get(ip) or (queue.pop(0) if queue else None)
                if scenario is None:
                    log(f"{ip} appeared; no scenario left, leaving it alone")
                    hosts[ip] = None
                    continue
                h = Host(ip, scenario, Bmc(ip, a.bmc_user, a.bmc_password, log, a.dry_run))
                hosts[ip] = h
                log(f"{ip} appeared -> scenario {scenario}")
                h.arm(inventory_ids, log)
            h = hosts[ip]
            if h is not None and not h.done:
                h.observe(ep, a.outage_seconds, log)

        active = [h for h in hosts.values() if h is not None]
        if active and all(h.done for h in active) and not queue:
            break
        time.sleep(a.interval)

    log("summary:")
    failed = 0
    for h in sorted((h for h in hosts.values() if h is not None), key=lambda h: h.ip):
        problems = h.verdict()
        path = " -> ".join(s for _, s in h.history)
        if problems:
            failed += 1
        log(f"  {h.ip:<16} {h.scenario:<20} {'PASS' if not problems else 'FAIL: ' + '; '.join(problems)}")
        log(f"  {'':<16} {'':<20} {path}")
    if queue:
        log(f"  unassigned scenarios: {queue}")
    sys.exit(1 if failed or queue else 0)


if __name__ == "__main__":
    main()
