import copy
import os
import re
import shutil
import six

from llnl.util import tty
from llnl.util.filesystem import mkdirp

from spack.error import SpackError
import spack.fetch_strategy as fs
from spack.spec import Spec
from spack.stage import Stage
from spack.version import Version


class DevPackageInfo:
    """Manage information for a development package.
    """
    _version_info_extractor = re.compile(r'^(.*?)(?:([@^])(.*))?$')

    def __init__(self, package_arg, **kwargs):
        """Constructor arguments:

        package_arg: development package designator in the form
                     package@tag or package^branch OR
                     dict providing name, key and tag_or_branch.

        default_info=<dict>: Provide default key and tag_or_branch keys
                     (ignored if provided package_arg is a dict).
        """
        if isinstance(package_arg, six.string_types):
            match_result = DevPackageInfo._version_info_extractor.match(package_arg)
            if not match_result:
                raise ValueError('Expected package arg matching /^(.*?)(?:([@^])(.*))?$/')
            self.version_info = {'name': match_result.group(1)}
            if (match_result.group(2)):
                self.version_info['key']\
                    = 'tag' if match_result.group(2) == '@' else 'branch'
                self.version_info['tag_or_branch'] = match_result.group(3)
            else:
                self.version_info.update(kwargs.get('default_info', {}))
        else:  # Assume dict-ish
            self.version_info\
                = {k: package_arg[k] for k in \
                   ('name', 'key', 'tag_or_branch') if k in package_arg}
        self.package_arg = copy.copy(self._package_arg_from_version_info())

    @property
    def name(self):
        return self.version_info['name']

    @property
    def key(self):
        return self.version_info['key'] or None

    @property
    def tag_or_branch(self):
        return self.version_info['tag_or_branch'] or None

    def _package_arg_from_version_info(self):
        if self.key:
            return '{0}{1}{2}'.\
                format(self.name,
                       '@' if self.key == 'tag' else '^',
                       self.tag_or_branch)
        else:
            return self.name

    def __str__(self):
        return self.package_arg


def read_package_info(want_specs=True):
    dev.environment.bootstrap_environment()
    packages_filename = os.path.join(os.environ['SPACKDEV_BASE'],
                                     dev.spackdev_aux_packages_sd_file)
    with open(packages_filename, 'r') as f:
        first_line = f.readline().rstrip()
        if first_line.find('[') > -1:
            tty.die('packages.sd in obsolete (unsafe) format: please re-execute spack init or initialize a new spackdev area.')
        requesteds = [ DevPackageInfo(package_arg) for
                       package_arg in first_line.split() ]
        additional = [ DevPackageInfo(package_arg) for
                       package_arg in f.readline().rstrip().split() ]
        deps = f.readline().rstrip().split()

    install_specs = []
    if want_specs:
        specs_dir = os.path.join(os.environ['SPACKDEV_BASE'],
                                 dev.spackdev_aux_specs_subdir)
        if not os.path.exists(specs_dir):
            tty.die('YAML spec information missing: please re-execute spack init or initialize a new spackdev area.')
        for spec_file in os.listdir(specs_dir):
            if spec_file.endswith('.yaml'):
                with open(os.path.join(specs_dir, spec_file), 'r') as f:
                    install_specs.append(Spec.from_yaml(f))
        return requesteds, additional, deps, install_specs

    return requesteds, additional, deps


def _seq_intersection(s1, s2):
    """Utility to provide the intersection between two sequences. Generator
    expression.
    """
    temp = set(s2)
    for v in s1:
        if v in temp:
            yield v


# Known VCS systems.
_vc_keys = ('git', 'svn', 'hg')  # No CVS, sorry.


def _version_is_vc(package, version):
    """Is this a known version-controlled version (vs tarball, etc.)?
    """
    return len(list(_seq_intersection(_vc_keys,
                                      package.versions[version]))) > 0


