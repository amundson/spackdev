# -*- coding: utf-8 -*-
from __future__ import print_function

import argparse
import copy
import os
import re
import shutil
import subprocess
import sys
from collections import deque

import spack.store  # For spack.store.root to replace SPACK_INSTALL
from llnl.util import filesystem
from llnl.util import tty
from six.moves import shlex_quote as cmd_quote

from ..environment import sanitized_environment, srcs_topdir, load_environment
from ..subcommands import DevPackageInfo

if sys.version_info[0] > 3 or \
   (sys.version_info[0] == 3 and sys.version_info[1] > 2):
    # Available from Python 3.3 onwards.
    from shutil import which
else:
    from distutils.spawn import find_executable as which

import spack.build_environment
import spack.concretize
import spack.paths
import spack.spec
from spack.util.environment import \
    dump_environment, pickle_environment, env_var_to_source_line
import spack.util.executable

description = "initialize a spackdev area"
spackdev_base = os.getcwd()


def append_unique(item, the_list):
    if type(item) == list:
        for subitem in item:
            append_unique(subitem, the_list)
    elif (not item in the_list) and (not item == []):
        the_list.append(item)


generator_extractor = re.compile(r'(?:.*?-\s+)?(Ninja|Unix Makefiles)')
class Build_system:
    def __init__(self, generator, override=False):
        primary_generator = generator_extractor.match(generator)
        if not primary_generator:
            tty.die('Build_system: invalid generator {0}--primary generator '
                    'must be either "Unix Makefiles" or "Ninja"'.format(generator))
        self.cmake_generator = generator
        self.label = 'ninja' if primary_generator.group(1) == 'Ninja' else 'make'
        self.build_command = self.label # default
        if self.label == 'ninja':
            if which('ninja'):
                self.build_command = 'ninja'
            elif which('ninja-build'):
                self.build_command = 'ninja-build'
            else:
                tty.msg('warning: ninja build selected, but neither "ninja" '
                        'nor "ninja-build" are available')
        self.override = override


class PathFixer:
    """Class to handle the (relatively) efficient replacement of spack stage
    and install prefixes with their SpackDev equivalents where
    appropriate.
    """
    def __init__(self, spack_install, spack_stage):
        self.spack_install = spack_install
        self.spack_stage = spack_stage
        self.spackdev_install = os.path.join(spackdev_base, 'install')
        self.spackdev_stage = os.path.join(spackdev_base, 'build')

    def set_packages(self, *args):
        # Sort package names by decreasing length to avoid problems with
        # packages that are dash-separated subsets of other packages in
        # the list.
        sorted_args = sorted(args, key=lambda x: -len(x))
        # Replace all stage and install paths for packages we're
        # developing with their correct locations.
        #
        # N.B. this requires that the Spack installation we're
        # installing dependencies, etc. into has a definition of
        # install_path_scheme ending with two directory components, the
        # first of which being the package name. For instance:
        #
        # install_path_scheme: "${ARCHITECTURE}/${COMPILERNAME}-${COMPILERVER}/${PACKAGE}/${VERSION}-${HASH}"
        raw_matcher = r'(?:(?<=[=\s;:"\'])|^){{path}}/(?:[^;:\"]*?/)*?(?P<pkg>{0})/[^;:"\'/]*'.\
                     format('|'.join(sorted_args))
        self.install_path_finder\
            = re.compile(raw_matcher.format(path=self.spack_install))

    def fix(self, path, **kwargs):
        result = self.install_path_finder.sub(os.path.join(self.spackdev_install, r'\g<pkg>'), path)
        if 'build_directory' in kwargs:
            result= re.sub(r'(?:(?<=[=\s;:"\'])|^){0}'.format(kwargs['build_directory']),
                            os.path.join(spackdev_base, 'build', kwargs.get('package_name', '')),
                            result)
        return result


def build_directory_for(package_obj):
    return getattr(package_obj, 'build_directory', package_obj.stage.path)


