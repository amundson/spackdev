#!/usr/bin/env python
from __future__ import print_function

import argparse
from spackdev import spack_cmd, external_cmd
from spackdev import stage_packages, install_dependencies, \
    environment_from_pickle, sanitized_environment
from spackdev.spack_import import tty, yaml, \
    dump_environment, pickle_environment, env_var_to_source_line
from spackdev import which

# from spackdev.spack import Spec
import cPickle
import copy
import glob
import os
import re
import shutil
import stat
import subprocess
import sys


description = "initialize a spackdev area"


def append_unique(item, the_list):
    if type(item) == list:
        for subitem in item:
            append_unique(subitem, the_list)
    elif (not item in the_list) and (not item == []):
        the_list.append(item)


class Dependencies:
    def __init__(self):
        self.deps = {}
        self.all_packages = {}

    def add(self, package, spec, dependencies):
        if not self.deps.has_key(package):
            self.deps[package] = dependencies.keys()
        else:
            append_unique(dependencies.keys(), self.deps[package])
        self.all_packages[package] = spec
        self.all_packages.update\
            ({key: val for (key, val) in dependencies.iteritems() if not
              self.all_packages.has_key(key)})

    def get_dependencies(self, package):
        if self.deps.has_key(package):
            retval = self.deps[package]
        else:
            retval = []
        return retval

    def get_all_dependencies(self, package, retval = None):
        if retval is None:
            retval = []
        for subpackage in self.get_dependencies(package):
            append_unique(subpackage, retval)
            self.get_all_dependencies(subpackage, retval)
        return retval

    def get_all_packages(self):
        return self.all_packages

    def package_info(self, package):
        try:
            return self.all_packages[package]
        except KeyError:
            tty.die('unable to obtain package information for {0}'.format(package))

    def has_dependency(self, package, other_packages):
        for other in other_packages:
            if other in self.get_dependencies(package):
                return True
        else:
            return False


class Build_system:
    def __init__(self, system_type):
        self.label = system_type
        if system_type == 'make':
            self.build_command = 'make'
            self.cmake_label = '"Unix Makefiles"'
        elif system_type == 'ninja':
            if which('ninja'):
                self.build_command = 'ninja'
            elif which('ninja-build'):
                self.build_command = 'ninja-build'
            else:
                tty.msg('warning: ninja build selected, but neither "ninja" nor "ninja-build" are available')
                self.build_command = 'ninja'
            self.cmake_label = 'Ninja'
        else:
            tty.die('Build_system: must be either "make" or "ninja"')


class PathFixer:
    """Class to handle the (relatively) efficient replacement of spack stage
    and install prefixes with their SpackDev equivalents where
    appropriate.
    """
    def __init__(self, spack_install, spack_stage):
        self.spack_install = spack_install
        self.spack_stage = spack_stage
        self.spackdev_install = os.path.join(os.getcwd(), 'build', 'install')
        self.spackdev_stage = os.path.join(os.getcwd(), 'build')

    def set_packages(self, *args):
        # Replace all stage and insatll paths for packages we're
        # developing with their correct locations.
        raw_matcher = r'(?:(?<=[=\s;:"\'])|^){{path}}/(?:[^;:\"]*?/)*?(?P<pkg>{0})-[^;:"\'/]*{{extra}}'.\
                     format('|'.join(args))
        self.install_path_finder\
            = re.compile(raw_matcher.format(path=self.spack_install, extra=''))
        self.stage_path_finder\
            = re.compile(raw_matcher.format(path=self.spack_stage, extra='/spack-build'))

    def fix(self, path):
        result = self.install_path_finder.sub(os.path.join(self.spackdev_install, r'\g<pkg>'), path)
        result = self.stage_path_finder.sub(os.path.join(self.spackdev_stage, r'\g<pkg>'), result)
        return result


def extract_stage_dir_from_output(output, package):
    stage_dir = None
    for line in output.split('\n'):
        s = re.search('.*stage.*in (.*)', line)
        if s:
            stage_dir = s.group(1)
    if stage_dir:
        real_dir = glob.glob(os.path.join(stage_dir, '*'))[0]
        parent = os.path.dirname(stage_dir)
        os.rename(real_dir, os.path.join(parent, package))
        shutil.rmtree(stage_dir)
    else:
        tty.die("extract_stage_dir_from_output: failed to find stage_dir")


def yaml_to_specs(yaml_text):
    documents = []
    document = ''
    for line in yaml_text.split('\n'):
        if line == 'spec:':
            if len(document) > 0:
                documents.append(document)
            document = 'spec:\n'
        else:
            document += line + '\n'
    if len(document) > 0:
        documents.append(document)
    super_specs = map(yaml.load, documents)
    specs = {}
    for spec in super_specs:
        for sub_spec in spec['spec']:
            specs.update({key: value for (key, value) in sub_spec.iteritems() if key not in specs})
    return specs


