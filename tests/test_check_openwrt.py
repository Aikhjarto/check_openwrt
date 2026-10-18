# Copyright (C) 2026 Thomas Wagner <wagner-thomas@gmx.at>
# SPDX-License-Identifier: GPL-2.0-or-later
"""Tests for check_openwrt using canned device output (no SSH needed)."""

import argparse
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import check_openwrt as co  # noqa: E402


def parse(*argv):
    return co.build_parser().parse_args(["-H", "device.example"] + list(argv))


def run(check_args, output):
    """Run main() with ssh replaced by canned output, return (state, text)."""
    with mock.patch.object(co, "run_ssh", return_value=output), \
            mock.patch("builtins.print") as printed:
        state = co.main(["-H", "device.example"] + check_args)
    return state, printed.call_args[0][0]


class RangeTest(unittest.TestCase):
    def test_simple(self):
        r = co.Range("10")
        self.assertFalse(r.alert(0))
        self.assertFalse(r.alert(10))
        self.assertTrue(r.alert(10.1))
        self.assertTrue(r.alert(-1))

    def test_open_end(self):
        r = co.Range("3600:")
        self.assertTrue(r.alert(3599))
        self.assertFalse(r.alert(10 ** 9))

    def test_negative_infinity(self):
        r = co.Range("~:5")
        self.assertFalse(r.alert(-100))
        self.assertTrue(r.alert(6))

    def test_inside(self):
        r = co.Range("@10:20")
        self.assertTrue(r.alert(15))
        self.assertFalse(r.alert(25))

    def test_empty_never_alerts(self):
        self.assertFalse(co.Range("").alert(1e12))

    def test_invalid(self):
        for spec in ("abc", "20:10"):
            with self.assertRaises(argparse.ArgumentTypeError):
                co.Range(spec)

    def test_worst(self):
        self.assertEqual(co.worst(co.OK, co.UNKNOWN), co.UNKNOWN)
        self.assertEqual(co.worst(co.UNKNOWN, co.WARNING), co.WARNING)
        self.assertEqual(co.worst(co.WARNING, co.CRITICAL, co.OK), co.CRITICAL)


MEMINFO = """MemTotal:         100000 kB
MemFree:           10000 kB
MemAvailable:      15000 kB
Buffers:            1000 kB
Cached:             4000 kB
"""


class SimpleChecksTest(unittest.TestCase):
    def test_mem(self):
        state, text = run(["mem"], MEMINFO)
        self.assertEqual(state, co.WARNING)
        self.assertIn("85.0% used", text)
        self.assertIn("used_pct=85%;80;90;0;100", text)

    def test_mem_without_memavailable(self):
        state, _ = run(["mem", "-w", "95", "-c", "99"],
                       MEMINFO.replace("MemAvailable", "Ignored"))
        self.assertEqual(state, co.OK)

    def test_disk(self):
        df = ("Filesystem           1024-blocks    Used Available Capacity Mounted on\n"
              "/dev/ubi0_1              10000      9500       500  95% /overlay\n")
        state, text = run(["disk"], df)
        self.assertEqual(state, co.CRITICAL)
        self.assertIn("/overlay 95.0% used", text)

    def test_disk_script_quotes_paths(self):
        self.assertEqual(co.script_disk(parse("disk", "-P", "/mnt/my disk", "-P", "/tmp")),
                         "df -kP '/mnt/my disk' '/tmp'")

    def test_load_percpu(self):
        state, text = run(["load", "-r", "-w", "1", "-c", "2"],
                          "6.00 4.00 2.00 1/100 1234\n4\n")
        self.assertEqual(state, co.WARNING)
        self.assertIn("1.50, 1.00, 0.50", text)

    def test_load_triplet_thresholds(self):
        args = parse("load", "-w", "1,2,3")
        self.assertEqual([str(r) for r in args.warning], ["1", "2", "3"])

    def test_uptime_recent_reboot(self):
        state, text = run(["uptime"], "120.5 300.0\n")
        self.assertEqual(state, co.WARNING)
        self.assertIn("recently rebooted", text)

    def test_uptime_ok(self):
        state, _ = run(["uptime"], "864000.00 1.00\n")
        self.assertEqual(state, co.OK)

    def test_conntrack(self):
        state, text = run(["conntrack"], "950\n1000\n")
        self.assertEqual(state, co.CRITICAL)
        self.assertIn("950 of 1000", text)

    def test_conntrack_missing(self):
        state, _ = run(["conntrack"], "")
        self.assertEqual(state, co.UNKNOWN)


