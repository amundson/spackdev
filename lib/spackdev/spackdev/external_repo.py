#!/usr/bin/env python

import os
import os.path
import sys
import imp


def parent_dir(path, n):
    retval = os.path.dirname(path)
    for n in range(0, n - 1):
        retval = os.path.dirname(retval)
    return retval


def mod_to_class(name):
    return name.capitalize()


class External_repo:
    def __init__(self):
        spackdev_root = parent_dir(__file__, 4)
        self.externals_path = os.path.join(spackdev_root, 'var', 'spackdev',
                                           'repo', 'packages')
        self.external_file_name = 'external.py'
        self._find_externals()

    def _find_externals(self):
        self._all_external_names = []
        for ext_name in os.listdir(self.externals_path):
            ext_dir = os.path.join(self.externals_path, ext_name)
            if os.path.isdir(ext_dir):
                ext_file = os.path.join(ext_dir, self.external_file_name)
                if os.path.isfile(ext_file):
                    self._all_external_names.append(ext_name)
        self._all_external_names.sort()

    def all_external_names(self):
        '''Returns a sorted list of all externals in external repo'''
        return self._all_external_names

    def get_pkg_class(self, pkg_name):
        '''Get the class for a package out of its module'''

        path_name = os.path.join(self.externals_path, pkg_name,
                                 self.external_file_name)

        if not os.path.isfile(path_name):
            sys.stderr.write(
                'External_repo.get_pkg_class: could not find "{}"\n'.format(
                    path_name))
        module_name = 'spackdev.external.{}'.format(pkg_name)
        module = imp.load_source(module_name, path_name)
        class_name = mod_to_class(pkg_name)
        class_ = getattr(module, class_name)
        return class_