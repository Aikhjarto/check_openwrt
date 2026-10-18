# check_openwrt

Agentless monitoring plugin for [OpenWrt](https://openwrt.org) devices,
compatible with Nagios, Naemon, Icinga and other tools that use the
[Monitoring Plugins](https://www.monitoring-plugins.org/doc/guidelines.html)
interface.

All checks run over SSH. Nothing has to be installed on the device: the
plugin only uses tools shipped with a default OpenWrt image (BusyBox, `ubus`,
`uci`, `opkg`/`apk`), plus `wg` from `wireguard-tools` for the WireGuard check.
This keeps it usable even on devices with only a few MB of flash.

The plugin is a single Python 3 file without dependencies outside the
standard library.

## Checks

| Check       | What it monitors                                                    | Default thresholds |
|-------------|---------------------------------------------------------------------|--------------------|
| `mem`       | memory usage in percent, based on `MemAvailable`                    | `-w 80 -c 90`      |
| `disk`      | usage of `/overlay` (or `/`) in percent, other mount points with `-P` | `-w 80 -c 90`    |
| `load`      | load averages (1, 5, 15 min), per CPU with `-r`                     | `-w 4,3,2 -c 8,6,4` |
| `uptime`    | uptime in seconds, detects (unnoticed) reboots                      | `-w 3600:`         |
| `conntrack` | usage of the connection tracking table in percent                   | `-w 75 -c 90`      |
| `iface`     | netifd interfaces are up (all with autostart, or `-I`/`-x`)         | down: CRITICAL, pending: WARNING |
| `wifi`      | wireless radios are up, client count per SSID as performance data   | down: CRITICAL, disabled radios are OK |
| `wireguard` | handshake age of selected peers (`-P`, `-a`) in seconds, traffic counters | `-w 300 -c 900` |
| `services`  | procd service instances are running                                 | dead daemon: CRITICAL, failed one-shot job: WARNING |
| `updates`   | number of upgradable packages (`opkg` or `apk`)                     | `-w 0`             |

Thresholds use the standard
[range format](https://www.monitoring-plugins.org/doc/guidelines.html#THRESHOLDFORMAT):
`10` alerts above 10, `10:` below 10, `~:10` above 10, `10:20` outside of
10..20 and `@10:20` inside of 10..20. An empty value (`-c ''`) disables a
threshold.

All checks print performance data. If the plugin cannot determine a state
(SSH failure, timeout, missing tool on the device), it exits with UNKNOWN.

## Requirements

* Python 3.7 or newer on the monitoring server
* the OpenSSH client (`ssh`) on the monitoring server
* SSH key login to the device (OpenWrt's dropbear supports this out of the
  box), usually as `root`

## Installation

The plugin is a single file, so copying it is enough:

```sh
install -m 755 check_openwrt.py /usr/lib/nagios/plugins/check_openwrt
```

Alternatively install it with pip, which creates a `check_openwrt` command:

```sh
pip install .
```

## Bash completion

`completions/check_openwrt` completes the checks, the options of the plugin
and of each check, host names, users and the key file. The packages install
it; otherwise copy it to bash-completion's directory:

```sh
install -D -m 0644 completions/check_openwrt /usr/share/bash-completion/completions/check_openwrt
```

## SSH setup

1. Create a key for the monitoring user (if it has none) and add the public
   key to `/etc/dropbear/authorized_keys` on the device, or in LuCI under
   *System → Administration → SSH-Keys*.
2. Log in once manually as the monitoring user to accept the host key, e.g.
   `ssh root@router.example.org true`.
3. Optional, but recommended: restrict the key to the monitoring server by
   prefixing its line in `authorized_keys` with
   `from="192.0.2.10" ` (the address of the monitoring server).

The plugin calls `ssh` with `BatchMode=yes`, so it never hangs at a password
prompt. All settings from `~/.ssh/config` apply. Connection sharing is
recommended, so the checks reuse one SSH connection per device instead of
logging in for every check:

```
Host router.example.org ap*.example.org
    ControlMaster auto
    ControlPath ~/.ssh/control-%C
    ControlPersist 11m
    ServerAliveInterval 30
```

## Usage

```
check_openwrt -H HOST [-l USER] [-p PORT] [-i KEYFILE] [-o SSH_OPTION]
              [-t TIMEOUT] CHECK [CHECK OPTIONS]
```

`check_openwrt --help` and `check_openwrt CHECK --help` list all options.
The default timeout is 10 seconds, and 60 seconds for `updates`.

Examples:

```sh
check_openwrt -H router.example.org mem
check_openwrt -H router.example.org disk -w 70 -c 85
check_openwrt -H router.example.org load -r -w 1.5 -c 3
check_openwrt -H router.example.org iface -x wan6
check_openwrt -H router.example.org wifi -x radio2
check_openwrt -H router.example.org wireguard -P "office" -P "xyzKEY...="
check_openwrt -H router.example.org services -s dnsmasq -s dropbear
check_openwrt -H router.example.org updates
```

Example output:

```
OPENWRT WIFI OK - 2 radios up, 0 disabled, 6 clients | clients_phy0-ap0=4;;;0 clients_phy1-ap0=2;;;0 clients=6;;;0
[OK] radio0 up (2g, channel 1)
  phy0-ap0 'ExampleNet': 4 clients
[OK] radio1 up (5g, channel 36)
  phy1-ap0 'ExampleNet': 2 clients
```

### Notes on individual checks

* **iface**: without `-I`, all interfaces with `autostart` enabled are
  checked. Exclude interfaces that are expected to be down, e.g. `wan6` when
  the provider has no IPv6.
* **wifi**: radios disabled in `/etc/config/wireless` are reported as OK.
  Client counts need hostapd's ubus interface, which is part of the default
  `wpad`/`hostapd` packages.
* **wireguard**: peers are named by the `description` of their uci peer
  section, falling back to the first 8 characters of the public key. Only
  peers selected with `-P` (by description or public key) or `-a` are checked,
  because roaming clients like phones are not always connected. All other
  peers are listed for information and in the performance data.
* **services**: services managed by procd are checked. Instances that are not
  running are CRITICAL if procd is supposed to respawn them, one-shot jobs
  that exited with code 0 are ignored. With `-s`, a service must exist and run.
* **updates**: refreshes the package lists first (`opkg update` or
  `apk update`); they are stored in RAM. Use `-n` to skip the refresh. Run
  this check rarely, e.g. once a day. Note that upgrading individual packages
  is usually not recommended on OpenWrt; many available updates often mean a
  new point release is available for sysupgrade.

## Naemon / Nagios configuration example

```
define command {
  command_name  check_openwrt
  command_line  $USER1$/check_openwrt -H $HOSTADDRESS$ $ARG1$
}

define service {
  use                  generic-service
  host_name            router.example.org
  service_description  Memory
  check_command        check_openwrt!mem
}

define service {
  use                  generic-service
  host_name            router.example.org
  service_description  Interfaces
  check_command        check_openwrt!iface -x wan6
}

define service {
  use                  generic-service
  host_name            router.example.org
  service_description  Package updates
  check_command        check_openwrt!updates
  check_interval       1440
}
```

## Development

Run the tests (no device or SSH access needed):

```sh
python3 -m unittest discover -s tests -v
```

## License

Copyright (C) 2026 Thomas Wagner

This program is free software; you can redistribute it and/or modify it
under the terms of the GNU General Public License as published by the Free
Software Foundation; either version 2 of the License, or (at your option) any
later version. See [LICENSE](LICENSE) for the full text.
