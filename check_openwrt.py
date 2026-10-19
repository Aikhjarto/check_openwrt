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
import datetime
import json
import re
import subprocess
import sys

__version__ = "0.5.2"

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
    # one section per path: "@@df N@@" + df output (or nothing if it fails)
    if args.path:
        df = "\n".join(f"echo @@df {i}@@; df -kP {shell_quote(p)} 2>/dev/null | tail -n +2"
                       for i, p in enumerate(args.path))
    else:
        df = "echo @@df 0@@; { df -kP /overlay 2>/dev/null || df -kP /; } | tail -n +2"
    return df + "\necho @@mounts@@\ncat /proc/mounts"


def mount_options(mounts: str) -> dict[str, list[str]]:
    """Mount options per mount point from /proc/mounts (last entry wins)."""
    options = {}
    for line in mounts.splitlines():
        fields = line.split()
        if len(fields) >= 4:
            point = fields[1].replace("\\040", " ")
            options[point] = fields[3].split(",")
    return options


def check_disk(out: str, args) -> Result:
    parts = sections(out)
    options = mount_options(parts.get("mounts", ""))
    res = Result()
    texts = []
    problems = []
    paths = args.path or [None]
    for index, wanted in enumerate(paths):
        lines = [line for line in parts.get(f"df {index}", "").splitlines() if line.strip()]
        if not lines:
            if wanted is None:
                raise CheckError("no filesystems found")
            res.set(CRITICAL)
            text = f"{wanted} does not exist"
            problems.append(text)
            res.details.append(f"[CRITICAL] {text}")
            continue
        row = lines[0].split()
        if len(row) < 6 or not row[1].isdigit():
            raise CheckError(f"could not parse df output: {' '.join(row)}")
        used, avail, mount = int(row[2]) * 1024, int(row[3]) * 1024, " ".join(row[5:])
        if wanted and wanted.rstrip("/") != mount.rstrip("/") and wanted != mount:
            res.set(CRITICAL)
            text = f"{wanted} is not mounted (it is part of {mount})"
            problems.append(text)
            res.details.append(f"[CRITICAL] {text}")
            continue
        size = used + avail
        pct = 100.0 * used / size if size else 0.0
        state = evaluate(pct, args.warning, args.critical)
        text = f"{mount} {pct:.1f}% used ({fmt_bytes(avail)} free)"
        if "ro" in options.get(mount, []):
            state = CRITICAL
            text += ", mounted read-only"
        res.set(state)
        texts.append(text)
        if state != OK:
            problems.append(text)
        res.details.append(f"[{STATE_NAMES[state]}] {text}, size {fmt_bytes(size)}")
        res.perf(f"{mount}_pct", round(pct, 2), "%", args.warning, args.critical, 0, 100)
        res.perf(mount, used, "B", "", "", 0, size)
    res.summary = ", ".join(problems) if problems else ", ".join(texts)
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


def parse_ping_thresholds(value: str) -> list[Range]:
    """check_ping style thresholds "RTA,LOSS%", e.g. "200,20%"."""
    parts = value.split(",")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("expected round trip average and packet loss, "
                                         "e.g. 200,20%")
    return [Range(parts[0]), Range(parts[1].rstrip("%"))]


def inet_timeout(args) -> int:
    """Upper bound for the run time of all enabled inet tests."""
    timeout = 5
    if not args.no_dns:
        timeout += 10
    if not args.no_ping:
        timeout += args.packets + 2
    if not args.no_http:
        timeout += args.http_timeout
    return timeout


