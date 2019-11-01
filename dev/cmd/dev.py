import glob
import os.path
import spack
import spack.cmd

# Import sub-commands
subcommands = []


def import_subcommands_automatically():
    subcommand_glob = os.path.join(os.path.dirname(__file__), '..', 'subcommands', '*.py')
    command_files = glob.glob(subcommand_glob)
    subcommands.extend(filter(
        lambda x: not x.startswith('__'), [os.path.basename(x)[:-3] for x in command_files]
    ))
    t = __import__('subcommands', globals(), locals(), subcommands, 2)
    for name in subcommands:
        globals()[name] = getattr(t, name)

import_subcommands_automatically()

description = 'develop multiple Spack packages simultaneously'
section = 'developer'
level = 'long'

_subcmd_functions = {}


def setup_parser(parser):
    subparsers = parser.add_subparsers(metavar='SUBCOMMAND', dest='dev_command')
    for name in subcommands:
        cmd_module = globals()[name]
        sp = subparsers.add_parser(name, help=cmd_module.description)
        command_fn = getattr(cmd_module, spack.cmd.python_name(name))
        cmd_module.setup_parser(sp)
        _subcmd_functions[name] = command_fn


def dev(parser, args):
    _subcmd_functions[args.dev_command](parser, args)
