from . import install_dependencies

description  = 'install missing dependencies of packages in a SpackDev area'

def setup_parser(subparser):
    pass

def getdeps(parser, args):
    cmd.install_dependencies()