def script_inet(args) -> str:
    if args.no_dns and args.no_ping and args.no_http:
        raise CheckError("all tests are disabled")
    if args.packets < 1:
        raise CheckError("--packets must be at least 1")
    # /proc/uptime has a resolution of 10 ms, BusyBox date only full seconds
    lines = ["up() { cut -d' ' -f1 /proc/uptime; }"]
    if not args.no_dns:
        server = " " + shell_quote(args.dns_server) if args.dns_server else ""
        lines.append("echo @@dns@@; t=$(up); nslookup -timeout=5 -retry=1 "
                     f"{shell_quote(args.dns_name)}{server} 2>&1; "
                     "echo \"@@dns-rc $? $t $(up)@@\"")
    if not args.no_ping:
        lines.append(f"echo @@ping@@; ping -q -c {args.packets} -W 1 -w {args.packets + 2} "
                     f"{shell_quote(args.address)} 2>&1; echo \"@@ping-rc $?@@\"")
    if not args.no_http:
        lines.append(f"echo @@http@@; t=$(up); uclient-fetch -T {args.http_timeout} "
                     f"-O /dev/null {shell_quote(args.url)} 2>&1; "
                     "echo \"@@http-rc $? $t $(up)@@\"")
    return "\n".join(lines)


def inet_result(parts: dict[str, str], name: str) -> tuple[str, int, float]:
    """Output, exit code and run time of one inet test."""
    for key in parts:
        fields = key.split()
        if fields and fields[0] == f"{name}-rc":
            try:
                code = int(fields[1])
                duration = float(fields[3]) - float(fields[2]) if len(fields) == 4 else 0.0
            except (ValueError, IndexError):
                break
            return parts.get(name, ""), code, max(duration, 0.0)
    raise CheckError(f"no result of the {name} test")


def error_lines(text: str) -> str:
    lines = [line.strip().lstrip(";").strip() for line in text.splitlines() if line.strip()]
    errors = [line for line in lines if re.search(r"error|fail|can't|bad address", line, re.I)]
    unique = list(dict.fromkeys(errors))
    return "; ".join(unique) or (lines[-1] if lines else "no output")


def check_inet(out: str, args) -> Result:
    parts = sections(out)
    res = Result()
    problems = []
    oks = []

    def report(state: int, text: str, detail: str = "") -> None:
        res.set(state)
        (oks if state == OK else problems).append(text)
        res.details.append(f"[{STATE_NAMES[state]}] {detail or text}")

    # HTTP is evaluated first: a failing ping is only a warning if HTTP works
    http = None
    if not args.no_http:
        text, code, duration = inet_result(parts, "http")
        kind = "HTTPS" if args.url.lower().startswith("https:") else "HTTP"
        if code == 0:
            http = (OK, f"{kind} {duration:.2f} s",
                    f"{kind} {args.url} fetched in {duration:.2f} s")
            res.perf("http_time", round(duration, 2), "s", "", "", 0)
        else:
            reason = error_lines(text)
            http = (CRITICAL, f"{kind} failed: {reason}", f"{kind} {args.url} failed: {reason}")

    if not args.no_dns:
        text, code, duration = inet_result(parts, "dns")
        _, _, answer = text.partition("Name:")
        addresses = list(dict.fromkeys(
            re.findall(r"^Address(?: \d+)?:\s+(\S+)\s*$", answer, re.M)))
        if code == 0 and addresses:
            report(OK, f"DNS {duration:.2f} s",
                   f"DNS {args.dns_name}: {', '.join(addresses)} ({duration:.2f} s)")
            res.perf("dns_time", round(duration, 2), "s", "", "", 0)
        else:
            reason = error_lines(text) if code else "no addresses"
            reason = re.sub(r"\*\* server can't find [^:]+: ", "", reason)
            report(CRITICAL, f"DNS lookup of {args.dns_name} failed: {reason}")

    if not args.no_ping:
        text, code, _ = inet_result(parts, "ping")
        match = re.search(r"(\d+) packets transmitted, (\d+) packets received", text)
        if not match:
            report(CRITICAL, f"ping {args.address} failed: {error_lines(text)}")
        else:
            sent, received = int(match.group(1)), int(match.group(2))
            loss = 100.0 * (sent - received) / sent if sent else 100.0
            warn_rta, warn_loss = args.warning
            crit_rta, crit_loss = args.critical
            state = evaluate(loss, warn_loss, crit_loss)
            summary = f"ping {args.address} {loss:.0f}% loss"
            rtt = re.search(r"min/avg/max = [\d.]+/([\d.]+)/", text)
            if rtt:
                rta = float(rtt.group(1))
                state = worst(state, evaluate(rta, warn_rta, crit_rta))
                summary += f", rta {rta:.1f} ms"
                res.perf("rta", rta, "ms", warn_rta, crit_rta, 0)
            res.perf("pl", round(loss), "%", warn_loss, crit_loss, 0, 100)
            if state == CRITICAL and http and http[0] == OK:
                state = WARNING  # ICMP may be blocked while the internet works
                summary += " (HTTP works)"
            report(state, summary)

    if http:
        report(*http)
    res.summary = ", ".join(problems) if problems else "internet access: " + ", ".join(oks)
    return res


