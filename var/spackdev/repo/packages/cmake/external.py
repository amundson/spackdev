#!/usr/bin/env python

from spackdev import external_tools


class Cmake:
    def doit(self):
        print('Cmake.doit: doing it\n')

    def find(self):
        pathname = external_tools.which_in_path('cmake')
        print('jfa: found cmake in ', pathname)
        #       exe = external_tools.which_in_path('tar')