IFACES = json.dumps({"interface": [
    {"interface": "lan", "up": True, "pending": False, "autostart": True,
     "proto": "static", "uptime": 100},
    {"interface": "wan", "up": False, "pending": False, "autostart": True,
     "proto": "dhcp", "errors": [{"code": "NO_DEVICE"}]},
    {"interface": "wan6", "up": False, "pending": True, "autostart": True,
     "proto": "dhcpv6"},
    {"interface": "spare", "up": False, "pending": False, "autostart": False,
     "proto": "none"},
]})


class IfaceTest(unittest.TestCase):
    def test_default_checks_autostart_interfaces(self):
        state, text = run(["iface"], IFACES)
        self.assertEqual(state, co.CRITICAL)
        self.assertIn("wan down (NO_DEVICE)", text)
        self.assertIn("wan6 pending", text)
        self.assertNotIn("spare", text)

    def test_exclude(self):
        state, text = run(["iface", "-x", "wan", "-x", "wan6"], IFACES)
        self.assertEqual(state, co.OK)
        self.assertIn("all 1 interfaces up", text)

    def test_missing_interface(self):
        state, text = run(["iface", "-I", "lan", "-I", "vpn"], IFACES)
        self.assertEqual(state, co.CRITICAL)
        self.assertIn("vpn not found", text)


def wireless(radio1_up=True):
    return json.dumps({
        "radio0": {"up": True, "pending": False, "disabled": False,
                   "config": {"band": "2g", "channel": "6"},
                   "interfaces": [{"section": "default_radio0", "ifname": "phy0-ap0",
                                   "config": {"ssid": "ExampleNet"}}]},
        "radio1": {"up": radio1_up, "pending": False, "disabled": False,
                   "config": {"band": "5g", "channel": "36"}, "interfaces": []},
        "radio2": {"up": False, "pending": False, "disabled": True, "interfaces": []},
    })


CLIENTS = json.dumps({"freq": 2437, "clients": {
    "02:00:00:00:00:01": {"auth": True}, "02:00:00:00:00:02": {"auth": True}}})


class WifiTest(unittest.TestCase):
    def test_ok_with_clients(self):
        out = wireless() + "\n@@clients@@\n@@hostapd.phy0-ap0@@\n" + CLIENTS
        state, text = run(["wifi"], out)
        self.assertEqual(state, co.OK)
        self.assertIn("2 radios up, 1 disabled, 2 clients", text)
        self.assertIn("phy0-ap0 'ExampleNet': 2 clients", text)
        self.assertIn("clients_phy0-ap0=2", text)

    def test_radio_down(self):
        state, text = run(["wifi"], wireless(radio1_up=False) + "\n@@clients@@\n")
        self.assertEqual(state, co.CRITICAL)
        self.assertIn("radio1 down", text)

    def test_no_radios(self):
        state, _ = run(["wifi"], "{}\n@@clients@@\n")
        self.assertEqual(state, co.UNKNOWN)


KEY_A = "A" * 43 + "="
KEY_B = "B" * 43 + "="
KEY_C = "C" * 43 + "="
WG_DUMP = "\n".join([
    "1700001000",
    "@@dump@@",
    "\t".join(["wg0", "(hidden)", "S" * 43 + "=", "51820", "off"]),
    "\t".join(["wg0", KEY_A, "(none)", "192.0.2.10:51820", "10.0.0.2/32",
               "1700000950", "1000", "2000", "25"]),
    "\t".join(["wg0", KEY_B, "(none)", "198.51.100.7:4000", "10.0.0.3/32",
               "1700000000", "10", "20", "off"]),
    "\t".join(["wg0", KEY_C, "(none)", "(none)", "10.0.0.4/32", "0", "0", "0", "off"]),
    "@@uci@@",
    f"network.@wireguard_wg0[0].description='Site A'",
    f"network.@wireguard_wg0[0].public_key='{KEY_A}'",
    f"network.peer_b.description='laptop'",
    f"network.peer_b.public_key='{KEY_B}'",
])