def script_banip(args) -> str:
    # newer banIP (1.9 in OpenWrt 25.12) keeps it in /var/run/banIP/, 1.5 in /var/run/
    return ("cat /var/run/banIP/banIP.runtime.json 2>/dev/null"
            " || cat /var/run/banip_runtime.json 2>/dev/null || echo '{}'\n"
            "echo @@now@@\n"
            "date '+%Y-%m-%d %H:%M:%S'\n"
            "echo @@sets@@\n"
            "nft -j list sets table inet banIP 2>/dev/null\n"
            "echo @@rules@@\n"
            "for c in _inbound _outbound; do nft list chain inet banIP $c 2>/dev/null; done")


def check_banip(out: str, args) -> Result:
    parts = sections(out)
    runtime = load_json(parts.get("", "").strip() or "{}", "banIP runtime file")
    if not runtime:
        raise CheckError("banIP is not installed or has not run (no runtime file)")
    sets = load_json(parts.get("sets", "").strip() or '{"nftables": []}', "nft set list")
    counts = {}
    for item in sets.get("nftables", []):
        nft_set = item.get("set")
        if nft_set:
            counts[nft_set.get("name", "?")] = len(nft_set.get("elem", []))
    drops: dict[str, int] = {}
    for name, packets, action in re.findall(r"@([\w.-]+) counter packets (\d+) bytes \d+ (\S+)",
                                            parts.get("rules", "")):
        if action != "accept":
            drops[name] = drops.get(name, 0) + int(packets)
    res = Result()
    status = runtime.get("status", "unknown")
    if status != "active":
        res.set(CRITICAL)
    blocking = {name: count for name, count in counts.items()
                if not name.startswith("allowlist")}
    total = sum(blocking.values())
    res.set(evaluate(total, args.warning, args.critical))
    age = None
    match = re.search(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", runtime.get("last_run", ""))
    try:
        now = datetime.datetime.strptime(parts.get("now", "").strip(), "%Y-%m-%d %H:%M:%S")
        age = (now - datetime.datetime.strptime(match.group(0), "%Y-%m-%d %H:%M:%S")).total_seconds()
    except (AttributeError, ValueError):
        pass
    if age is None:
        res.set(WARNING)
        run = "last run unknown"
    else:
        run = f"last run {fmt_duration(age)} ago"
        if args.max_age and age > args.max_age:
            res.set(WARNING)
            run += f" (more than {fmt_duration(args.max_age)})"
    feeds = sum(1 for count in blocking.values() if count)
    res.summary = (f"banIP {status}, {total} block entries in {feeds} "
                   f"{'list' if feeds == 1 else 'lists'}, {run}")
    res.perf("block_entries", total, "", args.warning, args.critical, 0)
    for name in sorted(counts):
        text = f"{name}: {counts[name]} entries"
        if name in drops:
            text += f", {drops[name]} packets blocked"
            res.perf(f"{name}_blocked", drops[name], "c")
        res.details.append(f"  {text}")
        res.perf(name, counts[name], "", "", "", 0)
    if age is not None:
        res.perf("last_run_age", int(age), "s", args.max_age or "", "", 0)
    return res


def script_temp(args) -> str:
    # one line per sensor: zone|type|millidegrees|critical trip, or
    # hwmon|name|sensor|label|millidegrees|critical; ERR = not readable
    return (r"""for z in /sys/class/thermal/thermal_zone*; do
  [ -e "$z/temp" ] || continue
  crit=
  for t in "$z"/trip_point_*_type; do
    [ -e "$t" ] && [ "$(cat "$t")" = critical ] && crit=$(cat "${t%_type}_temp")
  done
  v=$(cat "$z/temp" 2>/dev/null) || v=ERR
  echo "zone|$(cat "$z/type")|${v:-ERR}|$crit"
done
for h in /sys/class/hwmon/hwmon*; do
  case "$(readlink -f "$h/device" 2>/dev/null)" in *thermal_zone*) continue;; esac
  for t in "$h"/temp*_input; do
    [ -e "$t" ] || continue
    b=${t%_input}
    v=$(cat "$t" 2>/dev/null) || v=ERR
    echo "hwmon|$(cat "$h/name" 2>/dev/null)|${b##*/}|$(cat "${b}_label" 2>/dev/null)|${v:-ERR}|$(cat "${b}_crit" 2>/dev/null)"
  done
done""")


def sensor_name(kind: str, name: str) -> tuple[str, str]:
    """Readable name and performance data label of a temperature sensor."""
    if kind == "zone":
        base = name[:-len("-thermal")] if name.endswith("-thermal") else name
        return base, base
    match = re.fullmatch(r"(\w+?)_(phy\d+)", name)
    if match:
        return f"wifi {match.group(2)} ({match.group(1)})", f"wifi_{match.group(2)}"
    match = re.fullmatch(r"(\w+?)_hwmon", name)
    if match:
        return f"wifi ({match.group(1)})", f"wifi_{match.group(1)}"
    match = re.fullmatch(r"mdio[-_]bus:(\w+)", name)
    if match:
        return f"ethernet phy {match.group(1)}", f"eth_phy_{match.group(1)}"
    return name, re.sub(r"[^\w.-]", "_", name)


def check_temp(out: str, args) -> Result:
    sensors = []
    skipped = []
    for line in out.splitlines():
        fields = line.split("|")
        if fields[0] == "zone" and len(fields) == 4:
            name, label = sensor_name("zone", fields[1])
            raw, crit = fields[2], fields[3]
        elif fields[0] == "hwmon" and len(fields) == 6:
            name, label = sensor_name("hwmon", fields[1])
            if fields[2] != "temp1" or fields[3]:
                extra = fields[3] or fields[2]
                # no backslash in the f-string expression, a SyntaxError before Python 3.12
                suffix = re.sub(r"[^\w.-]", "_", extra)
                name, label = f"{name} {extra}", f"{label}_{suffix}"
            raw, crit = fields[4], fields[5]
        else:
            continue
        if args.exclude and label in args.exclude:
            continue
        if not raw.lstrip("-").isdigit():
            skipped.append(name)
            continue
        sensors.append((name, label, int(raw) / 1000,
                        int(crit) / 1000 if crit.lstrip("-").isdigit() else None))
    if not sensors:
        raise CheckError("no readable temperature sensors"
                         + (f" ({', '.join(skipped)} not readable)" if skipped else ""))
    res = Result()
    problems = []
    for name, label, value, crit in sensors:
        state = evaluate(value, args.warning, args.critical)
        res.set(state)
        text = f"{name} {value:.1f} °C"
        if state != OK:
            problems.append(text)
        limit = f" (device limit {crit:.0f} °C)" if crit else ""
        res.details.append(f"[{STATE_NAMES[state]}] {text}{limit}")
        res.perf(label, round(value, 1), "", args.warning, args.critical)
    for name in skipped:
        res.details.append(f"[info] {name} not readable")
    hottest = max(sensors, key=lambda s: s[2])
    res.summary = (", ".join(problems) if problems else
                   f"hottest {hottest[0]} {hottest[2]:.1f} °C ({len(sensors)} "
                   f"{'sensor' if len(sensors) == 1 else 'sensors'})")
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
    "inet": (script_inet, check_inet, "internet access from the device: DNS, ping and HTTP(S)",
             "200,20%", "500,60%"),
    "banip": (script_banip, check_banip, "banIP status, number of block list entries and last run",
              "", "1:"),
    "temp": (script_temp, check_temp, "temperatures of CPU, Wi-Fi and Ethernet chips in degrees Celsius",
             "80", "95"),
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
                        help="timeout in seconds (default: 10, 60 for updates, "
                             "sum of the test timeouts for inet)")
    parser.add_argument("--ssh", default="ssh", help="ssh binary (default: ssh)")
    sub = parser.add_subparsers(dest="check", metavar="CHECK")
    sub.required = True
    for name, (_, _, help_text, warning, critical) in CHECKS.items():
        p = sub.add_parser(name, help=help_text, description=help_text)
        rtype = {"load": parse_triplet, "inet": parse_ping_thresholds}.get(name, Range)
        if warning or critical or name in ("uptime",):
            # argparse formats help texts with %, so % must be doubled
            kind = "ping thresholds RTA,LOSS%%" if name == "inet" else "threshold range"
            shown_w = (warning or "none").replace("%", "%%")
            shown_c = (critical or "none").replace("%", "%%")
            p.add_argument("-w", "--warning", type=rtype, default=rtype(warning),
                           help=f"warning {kind} (default: {shown_w})")
            p.add_argument("-c", "--critical", type=rtype, default=rtype(critical),
                           help=f"critical {kind} (default: {shown_c})")
        if name == "load":
            p.add_argument("-r", "--percpu", action="store_true",
                           help="divide the load averages by the number of CPUs")
        elif name == "disk":
            p.add_argument("-P", "--path", action="append",
                           help="mount point to check, can be given multiple times; "
                                "CRITICAL if it is not mounted")
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
        elif name == "temp":
            p.add_argument("-x", "--exclude", action="append",
                           help="sensor to skip, by its performance data label (e.g. eth_phy_05)")
        elif name == "banip":
            p.add_argument("-a", "--max-age", type=int, default=90000,
                           help="WARNING if the last banIP run is older, in seconds "
                                "(default: 90000 = 25 hours, 0 = off)")
        elif name == "inet":
            p.add_argument("-D", "--dns-name", default="openwrt.org",
                           help="name to look up (default: openwrt.org)")
            p.add_argument("-S", "--dns-server",
                           help="DNS server to ask (default: the device's resolver)")
            p.add_argument("-A", "--address", default="9.9.9.9",
                           help="address to ping (default: 9.9.9.9)")
            p.add_argument("--packets", type=int, default=3,
                           help="number of ping packets (default: 3)")
            p.add_argument("-U", "--url", default="https://downloads.openwrt.org/",
                           help="URL to fetch (default: https://downloads.openwrt.org/)")
            p.add_argument("--http-timeout", type=int, default=10,
                           help="timeout of the HTTP(S) request in seconds (default: 10)")
            p.add_argument("--no-dns", action="store_true", help="skip the DNS test")
            p.add_argument("--no-ping", action="store_true", help="skip the ping test")
            p.add_argument("--no-http", action="store_true", help="skip the HTTP(S) test")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.timeout is None:
        if args.check == "updates":
            args.timeout = 60
        elif args.check == "inet":
            args.timeout = inet_timeout(args)
        else:
            args.timeout = 10
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
