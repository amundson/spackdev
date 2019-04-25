from __future__ import print_function

import argparse
import copy
import os
import re
import shutil
import subprocess
import sys

import spack.store  # For spack.store.root to replace SPACK_INSTALL

import fnal.spack.dev as dev
from fnal.spack.dev.environment import sanitized_environment, srcs_topdir

from llnl.util import tty
from six.moves import shlex_quote as cmd_quote

if sys.version_info[0] > 3 or \
   (sys.version_info[0] == 3 and sys.version_info[1] > 2):
    # Available from Python 3.3 onwards.
    from shutil import which
else:
    from distutils.spawn import find_executable as which

import spack.cmd
import spack.build_environment
import spack.paths
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
            tty.die('''Build_system: invalid generator {0}--primary generator must be
           either "Unix Makefiles" or "Ninja"'''.format(generator))
        self.cmake_generator = generator
        self.label = 'ninja' if primary_generator.group(1) == 'Ninja' else 'make'
        self.build_command = self.label # default
        if self.label == 'ninja':
            if which('ninja'):
                self.build_command = 'ninja'
            elif which('ninja-build'):
                self.build_command = 'ninja-build'
            else:
                tty.msg('warning: ninja build selected, but neither "ninja" nor "ninja-build" are available')
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
        raw_matcher = r'(?:(?<=[=\s;:"\'])|^){{path}}/(?:[^;:\"]*?/)*?(?P<pkg>{0})-[^;:"\'/]*{{extra}}'.\
                     format('|'.join(sorted_args))
        self.install_path_finder\
            = re.compile(raw_matcher.format(path=self.spack_install, extra=''))
        self.stage_path_finder\
            = re.compile(raw_matcher.format(path=self.spack_stage, extra='/spack-build'))

    def fix(self, path):
        result = self.install_path_finder.sub(os.path.join(self.spackdev_install, r'\g<pkg>'), path)
        result = self.stage_path_finder.sub(os.path.join(self.spackdev_stage, r'\g<pkg>'), result)
        return result


def extract_specs(spec_source):
    spec_args = []
    if type(spec_source) == list:
        # List of packages.
        spec_args.extend(spec_source)
    else:
        # File containing spack install specification.
        with open(spec_source, 'r') as dag_file:
            spec_args.extend(dag_file.read().rstrip().split())
    return spack.cmd.parse_specs(spec_args, concretize=True)


def get_additional(requested, specs):
    additional = []
    found = copy.copy(requested)
    while len(found):
        all_done = requested + additional
        to_consider = copy.copy(found)
        found = []
        for package in to_consider:
            for spec in specs:
                append_unique([dep for dep in spec.flat_dependencies() if
                               dep not in all_done and
                               dep in spec[package].dependents_dict()],
                              found)
        additional += found
    return additional


def init_cmakelists(project='spackdev'):
    f = open(os.path.join('srcs', 'CMakeLists.txt'), 'w')
    f.write(
        '''cmake_minimum_required(VERSION ${{CMAKE_VERSION}})
project({0} NONE)
set(SPACKDEV_SOURCE_DIR "{1}")
set(SPACKDEV_PREFIX "{2}")

include(ExternalProject)

set_property(DIRECTORY PROPERTY EP_STEP_TARGETS
             configure build install test
  )
'''.format(project,
           srcs_topdir(),
           os.path.join(srcs_topdir(), 'install')))
    return f


gen_arg = re.compile(r'-G(.*)')
def add_package_to_cmakelists(cmakelists, package, package_dependencies,
                              cmake_args, build_system):

    cmd_wrapper\
        = lambda x : os.path.join(spackdev_base,
                                  dev.spackdev_aux_packages_subdir,
                                  package,
                                  'bin',
                                  x)

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
file(MAKE_DIRECTORY tmp/{package})
file(MAKE_DIRECTORY {package})

ExternalProject_Add({package}
  TMP_DIR "tmp/{package}"
  STAMP_DIR "tmp/{package}/stamp"
  DOWNLOAD_DIR "tmp/{package}"
  SOURCE_DIR "${{SPACKDEV_SOURCE_DIR}}/{package}"
  BINARY_DIR "{package}"
  INSTALL_DIR "${{SPACKDEV_PREFIX}}/{package}"
  CMAKE_COMMAND "{cmake_wrapper}"
  CMAKE_GENERATOR "{cmake_generator}"
  CMAKE_ARGS {cmake_args}
  BUILD_COMMAND {build_command}
  BUILD_ALWAYS TRUE
  LIST_SEPARATOR "|"
  DEPENDS {package_dependency_targets}
  )