class WireguardTest(unittest.TestCase):
    def test_unchecked_peers_are_info_only(self):
        state, text = run(["wireguard"], WG_DUMP)
        self.assertEqual(state, co.OK)
        self.assertIn("1 interfaces, 3 peers, 1 with recent handshake", text)
        self.assertIn("'Site A_handshake'=50s", text)

    def test_peer_by_description(self):
        state, text = run(["wireguard", "-P", "laptop"], WG_DUMP)
        self.assertEqual(state, co.CRITICAL)
        self.assertIn("laptop: last handshake 16m 40s ago", text)

    def test_peer_by_key_without_handshake(self):
        state, text = run(["wireguard", "-P", KEY_C], WG_DUMP)
        self.assertEqual(state, co.CRITICAL)
        self.assertIn("CCCCCCCC: no handshake", text)

    def test_missing_peer(self):
        state, text = run(["wireguard", "-P", "Site A", "-P", "nonexistent"], WG_DUMP)
        self.assertEqual(state, co.CRITICAL)
        self.assertIn("peer nonexistent not found", text)

    def test_no_wg(self):
        state, text = run(["wireguard"], "@@nowg@@\n")
        self.assertEqual(state, co.UNKNOWN)
        self.assertIn("wg not found", text)

    def test_uci_quoting(self):
        peers = co.parse_uci_peers(
            "network.x.description='it'\\''s me'\nnetwork.x.public_key='K'\n")
        self.assertEqual(peers, {"K": "it's me"})


SERVICES = json.dumps({
    "dnsmasq": {"instances": {"cfg01": {"running": True, "pid": 1, "respawn": {}}}},
    "urandom_seed": {"instances": {"urandom_seed": {"running": False, "exit_code": 0}}},
    "firewall": {},
    "uhttpd": {"instances": {"instance1": {"running": False, "respawn": {}}}},
    "oneshot": {"instances": {"instance1": {"running": False, "exit_code": 1}}},
})


class ServicesTest(unittest.TestCase):
    def test_states(self):
        state, text = run(["services"], SERVICES)
        self.assertEqual(state, co.CRITICAL)
        self.assertIn("uhttpd not running", text)
        self.assertIn("[WARNING] oneshot not running (exit code 1)", text)
        self.assertNotIn("urandom_seed", text)

    def test_exclude_and_required(self):
        state, text = run(["services", "-x", "uhttpd", "-x", "oneshot", "-s", "dnsmasq"],
                          SERVICES)
        self.assertEqual(state, co.OK)
        self.assertIn("1 service instances running", text)

    def test_required_missing(self):
        state, text = run(["services", "-s", "dropbear", "-x", "uhttpd", "-x", "oneshot"],
                          SERVICES)
        self.assertEqual(state, co.CRITICAL)
        self.assertIn("dropbear not found", text)


class UpdatesTest(unittest.TestCase):
    def test_opkg(self):
        out = ("@@opkg@@\n@@refresh 0@@\n@@lists 2@@\n@@list@@\n"
               "busybox - 1.0-r1 - 1.0-r2\ndnsmasq - 2.0-r1 - 2.0-r2\n")
        state, text = run(["updates"], out)
        self.assertEqual(state, co.WARNING)
        self.assertIn("2 package updates available (opkg)", text)
        self.assertIn("  busybox", text)

    def test_apk_none(self):
        state, text = run(["updates"], "@@apk@@\n@@refresh 0@@\n@@list@@\n")
        self.assertEqual(state, co.OK)
        self.assertIn("no package updates available (apk)", text)

    def test_refresh_failed(self):
        state, _ = run(["updates"], "@@opkg@@\n@@refresh 1@@\n@@lists 0@@\n@@list@@\n")
        self.assertEqual(state, co.UNKNOWN)

    def test_no_lists(self):
        state, text = run(["updates", "-n"], "@@opkg@@\n@@lists 0@@\n@@list@@\n")
        self.assertEqual(state, co.UNKNOWN)
        self.assertIn("no package lists", text)

    def test_timeout_default(self):
        with mock.patch.object(co, "run_ssh", return_value="@@apk@@\n@@list@@\n") as ssh, \
                mock.patch("builtins.print"):
            co.main(["-H", "device.example", "updates"])
        self.assertEqual(ssh.call_args[0][0].timeout, 60)


