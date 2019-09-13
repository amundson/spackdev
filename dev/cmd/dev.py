import argparse
import os
import sys

import spack
from spack.cmd import find_commands, python_name
from spack.command_loading import get_command_module_from
import llnl.util.tty as tty

SPACKDEV_FILE = os.path.realpath(os.path.expanduser(__file__))
SPACKDEV_LIB = os.path.realpath(os.path.join(os.path.dirname(SPACKDEV_FILE),
                                             '..', '..', 'lib'))
sys.path.insert(0, SPACKDEV_LIB)

description = 'develop multiple Spack packages simultaneously'
section = 'developer'
level = 'long'

_subcmd_dir = os.path.join(SPACKDEV_LIB, 'fnal', 'spack', 'dev', 'cmd')
_subcmds = None
_subcmd_functions = {}


def add_subcommand(subparser, subcmd):
    pname = python_name(subcmd)
    module = get_command_module_from(subcmd, 'fnal.spack.dev')
    sp = subparser.add_parser(subcmd, help=module.description)
    module.setup_parser(sp)
    global _subcmd_functions
    _subcmd_functions[subcmd] = getattr(module, pname)


def setup_parser(subparser):
    sp = subparser.add_subparsers(metavar='SUBCOMMAND', dest='dev_command')
    global _subcmds
    _subcmds = find_commands(_subcmd_dir)
    for subcmd in _subcmds:
        add_subcommand(sp, subcmd)


def dev(parser, args):
    _subcmd_functions[args.dev_command](parser, args)
