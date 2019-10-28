#!/usr/bin/env python

from __future__ import print_function

from ..external_repo import External_repo
from ..packages_yaml import Packages_yaml

description = "search for system packages and add to packages.yaml"


def setup_parser(subparser):
    pass
    # subparser.add_argument('pathname', nargs=argparse.REMAINDER,
    #                        help="pathname of SpackDev area")
    # subparser.add_argument('-s', '--no-stage', action='store_true', dest='no_stage',
    #     help="do not stage packages")

def findext(parser, args):
    external_repo = External_repo()
    packages_yaml = Packages_yaml()
    packages_yaml.write_file(external_repo.all_external_packages())