'''.format(package=package,
           cmake_wrapper=cmd_wrapper('cmake'),
           cmake_args=cmake_args_string,
           cmake_generator=cmake_generator,
           build_command='"{0}"'.format(cmd_wrapper(generator_cmd)) if
           generator_label == 'ninja' else
           '"env" "MAKE={make_wrapper}" "$(MAKE)"'.
           format(make_wrapper=cmd_wrapper('make')),
           package_dependency_targets=' '.join(package_dependencies)))


cmake_args_start = re.compile(r'\[cmake-args\s+([^\]]+)\]')
cmake_args_end = re.compile(r'\[/cmake-args\]')


def extract_cmake_args(packages, package_specs):
    package_cmake_args = {}
    for package in packages:
        package_obj = package_specs[package].package
        package_cmake_args[package] = package_obj.std_cmake_args + \
                                      package_obj.cmake_args()
    return package_cmake_args


def intersection(a, b):
    temp = set(b)
    c = [ val for val in a if val in temp ]
    return c


def write_cmakelists(packages, package_specs, build_system, path_fixer):
    package_cmake_args = extract_cmake_args(packages, package_specs)
    cmakelists = init_cmakelists()
    remaining_packages = copy.copy(packages)
    while remaining_packages != []:
        for package in remaining_packages:
            spec = package_specs[package]
            dep_dict = spec.dependencies_dict()
            if not any(other for other in remaining_packages if
                       other in dep_dict):
                # package is a leaf.
                package_dependencies\
                    = intersection(packages, dep_dict.keys())
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
    return spack.paths.stage_path


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


class temp_environment:
    def __enter__(self):
        self._safe_env = os.environ.copy()
        return os.environ
    def __exit__(self, type, value, traceback):
        os.environ.clear()
        os.environ.update(self._safe_env)


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
    os.chmod(dest, 0755)


def create_compiler_wrappers(wrappers_dir, environment):
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
    wrappers_dir = os.path.join(dev.spackdev_aux_packages_subdir, package, 'bin')
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


def create_environment(packages, package_specs):
    path_fixer = None
    for package in packages:
        tty.msg('creating environment for {0}'.format(package))
        environment = get_environment(package_specs[package])
        if path_fixer is None:
            path_fixer = PathFixer(spack.store.root, spack_stage_top())
        # Fix paths in environment
        path_fixer.set_packages(*packages)
        environment = dict((var, path_fixer.fix(val)) for var, val in
                       environment.iteritems())
        create_wrappers(package, environment)
        create_env_files(os.path.join(dev.spackdev_aux_packages_subdir, package, 'env'), environment)
    create_env_files('spackdev-aux', sanitized_environment(os.environ))
    return path_fixer


def write_package_info(requested, additional, specs):
    packages_dir = os.path.join(spackdev_base, dev.spackdev_aux_packages_subdir)
    packages_filename = os.path.join(packages_dir, 'packages.sd')
    if not os.path.isdir(packages_dir):
        os.mkdir(packages_dir)
    install_args = ''
    dev_packages = requested + additional
    dep_specs = []
    install_names = []
    for package in dev_packages:
        for spec in specs:
            dep_specs_new\
                = [dep for dep in spec[package].dependencies() if
                   dep.name not in dev_packages + install_names]
            dep_specs += dep_specs_new
            install_names.extend([dep.name for dep in dep_specs_new])

    # Write package names
    with open(packages_filename, 'w') as f:
        f.write(' '.join(requested) + '\n')
        f.write(' '.join(additional) + '\n')
        f.write(' '.join([dep.name for dep in dep_specs]) + '\n')

    # Write spec YAML.
    spec_dir = os.path.join(spackdev_base, dev.spackdev_aux_specs_subdir)
    if not os.path.exists(spec_dir):
        os.makedirs(spec_dir)

    for dep in specs:
        with open(os.path.join(spec_dir, '{0}.yaml'.format(dep.name)), 'w') \
             as f:
            dep.to_yaml(f, True)

    return dep_specs


cmake = spack.util.executable.Executable('cmake')


def create_build_area(build_system, args):
    os.mkdir('build')
    os.chdir('build')
    cmd_args = [ '../srcs',
                 '-G',
                 '{0}'.format(build_system.cmake_generator) ]
    output = ''
    try:
        output = cmake(*cmd_args, output=str, error=str)
    except spack.util.executable.ProcessError as e:
        tty.msg(output)
        tty.error('''The SpackDev area has been initialized, but the initial
CMake command returned with status {status} and message:
"{msg}". Please check output above for
details and run:
  . {env}
  cd {build_dir}
  {cmd}