def extract_specs(spec_source):
    spec_args = []
    if type(spec_source) == list:
        # List of packages.
        spec_args.extend(spec_source)
    else:
        # File containing spack install specification.
        with open(spec_source, 'r') as dag_file:
            spec_args.extend(dag_file.read().rstrip().split())
    # From PR 11158.
    specs = spack.spec.parse(spec_args)
    return spack.concretize.concretize_specs_together(*specs)


def update_deps_with_deps_from(deps, new, specs, all_terminals):
    for tp in new:
        for spec in specs:
            if tp in spec:
                deps.update(spec[tp].flat_dependencies())
                break
    deps.difference_update(all_terminals)


def spec_for(package, specs):
    for spec in specs:
        if package in spec:
            return spec[package]
    return None


def tool_from(tool, package, specs):
    result = None

    package_spec = spec_for(package, specs)

    if package_spec:
        result = os.path.join(package_spec.prefix.bin, tool)
    else:
        tty.info('Unable to find specified tool {0} from package {1} in current dependencies: expect to find in PATH instead.'.format(tool, package))
        result = copy.copy(tool)
    return result


# TODO: This algorithm should be replaced by a more efficient marking
# algorithm that only traverses each spec tree once.
def get_additional(requested, specs):
    to_consider = deque(requested)
    all_done = set(requested)
    deps = set()
    update_deps_with_deps_from(deps, requested, specs, [])
    while len(to_consider):
        package = to_consider.popleft()
        for spec in specs:
            found = set()
            found.update([dep for dep in deps if
                          package in spec and
                          dep in spec[package].dependents_dict()])
            update_deps_with_deps_from(deps, found, specs, all_done)
            all_done.update(found)
            to_consider.extend(found)
    additional = list(all_done.difference(requested))
    tty.debug('get_additional: full list of additional packages: {0}'.format(additional))
    return additional


def init_cmakelists(project='spackdev'):
    f = open(os.path.join('srcs', 'CMakeLists.txt'), 'w')
    f.write(
        '''cmake_minimum_required(VERSION ${{CMAKE_VERSION}})
project({0} NONE)
set(SPACKDEV_PREFIX "{1}")
set(SPACKDEV_SOURCE_DIR "{2}")
set(SPACKDEV_TMPDIR "{3}")

include(ExternalProject)

set_property(DIRECTORY PROPERTY EP_STEP_TARGETS
             configure build install test
  )
'''.format(project,
           os.path.join(spackdev_base, 'install'),
           srcs_topdir(),
           os.path.join(spackdev_base, 'tmp')
       ))
    return f


gen_arg = re.compile(r'-G(.*)')
def add_package_to_cmakelists(cmakelists, package, spec,
                              package_dependencies,
                              cmake_args, build_system):

    cmd_wrapper = lambda x : os.path.join(spackdev_base,
                                          dev.spackdev_aux_packages_subdir,
                                          package, 'bin', x)

    filtered_cmake_args = []
    cmake_generator = build_system.cmake_generator
    generator_label = build_system.label
    generator_cmd = build_system.build_command
    generator_override = build_system.override
    gen_next = None

    for arg in cmake_args:
        if gen_next:
            gen_next = None
            if not generator_override:
                # Use the package's selection for the generator.
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
file(MAKE_DIRECTORY ${{SPACKDEV_TMPDIR}}/{package})
file(MAKE_DIRECTORY {package})

ExternalProject_Add({package}
  TEST_BEFORE_INSTALL ON
  TMP_DIR "${{SPACKDEV_TMPDIR}}/{package}"
  STAMP_DIR "${{SPACKDEV_TMPDIR}}/{package}/stamp"
  DOWNLOAD_DIR "${{SPACKDEV_TMPDIR}}/{package}"
  SOURCE_DIR "${{SPACKDEV_SOURCE_DIR}}/{package}"
  BINARY_DIR "{package}"
  INSTALL_DIR "${{SPACKDEV_PREFIX}}/{package}"
  CMAKE_COMMAND "{cmake_wrapper}"
  TEST_COMMAND "{ctest_wrapper}"
  CMAKE_GENERATOR "{cmake_generator}"
  CMAKE_ARGS {cmake_args}
  BUILD_COMMAND {build_command}
  BUILD_ALWAYS TRUE
  LIST_SEPARATOR "|"
  DEPENDS {package_dependency_targets}
  )