def extract_specs(spec_source):
    cmd = ['spec', '--yaml']
    if type(spec_source) == list:
        # List of packages.
        cmd.extend(spec_source)
    else:
        # File containing spack install specification.
        with open(spec_source, 'r') as dag_file:
            cmd.append(dag_file.read().rstrip())
    status, output = spack_cmd(cmd)
    specs = yaml_to_specs(output)
    return specs


def calculate_dependencies(specs):
    dependencies = Dependencies()
    for name in specs.keys():
        if specs[name].has_key('dependencies'):
            spec_deps = specs[name]['dependencies']
            package_dependencies \
                = {d: spec_deps[d] for d in spec_deps.keys()}
        else:
            package_dependencies = {}
        dependencies.add(name, specs[name], package_dependencies)
    return dependencies


def get_additional(requested, dependencies):
    additional = []
    for package in requested:
        append_unique([dep for dep in
                       dependencies.get_all_dependencies(package) if
                       dep not in requested and
                       dependencies.has_dependency(dep, requested)],
                      additional)
    return additional


def init_cmakelists(project='spackdev'):
    f = open(os.path.join('spackdev', 'CMakeLists.txt'), 'w')
    f.write(
        '''cmake_minimum_required(VERSION ${{CMAKE_VERSION}})
project({0} NONE)
set(SPACKDEV_SOURCE_DIR "{1}")

include(ExternalProject)

set_property(DIRECTORY PROPERTY EP_STEP_TARGETS
             configure build install test
  )
'''.format(project, os.getcwd()))
    return f


gen_arg = re.compile(r'-G(.*)')
def add_package_to_cmakelists(cmakelists, package, package_dependencies,
                              cmake_args, build_system):

    cmd_wrapper\
        = lambda x : os.path.join(os.getcwd(), 'spackdev', package, 'bin', x)

    filtered_cmake_args = []
    cmake_generator = build_system.cmake_label

    gen_next = None

    for arg in cmake_args:
        if gen_next:
            gen_next = None
            cmake_generator = arg
            continue
        else:
            gen_match = gen_arg.match(arg)
            if gen_match:
                if gen_match.group(1):
                    cmake_generator = gen_match.group(1)
                else:
                    gen_next = True
            else:
                filtered_cmake_args.append(arg)

    cmake_args_string\
        = ' '.join([ '"{0}"'.format(arg) for arg in filtered_cmake_args])

    cmake_args_string = cmake_args_string.replace(';', '|')

    cmakelists.write(
'''
# {package}
file(MAKE_DIRECTORY tmp/{package})
file(MAKE_DIRECTORY {package})

ExternalProject_Add({package}
  TMP_DIR "tmp/{package}"
  STAMP_DIR "tmp/{package}/stamp"
  DOWNLOAD_DIR "tmp/{package}"
  SOURCE_DIR "${{SPACKDEV_SOURCE_DIR}}/{package}"
  BINARY_DIR "{package}"
  INSTALL_DIR "install/{package}"
  CMAKE_COMMAND "{cmake_wrapper}"
  CMAKE_GENERATOR "{cmake_generator}"
  CMAKE_ARGS {cmake_args}
  BUILD_ALWAYS TRUE
  TEST_EXCLUDE_FROM_MAIN TRUE
  LIST_SEPARATOR "|"
  DEPENDS {package_dependency_targets}
  )
'''.format(package=package,
           cmake_wrapper=cmd_wrapper('cmake'),
           cmake_args=cmake_args_string,
           cmake_generator=cmake_generator,
           package_dependency_targets=' '.join(package_dependencies)))


cmake_args_start = re.compile(r'\[cmake-args\s+([^\]]+)\]')
cmake_args_end = re.compile(r'\[/cmake-args\]')
def extract_cmake_args(packages, install_args):
    retval, output = spack_cmd(['install', '--fake', '--only', 'package',
                                install_args])
    package_cmake_args = {}
    current_package = None
    current_package_args = []
    for line in output.splitlines():
        start_match = cmake_args_start.match(line)
        if start_match:
            current_package = start_match.group(1)
        elif current_package:
            end_match = cmake_args_end.match(line)
            if end_match:
                package_cmake_args[current_package] = current_package_args
                current_package = None
                current_package_args = []
            else:
                current_package_args.append(line)
    missing_packages = []
    for package in packages:
        if package not in package_cmake_args:
            missing_packages.append(package)

    retval, output = spack_cmd(['uninstall', '-y', install_args])
    if missing_packages:
        tty.die('unable to ascertain CMake arguments for packages: {0}'.
                format(' '.join(missing_packages)))

    return package_cmake_args


