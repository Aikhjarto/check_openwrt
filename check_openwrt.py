#!/usr/bin/env python3
# Copyright (C) 2026 Thomas Wagner <wagner-thomas@gmx.at>
# SPDX-License-Identifier: GPL-2.0-or-later
"""Agentless Nagios/Naemon/Icinga plugin for OpenWrt devices.

All checks run over SSH with the system ssh client, so settings from
~/.ssh/config (keys, ControlMaster, ...) apply. Nothing has to be installed
on the device; only tools shipped with a default OpenWrt image are used
(BusyBox, ubus, uci), plus wg for the wireguard check.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

__version__ = "0.2"

OK, WARNING, CRITICAL, UNKNOWN = 0, 1, 2, 3
STATE_NAMES = {OK: "OK", WARNING: "WARNING", CRITICAL: "CRITICAL", UNKNOWN: "UNKNOWN"}


class CheckError(Exception):
    """Raised when a check cannot determine a state (exits UNKNOWN)."""


# --------------------------------------------------------------------------
# Thresholds and output
# --------------------------------------------------------------------------


class Range:
    """Nagios threshold range, see the Monitoring Plugins guidelines.

    "10" alerts outside 0..10, "10:" below 10, "~:10" above 10,
    "10:20" outside 10..20 and "@10:20" inside 10..20.
    """

    def __init__(self, spec: str | None):
        self.spec = spec or ""
        self.start = 0.0
        self.end = float("inf")
        self.inside = False
        spec = self.spec.strip()
        if not spec:
            self.start = float("-inf")
            return
        if spec.startswith("@"):
            self.inside = True
            spec = spec[1:]
        try:
            if ":" in spec:
                start, end = spec.split(":", 1)
                self.start = float("-inf") if start == "~" else float(start or 0)
                self.end = float(end) if end else float("inf")
            else:
                self.end = float(spec)
        except ValueError:
            raise argparse.ArgumentTypeError(f"invalid threshold range '{self.spec}'")
        if self.start > self.end:
            raise argparse.ArgumentTypeError(f"invalid threshold range '{self.spec}'")

    def __bool__(self) -> bool:
        return bool(self.spec)

    def __str__(self) -> str:
        return self.spec

    def alert(self, value: float) -> bool:
        if not self.spec:
            return False
        inside = self.start <= value <= self.end
        return inside if self.inside else not inside


def evaluate(value: float, warning: Range, critical: Range) -> int:
    if critical.alert(value):
        return CRITICAL
    if warning.alert(value):
        return WARNING
    return OK


def worst(*states: int) -> int:
    """Combine states; CRITICAL beats WARNING beats UNKNOWN beats OK."""
    order = [OK, UNKNOWN, WARNING, CRITICAL]
    return max(states, key=order.index, default=OK)


def perf_label(name: str) -> str:
    name = name.replace("'", "_").replace("=", "_")
    return f"'{name}'" if re.search(r"\s", name) else name


def fmt_num(value: float) -> str:
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value)


class Result:
    def __init__(self) -> None:
        self.state = OK
        self.summary = ""
        self.details: list[str] = []
        self.perfdata: list[str] = []

    def set(self, state: int) -> None:
        self.state = worst(self.state, state)

    def perf(self, label, value, uom="", warning="", critical="", minimum="", maximum="") -> None:
        fields = [f"{fmt_num(value)}{uom}", str(warning), str(critical),
                  fmt_num(minimum) if minimum != "" else "",
                  fmt_num(maximum) if maximum != "" else ""]
        self.perfdata.append(f"{perf_label(label)}=" + ";".join(fields).rstrip(";"))

    def output(self, check: str) -> str:
        line = f"OPENWRT {check.upper()} {STATE_NAMES[self.state]} - {self.summary}"
        if self.perfdata:
            line += " | " + " ".join(self.perfdata)
        return "\n".join([line] + self.details)


def fmt_bytes(num: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(num) < 1024 or unit == "GiB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return str(num)


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def sections(output: str) -> dict[str, str]:
    """Split remote output at lines '@@name@@' into a dict of sections."""
    result: dict[str, str] = {}
    name = ""
    lines: list[str] = []
    for line in output.splitlines():
        match = re.fullmatch(r"@@(.+)@@", line.strip())
        if match:
            result[name] = "\n".join(lines)
            name, lines = match.group(1), []
        else:
            lines.append(line)
    result[name] = "\n".join(lines)
    return result


def load_json(text: str, what: str):
    try:
        return json.loads(text)
    except ValueError:
        raise CheckError(f"could not parse {what}")


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


# --------------------------------------------------------------------------
# Checks: each one has a remote shell script and an evaluation function
# --------------------------------------------------------------------------


def script_mem(args) -> str:
    return "cat /proc/meminfo"


def check_mem(out: str, args) -> Result:
    info = {}
    for line in out.splitlines():
        match = re.match(r"(\w+):\s+(\d+)", line)
        if match:
            info[match.group(1)] = int(match.group(2)) * 1024
    if "MemTotal" not in info:
        raise CheckError("could not parse /proc/meminfo")
    total = info["MemTotal"]
    available = info.get("MemAvailable",
                         info.get("MemFree", 0) + info.get("Buffers", 0) + info.get("Cached", 0))
    used = total - available
    pct = 100.0 * used / total
    res = Result()
    res.set(evaluate(pct, args.warning, args.critical))
    res.summary = f"{pct:.1f}% used ({fmt_bytes(used)} of {fmt_bytes(total)})"
    res.perf("used_pct", round(pct, 2), "%", args.warning, args.critical, 0, 100)
    res.perf("used", used, "B", "", "", 0, total)
    return res


def script_disk(args) -> str:
    if args.path:
        return "df -kP " + " ".join(shell_quote(p) for p in args.path)
    return "df -kP /overlay 2>/dev/null || df -kP /"


def check_disk(out: str, args) -> Result:
    res = Result()
    texts = []
    rows = [line.split() for line in out.splitlines()[1:] if line.strip()]
    if not rows:
        raise CheckError("no filesystems found")
    for row in rows:
        if len(row) < 6 or not row[1].isdigit():
            raise CheckError(f"could not parse df output: {' '.join(row)}")
        used, avail, mount = int(row[2]) * 1024, int(row[3]) * 1024, " ".join(row[5:])
        size = used + avail
        pct = 100.0 * used / size if size else 0.0
        state = evaluate(pct, args.warning, args.critical)
        res.set(state)
        text = f"{mount} {pct:.1f}% used ({fmt_bytes(avail)} free)"
        texts.append(text)
        res.details.append(f"[{STATE_NAMES[state]}] {text}, size {fmt_bytes(size)}")
        res.perf(f"{mount}_pct", round(pct, 2), "%", args.warning, args.critical, 0, 100)
        res.perf(mount, used, "B", "", "", 0, size)
    res.summary = ", ".join(texts)
    return res


def script_load(args) -> str:
    return "cat /proc/loadavg; grep -c ^processor /proc/cpuinfo"


def parse_triplet(value: str) -> list[Range]:
    parts = value.split(",")
    if len(parts) == 1:
        parts *= 3
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("expected one or three comma separated thresholds")
    return [Range(p) for p in parts]


def check_load(out: str, args) -> Result:
    lines = out.split()
    try:
        loads = [float(v) for v in lines[:3]]
        cpus = int(lines[-1]) if len(lines) > 5 else 1
    except (ValueError, IndexError):
        raise CheckError("could not parse /proc/loadavg")
    res = Result()
    values = [v / max(cpus, 1) for v in loads] if args.percpu else loads
    for name, value, warn, crit in zip(("load1", "load5", "load15"), values,
                                       args.warning, args.critical):
        res.set(evaluate(value, warn, crit))
        res.perf(name, round(value, 2), "", warn, crit, 0)
    kind = f" per CPU ({cpus} CPUs)" if args.percpu else ""
    res.summary = "load average" + kind + ": " + ", ".join(f"{v:.2f}" for v in values)
    return res


def script_uptime(args) -> str:
    return "cat /proc/uptime"


def check_uptime(out: str, args) -> Result:
    try:
        uptime = float(out.split()[0])
    except (ValueError, IndexError):
        raise CheckError("could not parse /proc/uptime")
    res = Result()
    res.set(evaluate(uptime, args.warning, args.critical))
    res.summary = f"up {fmt_duration(uptime)}"
    if res.state != OK:
        res.summary += " (recently rebooted)"
    res.perf("uptime", int(uptime), "s", args.warning, args.critical, 0)
    return res


def script_conntrack(args) -> str:
    return ("cat /proc/sys/net/netfilter/nf_conntrack_count "
            "/proc/sys/net/netfilter/nf_conntrack_max")


def check_conntrack(out: str, args) -> Result:
    try:
        count, maximum = (int(v) for v in out.split()[:2])
    except ValueError:
        raise CheckError("could not read nf_conntrack_count/nf_conntrack_max")
    pct = 100.0 * count / maximum if maximum else 0.0
    res = Result()
    res.set(evaluate(pct, args.warning, args.critical))
    res.summary = f"{count} of {maximum} connections tracked ({pct:.1f}%)"
    res.perf("conntrack_pct", round(pct, 2), "%", args.warning, args.critical, 0, 100)
    res.perf("conntrack", count, "", "", "", 0, maximum)
    return res


def script_iface(args) -> str:
    return "ubus call network.interface dump"


def check_iface(out: str, args) -> Result:
    data = load_json(out, "ubus network.interface dump")
    interfaces = {i.get("interface"): i for i in data.get("interface", [])}
    if args.interface:
        names = args.interface
    else:
        names = [n for n, i in interfaces.items() if i.get("autostart")]
    names = [n for n in names if n not in (args.exclude or [])]
    if not names:
        raise CheckError("no interfaces to check")
    res = Result()
    problems = []
    up = 0
    for name in names:
        iface = interfaces.get(name)
        if iface is None:
            state, text = CRITICAL, f"{name} not found"
        elif iface.get("up"):
            up += 1
            state = OK
            text = f"{name} up ({iface.get('proto', '?')}, {fmt_duration(iface.get('uptime', 0))})"
        else:
            state = WARNING if iface.get("pending") else CRITICAL
            errors = ", ".join(e.get("code", "?") for e in iface.get("errors", []))
            text = f"{name} {'pending' if iface.get('pending') else 'down'}"
            text += f" ({errors})" if errors else ""
        res.set(state)
        if state != OK:
            problems.append(text)
        res.details.append(f"[{STATE_NAMES[state]}] {text}")
    res.summary = ", ".join(problems) if problems else f"all {len(names)} interfaces up"
    res.perf("up", up, "", "", "", 0, len(names))
    return res


def script_wifi(args) -> str:
    return ("ubus call network.wireless status\n"
            "echo @@clients@@\n"
            "for o in $(ubus list 'hostapd.*' 2>/dev/null); do\n"
            "  echo \"@@$o@@\"; ubus call \"$o\" get_clients 2>/dev/null\n"
            "done")


def check_wifi(out: str, args) -> Result:
    parts = sections(out)
    radios = load_json(parts.get("", ""), "ubus network.wireless status")
    clients = {}
    for name, text in parts.items():
        if name.startswith("hostapd.") and text.strip():
            clients[name[len("hostapd."):]] = len(load_json(text, name).get("clients", {}))
    names = args.radio or sorted(radios)
    names = [n for n in names if n not in (args.exclude or [])]
    if not names:
        raise CheckError("no wireless radios found")
    res = Result()
    problems = []
    active = disabled = total_clients = 0
    for name in names:
        radio = radios.get(name)
        iface_lines = []
        if radio is None:
            state, text = CRITICAL, f"{name} not found"
            problems.append(text)
        elif radio.get("disabled"):
            disabled += 1
            state, text = OK, f"{name} disabled"
        elif radio.get("up"):
            active += 1
            state = OK
            band = radio.get("config", {}).get("band", "")
            channel = radio.get("config", {}).get("channel", "")
            text = f"{name} up ({band}, channel {channel})"
            for iface in radio.get("interfaces", []):
                ifname = iface.get("ifname")
                ssid = iface.get("config", {}).get("ssid", iface.get("section", "?"))
                if not ifname:
                    state = WARNING
                    problems.append(f"{name}: '{ssid}' has no interface")
                    iface_lines.append(f"  '{ssid}': no interface")
                elif ifname in clients:
                    total_clients += clients[ifname]
                    iface_lines.append(f"  {ifname} '{ssid}': {clients[ifname]} clients")
                    res.perf(f"clients_{ifname}", clients[ifname], "", "", "", 0)
        else:
            state = WARNING if radio.get("pending") else CRITICAL
            text = f"{name} {'pending' if radio.get('pending') else 'down'}"
            problems.append(text)
        res.set(state)
        res.details.append(f"[{STATE_NAMES[state]}] {text}")
        res.details.extend(iface_lines)
    if problems:
        res.summary = ", ".join(problems)
    else:
        res.summary = f"{active} radios up, {disabled} disabled, {total_clients} clients"
    res.perf("clients", total_clients, "", "", "", 0)
    return res


def script_wireguard(args) -> str:
    return ("command -v wg >/dev/null 2>&1 || { echo @@nowg@@; exit 0; }\n"
            "date +%s\n"
            "echo @@dump@@\n"
            "wg show all dump\n"
            "echo @@uci@@\n"
            r"uci -q show network | grep -E '\.(public_key|description)='")


def parse_uci_peers(text: str) -> dict[str, str]:
    """Map peer public keys to their uci description."""
    sections_: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        match = re.match(r"([^=]+)\.(public_key|description)='(.*)'$", line)
        if match:
            value = match.group(3).replace("'\\''", "'")
            sections_.setdefault(match.group(1), {})[match.group(2)] = value
    return {s["public_key"]: s.get("description", "") for s in sections_.values()
            if "public_key" in s}


def check_wireguard(out: str, args) -> Result:
    parts = sections(out)
    if "nowg" in parts:
        raise CheckError("wg not found on device (install wireguard-tools)")
    try:
        now = int(parts[""].split()[0])
    except (KeyError, ValueError, IndexError):
        raise CheckError("could not read device time")
    descriptions = parse_uci_peers(parts.get("uci", ""))
    peers = []
    interfaces = set()
    for line in parts.get("dump", "").splitlines():
        fields = line.split("\t")
        if len(fields) == 5:
            interfaces.add(fields[0])
        elif len(fields) == 9:
            if args.interface and fields[0] not in args.interface:
                continue
            peers.append({"iface": fields[0], "key": fields[1], "endpoint": fields[3],
                          "handshake": int(fields[5]), "rx": int(fields[6]),
                          "tx": int(fields[7]),
                          "name": descriptions.get(fields[1]) or fields[1][:8]})
    res = Result()
    for iface in args.interface or []:
        if iface not in interfaces:
            res.set(CRITICAL)
            res.details.append(f"[CRITICAL] interface {iface} not found")
    if not interfaces:
        raise CheckError("no WireGuard interfaces found")
    wanted = set(args.peer or [])
    found = set()
    problems = []
    for peer in peers:
        checked = args.all_peers or peer["key"] in wanted or peer["name"] in wanted
        found.update({peer["key"], peer["name"]} & wanted)
        if peer["handshake"]:
            age = now - peer["handshake"]
            text = f"{peer['name']}: last handshake {fmt_duration(age)} ago"
            state = evaluate(age, args.warning, args.critical) if checked else OK
            res.perf(f"{peer['name']}_handshake", age, "s",
                     args.warning if checked else "", args.critical if checked else "", 0)
        else:
            text = f"{peer['name']}: no handshake"
            state = CRITICAL if checked else OK
        res.perf(f"{peer['name']}_rx", peer["rx"], "c")
        res.perf(f"{peer['name']}_tx", peer["tx"], "c")
        res.set(state)
        if state != OK:
            problems.append(text)
        mark = STATE_NAMES[state] if checked else "info"
        res.details.append(f"[{mark}] {text} ({peer['iface']}, endpoint {peer['endpoint']})")
    for missing in sorted(wanted - found):
        res.set(CRITICAL)
        problems.append(f"peer {missing} not found")
    if problems:
        res.summary = ", ".join(problems)
    else:
        connected = sum(1 for p in peers if p["handshake"]
                        and now - p["handshake"] < 180)
        res.summary = (f"{len(interfaces)} interfaces, {len(peers)} peers, "
                       f"{connected} with recent handshake")
    return res


def script_services(args) -> str:
    return "ubus call service list"


def check_services(out: str, args) -> Result:
    services = load_json(out, "ubus service list")
    res = Result()
    problems = []
    running = 0
    for name in args.service or []:
        if not services.get(name, {}).get("instances"):
            res.set(CRITICAL)
            problems.append(f"{name} not found")
    for name, service in sorted(services.items()):
        if name in (args.exclude or []):
            continue
        for inst_name, inst in sorted(service.get("instances", {}).items()):
            label = name if inst_name in ("instance1", name) else f"{name}/{inst_name}"
            if inst.get("running"):
                running += 1
                continue
            exit_code = inst.get("exit_code")
            if "respawn" not in inst and exit_code == 0:
                continue  # one-shot job that finished successfully
            state = CRITICAL if "respawn" in inst or name in (args.service or []) else WARNING
            res.set(state)
            text = f"{label} not running"
            text += f" (exit code {exit_code})" if exit_code is not None else ""
            problems.append(text)
            res.details.append(f"[{STATE_NAMES[state]}] {text}")
    res.summary = ", ".join(problems) if problems else f"{running} service instances running"
    res.perf("running", running, "", "", "", 0)
    return res


def script_updates(args) -> str:
    refresh_apk = "" if args.no_refresh else "apk update >/dev/null 2>&1; echo \"@@refresh $?@@\"\n"
    refresh_opkg = "" if args.no_refresh else "opkg update >/dev/null 2>&1; echo \"@@refresh $?@@\"\n"
    return ("if command -v apk >/dev/null 2>&1; then\n"
            "echo @@apk@@\n" + refresh_apk +
            "echo @@list@@\napk list --upgradable 2>/dev/null\n"
            "elif command -v opkg >/dev/null 2>&1; then\n"
            "echo @@opkg@@\n" + refresh_opkg +
            "echo \"@@lists $(ls /var/opkg-lists 2>/dev/null | wc -l)@@\"\n"
            "echo @@list@@\nopkg list-upgradable 2>/dev/null\n"
            "else echo @@nopm@@; fi")


def check_updates(out: str, args) -> Result:
    parts = sections(out)
    if "nopm" in parts:
        raise CheckError("neither apk nor opkg found")
    for name in parts:
        if name.startswith("refresh ") and name != "refresh 0":
            raise CheckError("refreshing the package lists failed")
        if name == "lists 0":
            raise CheckError("no package lists available, run without --no-refresh")
    packages = [line.split()[0] for line in parts.get("list", "").splitlines() if line.strip()]
    res = Result()
    res.set(evaluate(len(packages), args.warning, args.critical))
    manager = "apk" if "apk" in parts else "opkg"
    if packages:
        res.summary = f"{len(packages)} package updates available ({manager})"
        res.details.extend(f"  {p}" for p in packages)
    else:
        res.summary = f"no package updates available ({manager})"
    res.perf("updates", len(packages), "", args.warning, args.critical, 0)
    return res


CHECKS = {
    "mem": (script_mem, check_mem, "memory usage in percent", "80", "90"),
    "disk": (script_disk, check_disk, "disk usage in percent (default /overlay)", "80", "90"),
    "load": (script_load, check_load, "load average (1, 5, 15 minutes)", "4,3,2", "8,6,4"),
    "uptime": (script_uptime, check_uptime, "uptime in seconds, detects reboots", "3600:", ""),
    "conntrack": (script_conntrack, check_conntrack, "connection tracking table usage in percent",
                  "75", "90"),
    "iface": (script_iface, check_iface, "network interfaces (netifd) are up", "", ""),
    "wifi": (script_wifi, check_wifi, "wireless radios are up, counts clients", "", ""),
    "wireguard": (script_wireguard, check_wireguard, "WireGuard peer handshake age in seconds",
                  "300", "900"),
    "services": (script_services, check_services, "procd service instances are running", "", ""),
    "updates": (script_updates, check_updates, "number of upgradable packages", "0", ""),
}


# --------------------------------------------------------------------------
# SSH and command line
# --------------------------------------------------------------------------


def run_ssh(args, script: str) -> str:
    cmd = [args.ssh, "-o", "BatchMode=yes", "-o", f"ConnectTimeout={min(args.timeout, 30)}"]
    if args.port:
        cmd += ["-p", str(args.port)]
    if args.identity:
        cmd += ["-i", args.identity]
    for option in args.ssh_option or []:
        cmd += ["-o", option]
    cmd += ["-l", args.user, args.hostname, script]
    try:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=args.timeout,
                              universal_newlines=True)
    except subprocess.TimeoutExpired:
        raise CheckError(f"timeout after {args.timeout}s")
    except OSError as err:
        raise CheckError(f"could not run {args.ssh}: {err.strerror}")
    if proc.returncode == 255:
        lines = proc.stderr.strip().splitlines()
        raise CheckError("ssh failed: " + (lines[-1] if lines else "unknown error"))
    if proc.returncode != 0 and not proc.stdout.strip():
        lines = proc.stderr.strip().splitlines()
        raise CheckError(f"remote command failed (exit code {proc.returncode})"
                         + (f": {lines[-1]}" if lines else ""))
    return proc.stdout


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_openwrt",
        description="Agentless monitoring plugin for OpenWrt devices via SSH.")
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-H", "--hostname", required=True, help="device to connect to")
    parser.add_argument("-l", "--user", default="root", help="SSH user (default: root)")
    parser.add_argument("-p", "--port", type=int, help="SSH port")
    parser.add_argument("-i", "--identity", help="SSH private key file")
    parser.add_argument("-o", "--ssh-option", action="append", metavar="OPTION",
                        help="extra ssh -o option, can be given multiple times")
    parser.add_argument("-t", "--timeout", type=int,
                        help="timeout in seconds (default: 10, 60 for updates)")
    parser.add_argument("--ssh", default="ssh", help="ssh binary (default: ssh)")
    sub = parser.add_subparsers(dest="check", metavar="CHECK")
    sub.required = True
    for name, (_, _, help_text, warning, critical) in CHECKS.items():
        p = sub.add_parser(name, help=help_text, description=help_text)
        rtype = parse_triplet if name == "load" else Range
        if warning or critical or name in ("uptime",):
            p.add_argument("-w", "--warning", type=rtype, default=rtype(warning),
                           help=f"warning threshold range (default: {warning or 'none'})")
            p.add_argument("-c", "--critical", type=rtype, default=rtype(critical),
                           help=f"critical threshold range (default: {critical or 'none'})")
        if name == "load":
            p.add_argument("-r", "--percpu", action="store_true",
                           help="divide the load averages by the number of CPUs")
        elif name == "disk":
            p.add_argument("-P", "--path", action="append",
                           help="mount point to check, can be given multiple times")
        elif name == "iface":
            p.add_argument("-I", "--interface", action="append",
                           help="interface to check (default: all with autostart)")
            p.add_argument("-x", "--exclude", action="append", help="interface to skip")
        elif name == "wifi":
            p.add_argument("-R", "--radio", action="append",
                           help="radio to check (default: all)")
            p.add_argument("-x", "--exclude", action="append", help="radio to skip")
        elif name == "wireguard":
            p.add_argument("-I", "--interface", action="append",
                           help="WireGuard interface to check (default: all)")
            p.add_argument("-P", "--peer", action="append",
                           help="peer public key or uci description whose handshake age "
                                "is checked, can be given multiple times")
            p.add_argument("-a", "--all-peers", action="store_true",
                           help="check the handshake age of all peers")
        elif name == "services":
            p.add_argument("-s", "--service", action="append",
                           help="service that must exist and run")
            p.add_argument("-x", "--exclude", action="append", help="service to skip")
        elif name == "updates":
            p.add_argument("-n", "--no-refresh", action="store_true",
                           help="do not refresh the package lists before checking")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.timeout is None:
        args.timeout = 60 if args.check == "updates" else 10
    script, check = CHECKS[args.check][:2]
    try:
        result = check(run_ssh(args, script(args)), args)
    except CheckError as err:
        result = Result()
        result.state = UNKNOWN
        result.summary = str(err)
    except Exception as err:  # never exit with a traceback and code 1
        result = Result()
        result.state = UNKNOWN
        result.summary = f"internal error: {type(err).__name__}: {err}"
    print(result.output(args.check))
    return result.state


if __name__ == "__main__":
    sys.exit(main())