'''.format(package=package,
           cmake_wrapper=cmd_wrapper('cmake'),
           ctest_wrapper=cmd_wrapper('ctest'),
           cmake_generator=cmake_generator,
           build_command='"{0}"'.format(cmd_wrapper(generator_cmd)) if
           generator_label == 'ninja' else
           '"env" "MAKE={0}" "$(MAKE)"'.format(cmd_wrapper('make')),
           cmake_args=cmake_args_string,
           package_dependency_targets=' '.join(package_dependencies)))


class temp_environment:
    def __init__(self, temp_env=None):
        self._temp_env = temp_env
    def __enter__(self):
        self._safe_env = os.environ.copy()
        if (self._temp_env is not None):
            os.environ.clear()
            os.environ.update(self._temp_env)
        return os.environ
    def __exit__(self, type, value, traceback):
        os.environ.clear()
        os.environ.update(self._safe_env)


def extract_cmake_args(dev_packages, dev_package_specs):
    package_cmake_args = {}
    for dp in dev_packages:
        with temp_environment(load_environment(dp)) as environment:
            package_obj = dev_package_specs[dp].package
            try:
                package_cmake_args[dp] = package_obj.std_cmake_args + \
                                              package_obj.cmake_args()
            except Exception as e:
                tty.error('Encountered an error obtaining CMake arguments from package {package}:\n'.format(package=package))
                raise
    return package_cmake_args


def intersection(a, b):
    temp = set(b)
    c = [ val for val in a if val in temp ]
    return c


def write_cmakelists(dev_packages, dev_package_specs,
                     build_system, path_fixer):
    package_cmake_args = extract_cmake_args(dev_packages, dev_package_specs)
    cmakelists = init_cmakelists()
    remaining_packages = copy.copy(dev_packages)
    while remaining_packages != []:
        for dp in remaining_packages:
            spec = dev_package_specs[dp]
            dep_dict = spec.dependencies_dict()
            if not any(other for other in remaining_packages if
                       other in dep_dict):
                # package is a leaf.
                package_dependencies\
                    = intersection(dev_packages, dep_dict.keys())
                # Fix install / stage paths.
                path_fixer.set_packages(dp, *package_dependencies)
                package_cmake_args[dp]\
                    = [path_fixer.fix(val, build_directory=
                                      build_directory_for(spec.package),
                                      package_name=dp) for val in
                       package_cmake_args[dp]]
                add_package_to_cmakelists(cmakelists, dp, spec,
                                          package_dependencies,
                                          package_cmake_args[dp],
                                          build_system)
                remaining_packages.remove(dp)


def spack_stage_top():
    return os.path.join(spackdev_base, dev.spackdev_aux_tmp_subdir)


def par_val_to_string(par, val):
    if type(val) == list:
        retval = ' {0}={1}'.format(par, ','.join(val)) if val else ''
    elif type(val) == tuple:
        retval = ' {0}={1}'.format(par, ','.join(val)) if val else ''
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


def format_dependencies_for_install(packages, all_dependencies):
    """We need to make sure dependencies are installed, while not having
    Spack install anything that we wish to develop, even as an indirect
    dependency. To do this we can leverage the fact that we have already
    identified additional packages for local compilation that are
    required for consistency. This makes our current problem simpler.
    """

    install_args = []
    packages_to_examine = copy.copy(packages)
    packages_to_install = []
    i = 0
    while i < len(packages_to_examine):
        package = packages_to_examine[i]
        if package in packages or \
           all_dependencies.has_dependency(package, packages):
            # Install only dependencies.
            packages_to_examine.extend\
                ([dep for dep in all_dependencies.get_dependencies(package) if
                  dep not in packages_to_install + packages])
        else:
            # Safe to install this one.
            packages_to_install.append(package)
            install_args.append(''.join(install_args_for_package\
                                        (package, all_dependencies)))
            install_args.extend\
            (["^{0}".format\
              (''.join(install_args_for_package(dep,
                                                all_dependencies)))
              for dep in all_dependencies.get_all_dependencies(package)])
        i += 1
    return install_args


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


def get_environment(spec):
    package_name = spec.name
    with temp_environment():
        spack.build_environment.setup_package(spec.package, False)
        environment = os.environ.copy()
    # This needs to be what we want it to be.
    if 'SPACK_PREFIX' in environment:
        environment['SPACK_PREFIX'] = os.path.join(spackdev_base, 'install')
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
    # FIXME: this is wrong see https://docs.python.org/2.7/library/os.html#os.chmod
    # FIXME: for what should go into "mode"
    os.chmod(dest, 0o755)


def create_package_compiler_wrappers(wrappers_dir, environment):
    for var, value in sorted(environment.iteritems()):
        if var in ['CC', 'CXX', 'F77', 'FC']:
            if value[0] == "'" and value[-1] == "'":
                # It's been quoted: we need the shell to unquote it.
                value=subprocess.call("echo {0}".format(value), shell=True)
            filename = os.path.basename(value)
            dest = os.path.join(wrappers_dir, filename)
            environment[var]\
                = os.path.join(spackdev_base, dest)
            copy_modified_script(value, dest, environment)


def create_package_cmd_wrappers(package, package_wrappers_dir,
                                global_wrappers_dir):
    for cmd in ['cmake', 'ctest', 'make', 'ninja']:
        filename = os.path.join(package_wrappers_dir, cmd)
        tool = os.path.join(global_wrappers_dir, cmd)
        with open(filename, 'w') as f:
            f.write('#!/bin/bash\n')
            f.write('exec spack dev build-env -- {0} {1} "$@"\n'.
                    format(package, tool if os.path.exists(tool) else cmd))
        # FIXME: this is wrong see https://docs.python.org/2.7/library/os.html#os.chmod
        # FIXME: for what should go into "mode"
        os.chmod(filename, 0o755)


def create_cmd_links(specs):
    wrappers_dir = os.path.abspath(os.path.join(dev.spackdev_aux_subdir,
                                                'bin'))
    filesystem.mkdirp(wrappers_dir)
    for tool, package in (('cmake', 'cmake'), ('ctest', 'cmake'),
                          ('make', 'make'), ('ninja', 'ninja')):
        filename = os.path.join(wrappers_dir, tool)
        toolpath = tool_from(tool, package, specs)
        if toolpath != tool:
            tty.debug('Link: {0} -> {1} in {2}'.format(filename, toolpath, os.getcwd()))
            os.symlink(toolpath, filename)
    spack.util.environment.path_put_first('PATH', [wrappers_dir])
    return wrappers_dir


def create_package_wrappers(package, global_wrappers_dir, environment):
    package_wrappers_dir\
        = os.path.join(dev.spackdev_aux_packages_subdir, package, 'bin')
    filesystem.mkdirp(package_wrappers_dir)
    create_package_compiler_wrappers(package_wrappers_dir, environment)
    create_package_cmd_wrappers(package, package_wrappers_dir,
                                global_wrappers_dir)


def create_env_files(env_dir, environment):
    filesystem.mkdirp(env_dir)
    # Write a human-readable file.
    with open(os.path.join(env_dir, 'env.txt'), 'w') as outfile:
        for var, value in sorted(environment.iteritems()):
            outfile.write('{0}={1}\n'.format(var, value))
    # Write a source-able file.
    dump_environment(os.path.join(env_dir, 'env.sh'), environment)
    # Write a pickled file.
    pickle_environment(os.path.join(env_dir, 'env.pickle'), environment)


def create_environment(dev_packages, dev_package_specs, path_fixer, global_wrappers_dir):
    for dp in dev_packages:
        tty.msg('creating environment for {0}'.format(dp))
        package_spec = dev_package_specs[dp]
        environment = get_environment(package_spec)
        # Fix paths in environment
        environment\
            = dict((var,
                    path_fixer.fix(val,
                                   build_directory=build_directory_for(package_spec.package),
                                   package_name=dp)) for var, val in
                   environment.iteritems())
        create_package_wrappers(dp, global_wrappers_dir, environment)
        create_env_files(os.path.join(dev.spackdev_aux_packages_subdir, dp, 'env'), environment)
    create_env_files(dev.spackdev_aux_env_subdir, sanitized_environment(os.environ))


def write_package_info(requested, additional,
                       requested_dev_package_info,
                       additional_dev_package_info,
                       specs):
    packages_dir = os.path.join(spackdev_base, dev.spackdev_aux_packages_subdir)
    packages_filename = os.path.join(spackdev_base, dev.spackdev_aux_packages_sd_file)
    filesystem.mkdirp(packages_dir)
    install_args = ''
    dev_packages = requested + additional
    dep_specs = []
    install_names = []

    for dp in dev_packages:
        for spec in specs:
            exclusions = dev_packages + install_names
            if dp not in spec:
                continue
            dep_specs_new\
                = [dep for dep in spec[dp].dependencies() if
                   dep.name not in exclusions]
            dep_specs += dep_specs_new
            install_names.extend([dep.name for dep in dep_specs_new])

    for spec in specs:
        if spec.name not in dev_packages:
            for dp in dev_packages:
                if dp in spec:
                    break;
            else:
               dep_specs.append(spec)

    # Write package names.
    with open(packages_filename, 'w') as f:
        f.write(' '.join([dp.package_arg for
                          dp in requested_dev_package_info]) + '\n')
        f.write(' '.join([dp.package_arg for
                          dp in additional_dev_package_info]) + '\n')
        f.write(' '.join([dep.name for dep in dep_specs]) + '\n')

    # Write spec YAML.
    spec_dir = os.path.join(spackdev_base, dev.spackdev_aux_specs_subdir)
    filesystem.mkdirp(spec_dir)

    for dep in specs:
        with open(os.path.join(spec_dir, '{0}.yaml'.format(dep.name)), 'w') \
             as f:
            dep.to_yaml(f, True)

    return dep_specs


cmake = spack.util.executable.Executable('cmake')


def init_build_area(build_system, args):
    os.mkdir('build')
    os.chdir('build')
    cmd_args = [ '../srcs',
                 '-G',
                 '{0}'.format(build_system.cmake_generator) ]
    output = ''
    try:
        output = cmake(*cmd_args, output=str, error=str)
    except spack.util.executable.ProcessError as e:
        tty.error('''The SpackDev area has been initialized, but the initial