def write_cmakelists(packages, install_args, all_dependencies,
                     build_system, path_fixer):
    package_cmake_args = extract_cmake_args(packages, install_args)
    cmakelists = init_cmakelists()
    remaining_packages = copy.copy(packages)
    while remaining_packages != []:
        for package in remaining_packages:
            if not all_dependencies.has_dependency(package, remaining_packages):
                package_dependencies = []
                for dependency in all_dependencies.get_dependencies(package):
                    if dependency in packages:
                        package_dependencies.append(dependency)
                # Fix install / stage paths.
                path_fixer.set_packages(package, *package_dependencies)
                package_cmake_args[package]\
                    = [path_fixer.fix(val) for val in \
                       package_cmake_args[package]]
                add_package_to_cmakelists(cmakelists, package,
                                          package_dependencies,
                                          package_cmake_args[package],
                                          build_system)
                remaining_packages.remove(package)


def spack_stage_top():
    retval, output = spack_cmd(['location', '-S'])
    return output


def par_val_to_string(par, val):
    if type(val) == list:
        retval = ' {0}={1}'.format(par, ' '.join(val)) if val else ''
    elif type(val) == tuple:
        retval = ' {0}={1}'.format(par, ' '.join(val)) if val else ''
    elif type(val) == bool:
        retval = '+{0}'.format(par) if val else '~{0}'.format(par)
    elif type(val) == None:
        retval = ''
    else:
        retval = ' {0}={1}'.format(par, val)
    return retval


def install_args_for_package(package, all_dependencies):
    package_info = all_dependencies.package_info(package)
    version = '@{0}'.format(package_info['version'])
    compiler = '%{0}@{1}'.format(package_info['compiler']['name'],
                                 package_info['compiler']['version']) \
        if package_info.has_key('compiler') else ''

    pars = package_info['parameters']
    bool_args = []
    other_args = []
    for (par, val) in pars.iteritems():
        parstring = par_val_to_string(par, val)
        if parstring:
            alist = bool_args if parstring.startswith(('+', '~')) else \
                    other_args
            alist.append(parstring)
    return [package, version, compiler] + bool_args + other_args


def format_packages_for_install(packages, all_dependencies):
    install_args = []
    for package in packages:
        install_args.append\
            (''.join(install_args_for_package(package, all_dependencies)))
        install_args.extend\
            (["^{0}".format\
              (''.join(install_args_for_package(dep,
                                                all_dependencies)))
              for dep in all_dependencies.get_all_dependencies(package)])
    return install_args


def get_environment(package, all_dependencies):
    package_env_file_name = '{0}-environment.pickle'.format(package)
    status, output \
        = spack_cmd(['env', '--pickle'] +
                    format_packages_for_install([package], all_dependencies) +
                    ['--', package_env_file_name])
    environment = environment_from_pickle(package_env_file_name)
    os.remove(package_env_file_name)
    # This needs to be what we want it to be.
    environment['SPACK_PREFIX'] = os.path.join(os.getcwd(), 'build', 'install')
    return sanitized_environment(environment, drop_unchanged=True)


def copy_modified_script(source, dest, environment):
    infile = open(source, 'r')
    outfile = open(dest, 'w')

    # copy hash bang line
    line = infile.readline()
    outfile.write(line)

    # insert select variables
    outfile.write('\n# begin SpackDev variables\n')
    for var, value in sorted(environment.iteritems()):
        if var in ['CMAKE_PREFIX_PATH', 'PATH'] or re.match('^SPACK_.*', var):
            outfile.write('{0}\n'.format(env_var_to_source_line(var, value)))
    outfile.write('# end SpackDev variables\n\n')

    # copy the rest
    for line in infile.readlines():
        outfile.write(line)
    outfile.close()
    os.chmod(dest, 0755)


def create_compiler_wrappers(wrappers_dir, environment):
    for var, value in sorted(environment.iteritems()):
        if var in ['CC', 'CXX', 'F77', 'FC']:
            if value[0] == "'" and value[-1] == "'":
                # It's been quoted: we need the shell to unquote it.
                value=subprocess.call("echo {0}".format(value), shell=True)
            filename = os.path.basename(value)
            dest = os.path.join(wrappers_dir, filename)
            environment[var] = os.path.join(os.getcwd(), dest)
            copy_modified_script(value, dest, environment)


def create_cmd_wrappers(wrappers_dir, environment):
    for cmd in ['cmake', 'ctest', 'make', 'ninja']:
        filename = os.path.join(wrappers_dir, cmd) 
        with open(filename, 'w') as f:
            f.write('#!/bin/bash\n')
            f.write('\n# begin spack variables\n')
            for var, value in sorted(environment.iteritems()):
                f.write('{0}\n'.format(env_var_to_source_line(var, value)))
            f.write('# end spack variables\n\n')
            f.write('exec {0} "$@"\n'.format(cmd))
        os.chmod(filename, 0755)


