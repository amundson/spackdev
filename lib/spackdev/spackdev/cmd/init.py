#!/usr/bin/env python

import argparse
from spackdev import utils
from spackdev.spack import tty
from spackdev.spack import yaml
# from spackdev.spack import Spec
import re
import glob
import os
import copy
import shutil
import stat
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
        self.all_packages = []

    def add(self, package, dependency):
        if type(dependency) == list:
            for item in dependency:
                self.add(package, item)
        else:
            if not self.deps.has_key(package):
                self.deps[package] = [dependency]
                self._append_unique(package, self.all_packages)
                self._append_unique(dependency, self.all_packages)
            if dependency and (not dependency in self.deps[package]):
                self.deps[package].append(dependency)
                self._append_unique(dependency, self.all_packages)

    def get_dependencies(self, package):
        if self.deps.has_key(package):
            retval = self.deps[package]
        else:
            retval = []
        return retval

    def _append_unique(self, item, the_list):
        if type(item) == list:
            for subitem in item:
                # print 'wtf calling with', subitem
                self._append_unique(subitem, the_list)
        elif (not item in the_list) and (not item == []):
            the_list.append(item)

    def get_all_dependencies(self, package, retval = []):
        for subpackage in self.get_dependencies(package):
            self._append_unique(subpackage, retval)
            for subsubpackage in self.get_dependencies(subpackage):
                self._append_unique(self.get_dependencies(subsubpackage), retval)
        return retval

    def get_all_packages(self):
        return self.all_packages

    def has_dependency(self, package, other_packages):
        retval = False
        for other in other_packages:
            if other in self.get_dependencies(package):
                retval = True
        return retval


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
        raise RuntimeError("extract_stage_dir_from_output: failed to find stage_dir")

def stage(packages):
    for package in packages:
        stage_py_filename = os.path.join('spackdev', package, 'bin', 'stage.py')
        retval = os.system(stage_py_filename)

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
    for sub_spec in super_specs[0]['spec']:
        key = sub_spec.keys()[0]
        value = sub_spec[key]
        specs[key] = value
    return specs

def extract_specs(packages):
    cmd = ['spec', '--yaml']
    cmd.extend(packages)
    status, output = utils.spack_cmd(cmd)
    specs = yaml_to_specs(output)
    return specs

def get_all_dependencies(packages):
    dependencies = Dependencies()
    specs = extract_specs(packages)
    for name in specs.keys():
        if specs[name].has_key('dependencies'):
            package_dependencies = specs[name]['dependencies'].keys()
        else:
            package_dependencies = []
        dependencies.add(name, package_dependencies)
    for package in dependencies.get_all_packages():
        package_dependencies = dependencies.get_dependencies(package)
    return dependencies

def get_additional(requesteds, dependencies):
    additional = []
    for package in dependencies.get_all_packages():
        package_dependencies = dependencies.get_dependencies(package)
        for requested in requesteds:
            if requested in package_dependencies:
                append_unique(package, additional)
    return additional

def init_cmakelists(project='spackdev'):
    f = open(os.path.join('spackdev', 'CMakeLists.txt'), 'w')
    f.write(
'''cmake_minimum_required(VERSION 2.8.8)
project({})
set(SPACKDEV_SOURCE_DIR "{}")
'''.format(project, os.getcwd()))
    return f

def add_package_to_cmakelists(cmakelists, package, dependencies):

    cmakelists.write(
'''
# {package}
file(MAKE_DIRECTORY ${{CMAKE_BINARY_DIR}}/tags/{package})
file(MAKE_DIRECTORY ${{CMAKE_BINARY_DIR}}/{package})

add_custom_command(OUTPUT ${{CMAKE_BINARY_DIR}}/tags/{package}/cmake
  COMMAND cmake
      -G Ninja
      -DCMAKE_INSTALL_PREFIX=${{CMAKE_BINARY_DIR}}/install
      ${{SPACKDEV_SOURCE_DIR}}/{package} && touch ${{CMAKE_BINARY_DIR}}/tags/{package}/cmake
  WORKING_DIRECTORY ${{CMAKE_BINARY_DIR}}/{package}
'''.format(package=package))

    for dependency in dependencies:
        cmakelists.write("  DEPENDS ${{CMAKE_BINARY_DIR}}/tags/{dependency}/install\n".
                         format(dependency=dependency))

    cmakelists.write(
''')

add_custom_target(tags_{package}_cmake
  DEPENDS ${{CMAKE_BINARY_DIR}}/tags/{package}/cmake)

set_source_files_properties(
  ${{CMAKE_BINARY_DIR}}/tags/{package}/cmake
  PROPERTIES GENERATED TRUE
)

add_custom_command(OUTPUT ${{CMAKE_BINARY_DIR}}/tags/{package}/ninja
  COMMAND ninja && touch ${{CMAKE_BINARY_DIR}}/tags/{package}/ninja_dummy
  WORKING_DIRECTORY ${{CMAKE_BINARY_DIR}}/{package}
  DEPENDS ${{CMAKE_BINARY_DIR}}/tags/{package}/cmake
)

add_custom_target(tags_{package}_ninja
DEPENDS ${{CMAKE_BINARY_DIR}}/tags/{package}/ninja)
add_dependencies(tags_{package}_ninja tags_{package}_cmake)

set_source_files_properties(
  ${{CMAKE_BINARY_DIR}}/tags/{package}/ninja
  PROPERTIES GENERATED TRUE
)

add_custom_command(OUTPUT ${{CMAKE_BINARY_DIR}}/tags/{package}/install
  COMMAND ninja install && touch ${{CMAKE_BINARY_DIR}}/tags/{package}/install_dummy
  WORKING_DIRECTORY ${{CMAKE_BINARY_DIR}}/{package}
  DEPENDS ${{CMAKE_BINARY_DIR}}/tags/{package}/ninja
)

add_custom_target(tags_{package}_install
  ALL
  DEPENDS ${{CMAKE_BINARY_DIR}}/tags/{package}/install)

add_dependencies(tags_{package}_install tags_{package}_ninja)

set_source_files_properties(
  ${{CMAKE_BINARY_DIR}}/tags/{package}/install
  PROPERTIES GENERATED TRUE
)

'''.format(package=package))