when you have addressed any problems.'''.
                  format(msg=' '.join([e.message, e.long_message]),
                         status=cmake.returncode,
                         env=cmd_quote(os.path.join(spackdev_base,
                                                    dev.spackdev_aux_subdir, 'env.sh')),
                         build_dir=cmd_quote(os.path.join(spackdev_base, 'build')),
                         cmd=' '.join([cmd_quote(x) for x in
                                       [cmake.name] + cmd_args])))
        sys.exit(cmake.returncode)
    if args.verbose:
        tty.msg(output)


def setup_parser(subparser):
    subparser.add_argument('packages', nargs=argparse.REMAINDER,
                           help="specs of packages to add to SpackDev area")
    subparser.add_argument('--dag-file',
                           help='packages and dependencies should be inferred from the list specified in this text file (in "spack install" format)')
    subparser.add_argument('-d', '--no-dependencies', action='store_true',
                           dest='no_dependencies',
                           help="do not have spack install dependent packages")
    subparser.add_argument('-f', '--force', action='store_true',
                           help="Continue if base directory is not empty")
    subparser.add_argument('-b', '--base-dir', dest='base_dir',
                           help="Specify base directory to use instead of current working directory")
    gengroup\
        = subparser.add_argument_group\
        ('generator control',
         '''Control the generator used by the top-level CMake invocation, and
optionally override generator choices for CMake packages under development.''')
    mgroup = gengroup.add_mutually_exclusive_group()
    mgroup.add_argument('-m', '--make', action='store_const',
                        dest='generator', const='Unix Makefiles',
                        help="use make instead of ninja")
    mgroup.add_argument('-n', '--ninja', action='store_const',
                        dest='generator', const='Ninja',
                        help="use ninja instead of make")
    mgroup.add_argument('-G', '--generator', dest='generator',
                        help="Specify the generator to use explicitly")
    mgroup.set_defaults(generator='Unix Makefiles')
    gengroup.add_argument('--override-generator', action='store_true',
                          default=False,
                          help="""Override CMake generator choice for packages under development.

Packages supporting the SPACKDEV_GENERATOR environment variable will
automatically use the selected generator regardless of this setting.""")

    subparser.add_argument('-s', '--no-stage', action='store_true',
                           dest='no_stage',
                           help="do not stage packages")
    subparser.add_argument('-v', '--verbose', action='store_true',
                           help="provide more helpful output")


def init(parser, args):
    global spackdev_base

    # Verbosity
    tty.set_verbose(args.verbose)

    if 'SPACKDEV_BASE' in os.environ:
        if args.force:
            tty.warn('spack dev init: (force) removing existing SPACKDEV_* from current environment.')
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

    # Save for posterity
    os.environ['SPACKDEV_BASE'] = spackdev_base

    requested = args.packages
    dag_filename = args.dag_file
    build_system = Build_system(args.generator, args.override_generator)
    os.environ['SPACKDEV_GENERATOR'] = build_system.cmake_generator

    if os.listdir(spackdev_base):
        if args.force:
            tty.warn('spack dev init: (force) using non-empty directory {0}'
                     .format(spackdev_base))
        else:
            tty.die('spack dev init: refusing to use non-empty directory {0}'
                    .format(spackdev_base))

    if (os.path.exists('spackdev-aux')):
        if args.force:
            tty.warn('spack dev init: (force) removing existing spackdev-aux, build and install directories from {0}'
                     .format(spackdev_base))
            for wd in ('spackdev-aux', 'install', 'build'):
                shutil.rmtree(wd, ignore_errors=True)
        else:
            tty.die('spack dev init: cannot re-init (spackdev-aux directory exists)')
    os.mkdir('spackdev-aux')
    os.mkdir('srcs')

    tty.msg('requested packages: {0}{1}'.\
            format(', '.join(requested),
                   ' from install tree as specified in {0}'.format(dag_filename)
                   if dag_filename else ''))
    specs = extract_specs(dag_filename if dag_filename else requested)
    additional = get_additional(requested, specs)
    if additional:
        tty.msg('additional inter-dependent packages: ' +
                ' '.join(additional))
    dev_packages = requested + additional
    dep_specs = write_package_info(requested, additional, specs)

    package_specs = {}
    for package in dev_packages:
        spec = dev.cmd.get_package_spec(package, specs)
        if spec:
            package_specs[package] = spec[package]
        else:
            tty.die("Unable to find spec for specified package {0}".\
                    format(package))

    if not args.no_stage:
        tty.msg('stage sources for {0}'.format(dev_packages))
        dev.cmd.stage_packages(dev_packages, package_specs)

    if not args.no_dependencies:
        tty.msg('install dependencies')
        dev.cmd.install_dependencies(dev_packages=dev_packages,
                                     dep_specs=dep_specs)

    tty.msg('create wrapper scripts')
    path_fixer = create_environment(dev_packages, package_specs)

    tty.msg('generate top level CMakeLists.txt')
    write_cmakelists(dev_packages, package_specs, build_system, path_fixer)

    tty.msg('create and initialize build area')
    create_build_area(build_system, args)

    tty.msg('initialization of {0} complete;'.format(spackdev_base))
    tty.msg('source {0} to begin.'.
            format(os.path.join(spackdev_base, dev.spackdev_aux_subdir,
                                'env.sh')))
