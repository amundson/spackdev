#!/usr/bin/env python
from __future__ import print_function

import argparse
import copy
import os
import re
import tempfile

from llnl.util import tty

import fnal.spack.dev as dev
from fnal.spack.dev.environment import bootstrap_environment, \
    sanitized_environment, environment_from_pickle

description = "run a command in the build environment of a spackdev package, or start a shell in same."


def load_environment(package):
    package_env_file_name\
        = os.path.join(os.environ['SPACKDEV_BASE'],
                       dev.spackdev_aux_packages_subdir,
                       package,
                       'env',
                       'env.pickle')
    if not os.path.exists(package_env_file_name):
        tty.die('unable to find environment for {0}: not a package being developed?'.format(package))
    environment = copy.copy(os.environ)
    environment.update(sanitized_environment
                       (environment_from_pickle(package_env_file_name)))
    return environment


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
    subparser.add_argument('package', action='store', default=None,
                           help='package for which to initialize environment.')
    subparser.add_argument('cmd', nargs=argparse.REMAINDER,
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