def write_cmakelists(packages, dependencies):
    cmakelists = init_cmakelists()
    remaining_packages = copy.copy(packages)
    while remaining_packages != []:
        for package in remaining_packages:
            if not dependencies.has_dependency(package, remaining_packages):
                package_dependencies = []
                for dependency in dependencies.get_dependencies(package):
                    if dependency in packages:
                        package_dependencies.append(dependency)
                add_package_to_cmakelists(cmakelists, package, package_dependencies)
                remaining_packages.remove(package)

def get_environment(package):
    environment = []
    status, output = utils.spack_cmd(["env", package])
    variables = ['CC', 'CXX', 'F77', 'FC', 'CMAKE_PREFIX_PATH', 'PATH']
    for line in output.split('\n'):
        for variable in variables:
            s_var = re.match('^{}=.*'.format(variable), line)
            if s_var:
                environment.append(line)
        s_spack = re.match('^SPACK_.*=.*', line)
        if s_spack:
            environment.append(line)
    environment.sort()
    return environment

def copy_modified_script(source, dest, environment):
    infile = open(source, 'r')
    outfile = open(dest, 'w')

    # copy hash bang line
    line = infile.readline()
    outfile.write(line)

    # insert select variables
    outfile.write('# begin SpackDev variables\n')
    for pair in environment:
        s = re.match('([a-zA-Z0-9_]*)=(.*)', pair)
        if s:
            var = s.group(1)
            value = s.group(2)
            if var in ['CMAKE_PREFIX_PATH', 'PATH']:
                outfile.write(pair + '\n')
                outfile.write('export ' + var + '\n')
            s_spack = re.match('^SPACK_.*', var)
            if s_spack:
                outfile.write(pair + '\n')
                outfile.write('export ' + var + '\n')
        # else:
        #     print "jfa: failed (again?) to parse environment line:"
        #     print pair
    outfile.write('# end SpackDev variables\n')

    # copy the rest
    for line in infile.readlines():
        outfile.write(line)
    outfile.close()
    os.chmod(dest, 0755)


def create_wrappers(package, environment):
    # print 'jfa start create_wrappers'
    wrappers_dir = os.path.join('spackdev', package, 'bin')
    # wrappers_dir = os.path.join('env', package, 'bin')
    if not os.path.exists(wrappers_dir):
        os.makedirs(wrappers_dir)
    for index in range(0, len(environment)):
        s = re.match('([a-zA-Z0-9_]*)=(.*)', environment[index])
        if s:
            var = s.group(1)
            value = s.group(2)
            if var in ['CC', 'CXX', 'F77', 'FC']:
                if value[0] == "'" and value[-1] == "'":
                    value = value[1:-1]
                filename = os.path.basename(value)
                dest = os.path.join(wrappers_dir, filename)
                copy_modified_script(value, dest, environment)
        # else:
        #     print 'jfa: failed to parse environment line:'
        #     print environment[index]
    # print 'jfa end create wrappers'

def create_env_sh(package, environment):
    env_dir = os.path.join('spackdev', package, 'env')
    if not os.path.exists(env_dir):
        os.makedirs(env_dir)
    pathname = os.path.join(env_dir, 'env.sh')
    # pathname = os.path.join('env', package, 'env.sh')
    outfile = open(pathname, 'w')
    for line in environment:
        outfile.write(line + '\n')