def _tweak_dev_package_fetcher(dp, spec):
    """Attempt to configure the package's fetcher and stage to obtain the
source we want to develop for this package.
"""
    if dp.tag_or_branch is None:
        # Nothing to do.
        return

    fetcher_Version = None
    spack_package = spec.package

    # We want the tag or branch specified in dp.
    package_Version = Version(dp.tag_or_branch)
    develop_Version = Version('develop')
    if package_Version in spack_package.versions and \
       _version_is_vc(spack_package, package_Version):
        # Specified version is version-controlled.
        fetcher_Version = package_Version
    elif not fetcher_Version and \
       develop_Version in spack_package.versions and \
       _version_is_vc(spack_package, develop_Version):
        # Repurpose develop to obtain the tag/branch we need.
        version_dict = spack_package.versions[develop_Version]
        # Attempt to tweak things to check out our desired tag or branch.
        if 'git' in version_dict:  # Git.
            version_dict.pop('commit', None)
            if dp.key == 'tag':
                version_dict['tag'] = dp.tag_or_branch
                version_dict.pop('branch', None)
            else:  # Branch.
                version_dict['branch'] = dp.tag_or_branch
                version_dict.pop('tag', None)
        elif 'hg' in version_dict:  # Mercury
            version_dict['revision'] = dp.tag_or_branch
        elif 'svn' in version_dict:  # Subversion.
            # Can't reliably do anything here since SVN URL structure is
            # convention only, and it is also not possible to reliably
            # distinguish between two common conventions
            # ('project/<trunk-or-branches-or-tags>' vs
            # '<trunk-or-branches-or-tags>/project'.
            raise ExtrapolationError(
                'For subversion repositories, a VC version corresponding to '
                '{0} must be defined in the recipe for {1}.'.
                format(dp.tag_or_branch, dp.name))
        else:
            raise SpackError('INTERNAL ERROR: spack dev cannot handle '
                             'apparently-supported VC method\n'
                             'version_dict = {0}'.format(version_dict))
        fetcher_Version = develop_Version

    if fetcher_Version:
        version_dict = spack_package.versions[fetcher_Version]
        version_dict['no_cache'] = True  # Disable caching.
        if 'git' in version_dict:
            # Disable efficiency options that aren't wanted here.
            version_dict.update({'full_depth': True, 'all_branches': True})
        spack_package.fetcher = fs.for_package_version(spack_package, fetcher_Version)
        spack_package.stage = Stage(spack_package.fetcher, path=spack_package.path)
    else:
        tty.warn('Spack dev unable to obtain VC source for package {0} {1}'
                 '\nFalling back to version {2} as concretized'.
                 format(dp.name,
                        'with user-specified {0} {1}'.format(dp.key, dp.tag_or_branch) if dp.key else '',
                        spack_package.version))


def stage_package(dp, spec):
    package = dp.name
    topdir = dev.environment.srcs_topdir()
    if not os.path.exists(topdir):
        os.mkdir(topdir)
    package_dest = os.path.join(topdir, package)
    if os.path.exists(package_dest):
        tty.msg('Package {0} is already staged for development: skipping'.
                format(package))
        return
    tty.msg('Staging {0} for development'.format(package))
    _tweak_dev_package_fetcher(dp, spec)
    spec.package.do_stage()
    if os.path.exists(os.path.join(spec.package.path,
                                   'spack-expanded-archive')):
        package_path = os.path.join(spec.package.path,
                                   'spack-expanded-archive')
    else:
        package_path = spec.package.path

    files_or_dirs = os.listdir(package_path)
    if len(files_or_dirs) > 1:  # Automatic consolidation.
        mkdirp(package_dest)
    for file_or_dir in files_or_dirs:
        tty.debug('Moving {0} to {1}'.
                  format(os.path.join(package_path, file_or_dir),
                         package_dest))
        shutil.move(os.path.join(package_path, file_or_dir),
                    package_dest)


def stage_packages(dev_package_info, package_specs):
    for dp in dev_package_info:
        stage_package(dp, package_specs[dp.name])


def get_package_spec(package, specs):
    return reduce(lambda a, b : a if package in a else b, specs, {})[package]


def install_dependencies(**kwargs):
    if 'dep_specs' in kwargs:
        dev_package_info = kwargs['dev_package_info']
        dep_specs = kwargs['dep_specs']
    else:
        # Concretization is necessary.
        (requested_info, additional_info, deps, install_specs) = read_package_info()
        dev_package_info = requested_info + additional_info
        dep_specs = [ get_package_spec(dep, install_specs) for dep in deps ]

    tty.msg('requesting spack install of dependencies for: {0}'
            .format(' '.join([dp.name for dp in dev_package_info])))
    for dep in dep_specs:
        tty.debug('installing dependency {0}'.format(dep.name))
        dep.package.do_install()