DNS_OK = """@@dns@@
Server:\t\t127.0.0.1
Address:\t127.0.0.1:53

Non-authoritative answer:
Name:\texample.org
Address: 192.0.2.80

Non-authoritative answer:
Name:\texample.org
Address: 2001:db8::80

@@dns-rc 0 1000.00 1000.03@@
"""
DNS_NXDOMAIN = """@@dns@@
Server:\t\t127.0.0.1
Address:\t127.0.0.1:53

** server can't find example.invalid: NXDOMAIN

** server can't find example.invalid: NXDOMAIN

@@dns-rc 1 1000.00 1000.01@@
"""
DNS_TIMEOUT = """@@dns@@
;; connection timed out; no servers could be reached
@@dns-rc 1 1000.00 1010.00@@
"""


def ping(received, rtt="14.914/16.210/16.924"):
    out = (f"@@ping@@\nPING 198.51.100.9 (198.51.100.9): 56 data bytes\n\n"
           f"--- 198.51.100.9 ping statistics ---\n"
           f"3 packets transmitted, {received} packets received, "
           f"{100 - received * 100 // 3}% packet loss\n")
    if received:
        out += f"round-trip min/avg/max = {rtt} ms\n"
    return out + f"@@ping-rc {0 if received else 1}@@\n"


HTTP_OK = """@@http@@
Downloading 'https://example.org/'
Connecting to 192.0.2.80:443
Writing to '/dev/null'
Download completed (9186 bytes)
@@http-rc 0 1000.10 1000.58@@
"""
HTTP_BAD_CERT = """@@http@@
Downloading 'https://example.org/'
Connecting to 192.0.2.80:443
SSL verify error: certificate has expired
Connection error: Invalid SSL certificate
@@http-rc 5 1000.10 1000.30@@
"""