def create_stage_script(package):
    bin_dir = os.path.join('spackdev', package, 'bin')
    if not os.path.exists(bin_dir):
        os.makedirs(bin_dir)
    status, output = utils.spack_cmd(["export-stage", package])
    output_lines = output.split('\n')
    # print 'jfa: output_lines =',output_lines
    # print 'jfa: output_lines[1] =', output_lines[1]
    method = output_lines[0]
    dict_str = output_lines[1]
    stage_py_filename = os.path.join(bin_dir, 'stage.py')
    stage_py = open(stage_py_filename, 'w')
    stage_py.write('''#!/usr/bin/env python
import os
import sys
def stage(package, method, the_dict):
    if method == 'GitFetchStrategy':
        cmd = 'git clone ' + the_dict['url'] + ' ' + package
        retval = os.system(cmd)
        if retval != 0:
            sys.stderr.write('"' + cmd + '" failed\\n')
            sys.exit(retval)
        os.chdir(package)
        if the_dict['tag']:
            cmd = 'git checkout ' + the_dict['tag']
            retval = os.system(cmd)
            if retval != 0:
                sys.stderr.write('"' + cmd + '" failed\\n')
                sys.exit(retval)
    else:
        sys.stderr.write('SpackDev stage.py does not yet handle sources of type ' + method)
        sys.exit(1)
    ''')
    stage_py.write('''
if __name__ == '__main__':
    package = "''')
    stage_py.write(package)
    stage_py.write('''"
    method = "''')
    stage_py.write(output_lines[0])
    stage_py.write('''"
    the_dict = ''')
    stage_py.write(dict_str)
    stage_py.write('''
    stage(package, method, the_dict)
    ''')
    stage_py.close()
    stage_py_st = os.stat(stage_py_filename)
    os.chmod(stage_py_filename, stage_py_st.st_mode | stat.S_IEXEC)

def create_environment(packages):
    pkg_environments = {}
    for package in packages:
        environment = get_environment(package)
        pkg_environments[package] = environment
        # print package,':'
        # for line in environment:
        #     print line
        create_wrappers(package, environment)
        create_env_sh(package, environment)
        create_stage_script(package)
    return pkg_environments

def extract_build_step_scripts(package, dry_run_filename):
    # f = open(dry_run_filename, 'r')
    # lines = f.readlines()
    # f.close()
    steps = utils.read_all_csv_lists(dry_run_filename)
    # print 'jfa: found', len(steps),'build steps'
    wrappers_dir = os.path.join('spackdev', package, 'bin')
    # wrappers_dir = os.path.join('env', package, 'bin')

def extract_short_spec(package, pkg_environements):
    retval = None
    for line in pkg_environements[package]:
        s = re.match('^SPACK_SHORT_SPEC=(.*)', line)
        if s:
            rhs = s.group(1)
            s2 = re.match('(.*)=', rhs)
            retval = s2.group(1)
    return retval

def create_build_scripts(packages, pkg_environments):
    for package in packages:
        os.chdir(package)
        short_spec = extract_short_spec(package, pkg_environments)
        status, output = utils.spack_cmd(["diy", "--dry-run-file", "spackdev.out",
                                          short_spec])
        os.chdir("..")
        extract_build_step_scripts(package, os.path.join(package, "spackdev.out"))

def write_packages_file(requesteds, additional):
    packages_filename = os.path.join('spackdev', 'packages.sd')
    with open(packages_filename, 'w') as f:
        f.write(str(requesteds) + '\n')
        f.write(str(additional) + '\n')

def create_build_area():
    os.mkdir('build')
    os.chdir('build')
    os.system('cmake ../spackdev -GNinja')

def setup_parser(subparser):
    subparser.add_argument('packages', nargs=argparse.REMAINDER,
                           help="specs of packages to add to SpackDev area")
    subparser.add_argument('-s', '--no-stage', action='store_true', dest='no_stage',
        help="do not stage packages")


def init(parser, args):
    dir = os.getcwd()
    if (not os.path.exists(dir)):
        os.makedirs(dir)
    os.chdir(dir)
    if (os.path.exists('spackdev')) :
        tty.die('spackdev init: cannot re-init (spackdev directory exists)')
    os.mkdir('spackdev')

    requesteds = args.packages
    tty.msg('requested packages: ' + str(' '.join(requesteds)))
    all_dependencies = get_all_dependencies(requesteds)
    additional = get_additional(requesteds, all_dependencies)
    if additional:
        tty.msg('additional inter-dependent packages: ' + str(' '.join(additional)))
    write_packages_file(requesteds, additional)
    all_packages = requesteds + additional

    write_cmakelists(all_packages, all_dependencies)

    tty.msg('creating wrapper scripts')
    pkg_environments = create_environment(all_packages)

    if not args.no_stage:
        stage(all_packages)

    #create_build_scripts(all_packages, pkg_environments)
    tty.msg('creating build area')
    create_build_area()
    