CMake command returned with status {status} and message:
"{msg}". Please check output above for
details and run:
  . {env}
  cd {build_dir}
  {cmd}
when you have addressed any problems.'''.
                  format(msg='\n'.join([e.message, e.long_message]),
                         status=cmake.returncode,
                         env=cmd_quote(os.path.join(spackdev_base,
                                                    dev.spackdev_aux_subdir, 'env.sh')),
                         build_dir=cmd_quote(os.path.join(spackdev_base, 'build')),
                         cmd=' '.join([cmd_quote(x) for x in
                                       [cmake.name] + cmd_args])))
        if args.no_stage and 'No download info given' in e.long_message:
            tty.error('Output indicates that sources are missing: please rectify and retry.')
        sys.exit(cmake.returncode)
    if args.verbose:
        tty.msg(output)


def setup_parser(subparser):
    # Main steering options.
    package_group = subparser.add_argument_group\
                ('package arguments',
                 'Specify packages to develop and their dependencies.')
    package_group.add_argument('--dag-file',
                               help='packages and dependencies should be inferred '
                               'from the list specified in this text file (in '
                               '"spack install" format)')
    # Default branch / tag options.
    defgroup = package_group.add_mutually_exclusive_group()
    defgroup.add_argument('--default-branch', dest='default_branch', default='develop',
                          help='default branch for package checkout.')
    defgroup.add_argument('--default-tag', dest='default_tag',
                          help='default tag for package checkout.')

    # Non-option arguments
    package_group.add_argument('packages', nargs=argparse.REMAINDER, metavar='PACKAGES',
                               help='non-option arguments: specs of packages (<package>@<tag>, <package>^<branch>) to add to SpackDev area')


    # Task control group
    task_group = subparser.add_argument_group\
                 ('init task control',
                  'Control what initialization tasks are executed.')

    task_group.add_argument('--resume', action='store_true', default=False,
                           help='Continue initialization of an incomplete SpackDev area. '
                           'Mutually incompatible with specified packages or --dag-files.')
    task_group.add_argument('-d', '--no-dependencies', action='store_true',
                            dest='no_dependencies',
                            help='do not have spack install dependent packages')
    task_group.add_argument('-s', '--no-stage', action='store_true',
                            dest='no_stage',
                            help='do not stage packages for development')


    # Generator control options.
    gengroup\
        = subparser.add_argument_group\
        ('generator control',
         'Control the generator used by the top-level CMake invocation, and '
         'optionally override generator choices for CMake packages under '
         'development.')
    mgroup = gengroup.add_mutually_exclusive_group()
    mgroup.add_argument('-m', '--make', action='store_const',
                        dest='generator', const='Unix Makefiles',
                        help='use make instead of ninja')
    mgroup.add_argument('-n', '--ninja', action='store_const',
                        dest='generator', const='Ninja',
                        help='use ninja instead of make')
    mgroup.add_argument('-G', '--generator', dest='generator',
                        help='Specify the generator to use explicitly')
    mgroup.set_defaults(generator='Unix Makefiles')
    gengroup.add_argument('--override-generator', action='store_true',
                          default=False,
                          help='Override CMake generator choice for packages '
                          'under development. Packages supporting the '
                          'SPACKDEV_GENERATOR environment variable will '
                          'automatically use the selected generator '
                          'regardless of this setting.')

    # Other options.
    subparser.add_argument('-b', '--base-dir', dest='base_dir',
                           help='Specify base directory to use instead of current working directory')
    subparser.add_argument('-f', '--force', action='store_true',
                           help='continue if base directory is not empty')
    subparser.add_argument('-p', '--print-spec-tree', action='store_true',
                           dest='print_spec_tree',
                           help='Print the full calculated spec tree(s)---cf spack spec -It')
    subparser.add_argument('-P', '--print-spec-tree-exit', action='store_const',
                           dest='print_spec_tree',
                           const='exit',
                           help='Print the full calculated spec tree(s)---cf spack spec -It---and then exit')
    subparser.add_argument('-v', '--verbose', action='store_true',
                           help='provide more helpful output')

    subparser.epilog\
        = '''Package specification for devlopment:
  The exact source code used for a package checked out for development is chosen as follows:

  A) If the package is specified as <package>@<tag> or <package>^<branch>, then:
     1. If the tag or branch corresponds to a version known to the package that utilizes a version-control system for checkout, it will be used. 
     2. Otherwise, the 'develop' version will be used as a template to check out the desired branch or tag of the code.
     3. If there is no 'develop' version, the specified version will be fetched regardless of the fetch method specified in the recipe.
  B) If the package does not have a specified version or it is an "additional" package identified as necessary for build consistency, then use the value from --default-branch, --default-tag, or 'develop' and see (A) above.

  Notes:
    * At any time, the user may check out a different branch or tag in srcs/<package>, or may even scrub and re-obtain the source independently. This may be desirable if, for instance, a static (not version-controlled) copy of the source was staged.
    * The dependency tree is determined by the DAG *as specified*—it is not altered in any way by a specific or default version override on the command line.
       '''

    # Save for later access during post-parse argument checking.
    global _init_subparser
    _init_subparser = subparser


def default_version_info_from_args(args):
    return\
        {'key': 'tag',
         'tag_or_branch': args.default_tag} if args.default_tag else \
        {'key': 'branch',
         'tag_or_branch': args.default_branch}


def init_spackdev_base(args):
    global spackdev_base
    # Sanity checks.
    if 'SPACKDEV_BASE' in os.environ:
        if args.force:
            tty.info('spack dev init: (force) removing existing SPACKDEV_* from current environment.')
            for x in [ x for x in os.environ if x.startswith('SPACKDEV_') ]:
                del os.environ[x]
        else:
            tty.die('spack dev init: current environment is already aware of a SpackDev environment ({0})'.
                    format(os.environ['SPACKDEV_BASE']))

    if args.base_dir:
        if os.path.isdir(args.base_dir) or \
           os.path.exists(os.path.abspath(os.path.dirname(args.base_dir))):
            spackdev_base = os.path.abspath(args.base_dir)
        else:
            tty.die('spack dev init: {0} is not a directory or its parent does not exist.'
                    .format(args.base_dir))

    try:
        if (not os.path.exists(spackdev_base)):
            os.mkdir(spackdev_base)
        os.chdir(spackdev_base)
    except OSError:
        tty.die('spack dev init: unable to make or change directory to {0}'
                .format(spackdev_base))

    if args.resume:
        if not (os.path.exists(dev.spackdev_aux_packages_sd_file) and
                os.path.exists(dev.spackdev_aux_specs_subdir)):
            _init_subparser.error('--resume specified, but required '
                                  'packages.sd and spec files missing: redo from start')
        tty.debug('cleaning incomplete SpackDev installation files')
        for wd in ('build', 'install', 'tmp',
                   dev.spackdev_aux_bin_subdir,
                   dev.spackdev_aux_env_subdir,
                   dev.spackdev_aux_packages_subdir):
            shutil.rmtree(wd, ignore_errors=True)
    elif os.listdir(spackdev_base):
        if args.force:
            tty.info('spack dev init: (force) using non-empty directory {0}'
                     .format(spackdev_base))
            if (os.path.exists('spackdev-aux') or
                os.path.exists('build') or
                os.path.exists('install') or
                os.path.exists('tmp')):
                if args.force:
                    tty.info('spack dev init: (force) removing existing spackdev-aux, build and install directories from {0}'
                             .format(spackdev_base))
                    for wd in ('spackdev-aux', 'build', 'install', 'tmp'):
                        shutil.rmtree(wd, ignore_errors=True)
                else:
                    tty.die('spack dev init: cannot re-init (spackdev-aux/build/install/tmp directories exist)')
        else:
            tty.die('spack dev init: refusing to use non-empty directory {0}'
                    .format(spackdev_base))

    # Save for posterity.
    os.environ['SPACKDEV_BASE'] = spackdev_base

    # Make necessary subdirectories.
    filesystem.mkdirp('spackdev-aux')
    filesystem.mkdirp('srcs')


def print_spec_tree(dev_packages, specs):
    # Print the minimum number of spec trees starting with a package
    # for development such that all packages for development
    # (including additional ones) are shown in at least one tree.
    spec_names_to_print = set()
    specs_to_print_dict = {}
    all_to_print = set()
    for spec in specs:
        for p in dev_packages:
            if p in spec and p not in all_to_print:
                flat_dependencies = spec[p].flat_dependencies().keys()
                # Print the largest tree(s) only, avoiding
                # duplication of development packages.
                spec_names_to_print.difference_update(flat_dependencies)
                all_to_print.update(flat_dependencies)
                spec_names_to_print.add(p)
                specs_to_print_dict[p] = spec[p]
    tty.msg('Development package spec trees: \n{0}'.\
            format('\n'.join([spec.tree(cover='nodes',
                                        format='{name}{@version}{%compiler}{compiler_flags}{variants}{arch=architecture}',
                                        hashlen=7,
                                        show_types=True,
                                        status_fn=spack.spec.Spec.install_status)
                              for spec in [specs_to_print_dict[p] for p in spec_names_to_print]])))


def get_package_info(args):
    dag_filename = None
    if args.resume:
        tty.msg('resuming an incomplete SpackDev initialization')
        (requested_info, additional_info, deps, specs) = dev.cmd.read_package_info()
        requested = [dp.name for dp in requested_info]
        additional = [dp.name for dp in additional_info]
        dev_package_info = requested_info + additional_info
        dep_specs = [dev.cmd.get_package_spec(dep, specs) for dep in deps]
    else:
        tty.msg('Calculating package information')
        # Extract and build dev package and spec info from args.
        default_version_info = default_version_info_from_args(args)

        requested_dev_package_info\
            = [DevPackageInfo(package_arg, default_info=default_version_info) for
               package_arg in args.packages]
        requested = [dp.name for dp in requested_dev_package_info]

        # Construct the concretized spec tree and identify additional
        # packages for checkout.
        dag_filename = args.dag_file
        specs = extract_specs(dag_filename if dag_filename else requested)
        additional = get_additional(requested, specs)
        additional_dev_package_info\
            = [DevPackageInfo(a, default_info=default_version_info)
               for a in additional]

        dev_package_info = requested_dev_package_info + additional_dev_package_info

        # Identify non-development dependencies and write package info to
        # file.
        dep_specs\
            = write_package_info(requested, additional,
                                 requested_dev_package_info,
                                 additional_dev_package_info,
                                 specs)

    # Report what we're doing.
    tty.msg('requested packages: {0}{1}'.\
            format(', '.join(requested),
                   ' from install tree as specified in {0}'.format(dag_filename)
                   if dag_filename else ''))
    if additional:
        tty.msg('additional inter-dependent packages: ' +
                ' '.join(additional))

    # Return obtained information
    return requested, additional, dev_package_info, specs, dep_specs


# Implementation of the subcommand.
def init(parser, args):
    # Verbosity
    tty.set_verbose(args.verbose)

    # Non-trivial exclusivity checks
    if args.resume:
        if args.packages or args.dag_file:
            _init_subparser.error('--resume is incompatible with --dag-file or non-option arguments PACKAGES')

    # Initialize the spack dev area.
    init_spackdev_base(args)

    # Specify the build system in the environment (may be used by
    # recipes during spec concretization).
    build_system = Build_system(args.generator, args.override_generator)
    os.environ['SPACKDEV_GENERATOR'] = build_system.cmake_generator

    (requested, additional, dev_package_info, specs, dep_specs)\
        = get_package_info(args)

    dev_packages = requested + additional

    # Identify specs and set staging path for development packages.
    dev_package_specs = {}
    for package in dev_packages:
        spec = dev.cmd.get_package_spec(package, specs)
        if spec:
            dev_package_specs[package] = spec[package]
            # Set the package's path to be passed to the stage object
            # when it is created.
            spec.package.path = spack_stage_top()
        else:
            tty.die('Unable to find spec for specified package {0}'.\
                    format(package))

    # Print development package spec tree(s) if desired.
    if args.print_spec_tree:
        print_spec_tree(dev_packages, specs)
        if args.print_spec_tree == 'exit':
            sys.exit(1)

    # Stage development packages if selected.
    if not args.no_stage:
        tty.msg('stage sources for {0}'.format(dev_packages))
        dev.cmd.stage_packages(dev_package_info, dev_package_specs)

    # Exit now if we're not installing dependencies.
    if args.no_dependencies:
        tty.msg('Dependencies will not be built, SpackDev environment is incomplete.')
        tty.msg('Use spack dev --resume to complete initialization of SpackDev environment.')
        sys.exit(1)

    # Continue with the rest of the initialization process.
    tty.msg('install dependencies')
    dev.cmd.install_dependencies(dev_package_info=dev_package_info,
                                 dep_specs=dep_specs)

    # Create tool wrappers.
    global_wrappers_dir = create_cmd_links(specs)

    # Create the environment files.
    tty.msg('create environment files.')
    path_fixer = PathFixer(spack.store.root, spack_stage_top())
    path_fixer.set_packages(*dev_packages)
    create_environment(dev_packages, dev_package_specs,
                       path_fixer, global_wrappers_dir)

    # Generate the top level CMakeLists.txt.
    tty.msg('generate top level CMakeLists.txt')
    write_cmakelists(dev_packages, dev_package_specs, build_system, path_fixer)

    # Initialize the build area.
    tty.msg('initialize build area')
    init_build_area(build_system, args)

    # Done.
    tty.msg('initialization of {0} complete;'.format(spackdev_base))
    tty.msg('source {0} to begin.'.
            format(os.path.join(spackdev_base, dev.spackdev_aux_env_subdir,
                                'env.sh')))
