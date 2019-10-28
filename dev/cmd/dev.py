import spack
import spack.cmd

# Import sub-commands
from ..subcommands import build_env, findext, getdeps, info, init, stage
subcommands = ['build_env', 'findext', 'getdeps', 'info', 'init', 'stage']

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
