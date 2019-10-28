#!/usr/bin/env python

import argparse
import os
import sys

from llnl.util import tty


description = "describe a spackdev area"


def setup_parser(subparser):
    subparser.add_argument('pathname', nargs='?', default='',
                           help="pathname of SpackDev area")


def info(parser, args):
    dev.environment.bootstrap_environment(args.pathname)

    requested, additional, deps = dev.cmd.read_package_info(want_specs=False)
    print('requested packages:')
    for package in requested:
        print('    ' + package)
    print('additional dependent packages:')
    for package in additional:
        print('    ' + package)
    print('collected dependencies: ')
    for package in deps:
        print('    ' + package)
