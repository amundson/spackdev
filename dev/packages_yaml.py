#!/usr/bin/env python

from __future__ import print_function

import os.path
import shutil
import spack.architecture

class Packages_yaml:
    def __init__(self):
        self.platform = spack.architecture.platform()
        self.filename = os.path.join(os.path.expanduser('~'),
                                     '.spack/', self.platform,
                                     'packages.yaml')
        self.pre_lines = []
        self.post_lines = []
        self.delimiter = '## spackdev findext: '
        self.delim_len = len(self.delimiter)
        self.begin = 'begin'
        self.end = 'end'
        self.indent = '    '
        self.external_packages = {}
        self.read_file()

    def read_file(self):
        self.pre_lines = []
        self.post_lines = []
        if os.path.exists(self.filename):
            f = open(self.filename, 'r')
            begin_found = False
            end_found = False
            for line in f.readlines():
                if line[:self.delim_len] == self.delimiter:
                    # print('jfa: findext line: "{0}"'.format(line.rstrip()))
                    rest = line[self.delim_len:].rstrip()
                    if rest == self.begin:
                        # print('jfa: begin found')
                        begin_found = True
                    elif rest == self.end:
                        # print('jfa: end found')
                        end_found = True
                    else:
                        pass
                else:
                    if begin_found:
                        if end_found:
                            self.post_lines.append(line)
                    else:
                        self.pre_lines.append(line)

    def write_file(self, external_packages):
        if os.path.exists(self.filename):
            backup_filename = self.filename + '.findext'
            # print('jfa: copy {0} {1}'.format(self.filename, backup_filename))
            shutil.copy(self.filename, backup_filename)
        dirname = os.path.dirname(self.filename)
        if not os.path.exists(dirname):
            os.makedirs(dirname)
        f = open(self.filename, 'w')
        for line in self.pre_lines:
            f.write(line)
        self.write_packages_declaration(f)
        f.write(self.delimiter + self.begin + '\n')
        self.write_external_packages(f, external_packages)
        f.write(self.delimiter + self.end + '\n')
        for line in self.post_lines:
            f.write(line)

    def write_packages_declaration(self, outfile):
        need_packages_decl = True
        for line in self.pre_lines:
            if line.lstrip().rstrip() == 'packages:':
                need_packages_decl = False
        if need_packages_decl:
            outfile.write('packages:\n')

    def write_external_packages(self, outfile, external_packages):
        packages = external_packages.keys()
        packages.sort()
        for package in packages:
            external_package = external_packages[package]
            outfile.write(self.indent + package + ':\n')
            outfile.write(self.indent + self.indent + 'paths:\n')
            outfile.write(self.indent + self.indent + self.indent + package)
            if external_package.version:
                outfile.write('@' + external_package.version)
            outfile.write(': ' + external_package.pathname + '\n')

        def add_external_package(self, external_package):
            self.external_packages[external_package.name] = external_package
