#!/usr/bin/env python
from __future__ import print_function

import os
import re
import tempfile

from llnl.util import tty

from ..environment import bootstrap_environment, \
    load_environment

description = "run a command in the build environment of a spackdev package, or start a shell in same."


prompt_splitter = re.compile(r'(.*?)([#\$]\s*)?$')
def update_prompt(args_cli):
    tty.msg('Setting prompt')
    tfile = tempfile.NamedTemporaryFile(mode='w', delete=False)
    tfile.write(r'''if [ -r ~/.bashrc ]; then . ~/.bashrc; fi
  if [[ "${{PS1}}" =~ ^(.*)([\#\$%][ 	]*)$ ]]; then
  PS1="${{BASH_REMATCH[1]}}\[\e[1;95m\]{package}\[\e[m\] ${{BASH_REMATCH[2]}}"
  fi
rm -f "{tfile_name}"
'''.format(package=args_cli.package, tfile_name=tfile.name))
    args_cli.cmd.extend(['--rcfile', tfile.name])


def setup_parser(subparser):
    subparser.add_argument('--cd', action='store_true', default=False,
                           help='Execute the command in the build directory for the specified package')
    subparser.add_argument('--prompt', action='store_true', default=False,
                           help='Show the package whose environment is current at the command prompt of interactive shells (BASH only).')
    subparser.add_argument('package',
                           help='package for which to initialize environment.')
    subparser.add_argument('cmd', nargs='*',
                           help='Command and arguments to execute (default is to start a shell)')


def build_env(parser, args):
    bootstrap_environment()
    if not args.cmd:
        shell = os.environ['SPACK_SHELL']
        if not shell:
            shell = os.environ['SHELL']
        args.cmd = [ shell ]
        if args.prompt:
            if shell.endswith('bash'):
                update_prompt(args)
            else:
                tty.warn('--prompt is only honored for BASH at this time')
    elif args.prompt:
        tty.warn('--prompt ignored when cmd is specified')

    environment = load_environment(args.package)
    if args.cd:
        os.chdir(os.path.join(os.environ['SPACKDEV_BASE'],
                              'build', args.package))
    tty.msg('executing {0} in environment for package {1} in directory {2}'.
            format(' '.join(args.cmd), args.package, os.getcwd()))
    os.execvpe(args.cmd[0], args.cmd, environment)