class InetTest(unittest.TestCase):
    ARGS = ["inet", "-D", "example.org", "-A", "198.51.100.9", "-U", "https://example.org/"]

    def test_all_ok(self):
        state, text = run(self.ARGS, DNS_OK + ping(3) + HTTP_OK)
        self.assertEqual(state, co.OK)
        first = text.splitlines()[0]
        self.assertIn("internet access: DNS 0.03 s, ping 198.51.100.9 0% loss, rta 16.2 ms, "
                      "HTTPS 0.48 s", first)
        self.assertIn("rta=16.21ms;200;500;0", first)
        self.assertIn("pl=0%;20;60;0;100", first)
        self.assertIn("http_time=0.48s", first)
        self.assertIn("example.org: 192.0.2.80, 2001:db8::80", text)
        lines = text.splitlines()
        self.assertTrue(lines[1].startswith("[OK] DNS") and lines[2].startswith("[OK] ping")
                        and lines[3].startswith("[OK] HTTPS"))

    def test_nxdomain(self):
        state, text = run(self.ARGS, DNS_NXDOMAIN + ping(3) + HTTP_OK)
        self.assertEqual(state, co.CRITICAL)
        self.assertIn("DNS lookup of example.org failed: NXDOMAIN", text)

    def test_dns_timeout(self):
        state, text = run(self.ARGS + ["--no-ping", "--no-http"], DNS_TIMEOUT)
        self.assertEqual(state, co.CRITICAL)
        self.assertIn("failed: connection timed out; no servers could be reached", text)

    def test_ping_lost_but_http_works_is_warning(self):
        state, text = run(self.ARGS, DNS_OK + ping(0) + HTTP_OK)
        self.assertEqual(state, co.WARNING)
        self.assertIn("100% loss (HTTP works)", text)

    def test_ping_lost_without_http_is_critical(self):
        state, _ = run(self.ARGS + ["--no-http"], DNS_OK + ping(0))
        self.assertEqual(state, co.CRITICAL)

    def test_ping_lost_and_http_failed(self):
        state, text = run(self.ARGS, DNS_OK + ping(0) + HTTP_BAD_CERT)
        self.assertEqual(state, co.CRITICAL)
        self.assertNotIn("HTTP works", text)
        self.assertIn("HTTPS failed: SSL verify error: certificate has expired; "
                      "Connection error: Invalid SSL certificate", text)

    def test_rta_thresholds(self):
        out = DNS_OK + ping(3, "250.0/300.0/350.0") + HTTP_OK
        self.assertEqual(run(self.ARGS, out)[0], co.WARNING)
        self.assertEqual(run(self.ARGS + ["-c", "280,60%"], out)[0], co.WARNING)
        self.assertEqual(run(self.ARGS + ["-c", "280,60%", "--no-http"], out)[0], co.CRITICAL)

    def test_loss_thresholds(self):
        state, text = run(self.ARGS, DNS_OK + ping(2) + HTTP_OK)
        self.assertEqual(state, co.WARNING)
        self.assertIn("33% loss", text)

    def test_bad_ping_address(self):
        out = "@@ping@@\nping: bad address 'example.invalid'\n@@ping-rc 1@@\n"
        state, text = run(["inet", "--no-dns", "--no-http"], out)
        self.assertEqual(state, co.CRITICAL)
        self.assertIn("bad address", text)

    def test_missing_result_is_unknown(self):
        state, text = run(self.ARGS, DNS_OK + ping(3))
        self.assertEqual(state, co.UNKNOWN)
        self.assertIn("no result of the http test", text)

    def test_all_disabled(self):
        state, text = run(["inet", "--no-dns", "--no-ping", "--no-http"], "")
        self.assertEqual(state, co.UNKNOWN)
        self.assertIn("all tests are disabled", text)

    def test_threshold_format(self):
        args = parse("inet", "-w", "100,10%")
        self.assertEqual([str(r) for r in args.warning], ["100", "10"])
        with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
            parse("inet", "-w", "100")

    def test_script_quotes_values(self):
        args = parse("inet", "-D", "a b", "-S", "192.0.2.53", "-A", "x;y", "--packets", "5",
                     "-U", "https://example.org/?a=1&b=2", "--http-timeout", "7")
        script = co.script_inet(args)
        self.assertIn("nslookup -timeout=5 -retry=1 'a b' '192.0.2.53'", script)
        self.assertIn("ping -q -c 5 -W 1 -w 7 'x;y'", script)
        self.assertIn("uclient-fetch -T 7 -O /dev/null 'https://example.org/?a=1&b=2'", script)
        self.assertNotIn("nslookup", co.script_inet(parse("inet", "--no-dns")))

    def test_timeout_default(self):
        self.assertEqual(co.inet_timeout(parse("inet")), 5 + 10 + 5 + 10)
        self.assertEqual(co.inet_timeout(parse("inet", "--no-dns", "--no-ping")), 15)
        with mock.patch.object(co, "run_ssh", return_value="") as ssh, \
                mock.patch("builtins.print"):
            co.main(["-H", "device.example", "inet"])
        self.assertEqual(ssh.call_args[0][0].timeout, 30)


class SshTest(unittest.TestCase):
    def test_command_line(self):
        args = parse("-l", "monitor", "-p", "2222", "-i", "/key", "-o", "Foo=bar", "uptime")
        args.timeout = 5
        proc = mock.Mock(returncode=0, stdout="1 2\n", stderr="")
        with mock.patch.object(co.subprocess, "run", return_value=proc) as srun:
            self.assertEqual(co.run_ssh(args, "cat /proc/uptime"), "1 2\n")
        cmd = srun.call_args[0][0]
        self.assertEqual(cmd[:5], ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5"])
        self.assertEqual(cmd[-4:], ["-l", "monitor", "device.example", "cat /proc/uptime"])
        self.assertIn("2222", cmd)
        self.assertIn("Foo=bar", cmd)

    def test_ssh_failure_is_unknown(self):
        proc = mock.Mock(returncode=255, stdout="", stderr="Permission denied (publickey).\n")
        with mock.patch.object(co.subprocess, "run", return_value=proc), \
                mock.patch("builtins.print") as printed:
            state = co.main(["-H", "device.example", "uptime"])
        self.assertEqual(state, co.UNKNOWN)
        self.assertIn("ssh failed: Permission denied", printed.call_args[0][0])

    def test_timeout_is_unknown(self):
        err = co.subprocess.TimeoutExpired("ssh", 10)
        with mock.patch.object(co.subprocess, "run", side_effect=err), \
                mock.patch("builtins.print") as printed:
            state = co.main(["-H", "device.example", "mem"])
        self.assertEqual(state, co.UNKNOWN)
        self.assertIn("timeout after 10s", printed.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