def create_wrappers(package, environment):
    # print 'jfa start create_wrappers'
    wrappers_dir = os.path.join('spackdev', package, 'bin')
    if not os.path.exists(wrappers_dir):
        os.makedirs(wrappers_dir)
    create_compiler_wrappers(wrappers_dir, environment)
    create_cmd_wrappers(wrappers_dir, environment)


def create_env_files(env_dir, environment):
    if not os.path.exists(env_dir):
        os.makedirs(env_dir)
    # Write a human-readable file.
    with open(os.path.join(env_dir, 'env.txt'), 'w') as outfile:
        for var, value in sorted(environment.iteritems()):
            outfile.write('{0}={1}\n'.format(var, value))
    # Write a source-able file.
    dump_environment(os.path.join(env_dir, 'env.sh'), environment)
    # Write a pickled file.
    pickle_environment(os.path.join(env_dir, 'env.pickle'), environment)


def create_environment(packages, all_dependencies):
    path_fixer = None
    for package in packages:
        tty.msg('creating environment for {0}'.format(package))
        environment = get_environment(package, all_dependencies)
        if path_fixer is None:
            path_fixer = PathFixer(environment['SPACK_INSTALL'], spack_stage_top())
        # Fix paths in environment
        path_fixer.set_packages(*packages)
        environment = {var: path_fixer.fix(val) for var, val in
                       environment.iteritems()}
        create_wrappers(package, environment)
        create_env_files(os.path.join('spackdev', package, 'env'), environment)
    create_env_files('spackdev', sanitized_environment(os.environ))
    return path_fixer


def write_packages_file(requested, additional, all_dependencies):
    packages_filename = os.path.join('spackdev', 'packages.sd')
    install_args = ''
    with open(packages_filename, 'w') as f:
        f.write(' '.join(requested) + '\n')
        f.write(' '.join(additional) + '\n')
        install_args\
            = ' '.join(format_packages_for_install(requested + additional,
                                                   all_dependencies))
        f.write(install_args + '\n')
    return install_args

def create_build_area(build_system):
    os.mkdir('build')
    os.chdir('build')
    status, output\
        = external_cmd(['cmake', '../spackdev',
                        '-G {}'.format(build_system.cmake_label)])
    tty.msg(output)

def setup_parser(subparser):
    subparser.add_argument('packages', nargs=argparse.REMAINDER,
                           help="specs of packages to add to SpackDev area")
    subparser.add_argument('--dag-file',
                           help='packages and dependencies should be inferred from the DAG specified in this text file (in "spack install" format)')
    subparser.add_argument('-d', '--no-dependencies', action='store_true', dest='no_dependencies',
                           help="do not have spack install dependent packages")
    subparser.add_argument('-m', '--make', action='store_true', dest='make',
        help="use make instead of ninja")
    subparser.add_argument('-s', '--no-stage', action='store_true', dest='no_stage',
        help="do not stage packages")


def init(parser, args):
    spackdev_base = os.getcwd()
    # Save for posterity
    os.environ['SPACKDEV_BASE'] = spackdev_base
    if (not os.path.exists(spackdev_base)):
        os.makedirs(spackdev_base)
    os.chdir(spackdev_base)

    if (os.path.exists('spackdev')) :
        tty.die('spackdev init: cannot re-init (spackdev directory exists)')
    os.mkdir('spackdev')

    requested = args.packages
    dag_filename = args.dag_file
    tty.msg('requested packages: {0}{1}'.\
            format(', '.join(requested),
                   ' from DAG as specified in {0}'.format(dag_filename)
                   if dag_filename else ''))
    specs = extract_specs(dag_filename if dag_filename else requested)
    all_dependencies = calculate_dependencies(specs)
    additional = get_additional(requested, all_dependencies)
    if additional:
        tty.msg('additional inter-dependent packages: ' +
                ' '.join(additional))
    dev_packages = requested + additional
    install_args = write_packages_file(requested, additional, all_dependencies)

    if not args.no_dependencies:
        tty.msg('install dependencies')
        (retval, output) = install_dependencies(dev_packages=dev_packages,
                                                install_args=install_args)

    if args.make:
        build_system = Build_system('make')
    else:
        build_system = Build_system('ninja')

    tty.msg('create wrapper scripts')
    path_fixer = create_environment(dev_packages, all_dependencies)

    tty.msg('generate top level CMakeLists.txt')
    write_cmakelists(dev_packages, install_args,
                     all_dependencies, build_system, path_fixer)

    if not args.no_stage:
        tty.msg('stage sources for {0}'.format(dev_packages))
        stage_packages(dev_packages)

    tty.msg('create and initialize build area')
    create_build_area(build_system)
