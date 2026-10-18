# Copyright (C) 2026 Thomas Wagner <wagner-thomas@gmx.at>
# SPDX-License-Identifier: GPL-2.0-or-later
"""Tests of the bash completion in completions/check_openwrt."""

import contextlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

import check_openwrt as co  # noqa: E402

COMPLETION = os.path.join(ROOT, "completions", "check_openwrt")


def complete(*words, cwd=None):
    """The candidates bash offers for the last of words, which may be empty."""
    words = ("check_openwrt",) + words
    script = 'source "$1"; shift; COMP_WORDS=("$@"); COMP_CWORD=$(($# - 1)); ' \
             '_check_openwrt; printf "%s\\n" "${COMPREPLY[@]}"'
    result = subprocess.run(["bash", "-c", script, "bash", COMPLETION] + list(words),
                            stdout=subprocess.PIPE, universal_newlines=True, check=True, cwd=cwd)
    return [line for line in result.stdout.split("\n") if line]


def help_words(*args):
    """The option strings and checks --help lists.

    Before Python 3.13 argparse repeats the metavar after every option string
    ("-H HOSTNAME, --hostname HOSTNAME"), since then only after the last
    ("-H, --hostname HOSTNAME"). An option line starts with the invocation,
    followed by the description after two or more spaces or, if the
    invocation is long, on the next line.
    """
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.suppress(SystemExit):
        co.build_parser().parse_args(list(args) + ["--help"])
    words = set()
    for line in output.getvalue().split("\n"):
        match = re.match(r"  (-\S.*?)(?:\s{2,}|$)", line)
        if match:
            words.update(re.findall(r"(?:^|, )(-{1,2}[^\s,=\[]+)", match.group(1)))
        match = re.match(r"    ([a-z]+)(?:\s{2,}|$)", line)
        if match and not args:
            words.add(match.group(1))
    return words


@unittest.skipUnless(shutil.which("bash"), "bash is not installed")
class CompletionTests(unittest.TestCase):
    def test_syntax(self):
        subprocess.run(["bash", "-n", COMPLETION], check=True)

    def test_offers_exactly_the_checks_and_options_of_help(self):
        self.assertEqual(set(complete("")), help_words())
        self.assertTrue(set(co.CHECKS) <= help_words())

    def test_offers_exactly_the_options_of_the_check_help(self):
        for check in co.CHECKS:
            self.assertEqual(set(complete("-H", "router", check, "")),
                             help_words("-H", "router", check), check)

    def test_check_prefix(self):
        self.assertEqual(complete("-H", "router", "w"), ["wifi", "wireguard"])
        self.assertEqual(complete("u"), ["uptime", "updates"])

    def test_check_after_option_values(self):
        self.assertEqual(complete("-H", "mem", "-l", "root", "con"), ["conntrack"])
        self.assertEqual(complete("--hostname", "=", "router", "-t", "5", "load", "-r", "--p"),
                         ["--percpu"])
        self.assertEqual(complete("-o", "ConnectTimeout=5", "updates", "-n", "--no"),
                         ["--no-refresh"])

    def test_identity_file(self):
        with tempfile.TemporaryDirectory() as directory:
            open(os.path.join(directory, "router.key"), "w").close()
            self.assertEqual(complete("-i", "rou", cwd=directory), ["router.key"])
            self.assertEqual(complete("--identity", "=", "rou", cwd=directory), ["router.key"])

    def test_no_values_for_thresholds_and_names(self):
        for words in (("-p", ""), ("-t", ""), ("mem", "-w", ""), ("iface", "-x", ""),
                      ("services", "--service", "=", ""), ("inet", "-U", ""),
                      ("inet", "--dns-name", "=", "")):
            self.assertEqual(complete(*words), [], words)

    def test_unknown_check(self):
        self.assertEqual(complete("bogus", ""), [])


if __name__ == "__main__":
    unittest.main()
