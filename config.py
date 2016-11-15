# Copyright (C) 2015 Sebastian Herbord. All rights reserved.
#
# This file is part of Mod Organizer.
#
# Mod Organizer is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Mod Organizer is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Mod Organizer.  If not, see <http://www.gnu.org/licenses/>.


from _winreg import *
from unibuild.utility.lazy import Lazy
import os

from buildtools import os_utils
from buildtools.config import YAMLConfig

global missing_prerequisites
missing_prerequisites = False

def path_or_default(filename, *default):
    from distutils.spawn import find_executable
    defaults = gen_search_folders(*default)
    res = find_executable(filename, os.environ['PATH'] + ";" + ";".join(defaults))
    if res is None:
        print 'Cannot find', filename, 'on your path or in', os.path.join('', *default)
        global missing_prerequisites
        missing_prerequisites = True
    return res

def get_from_hklm(path, name, wow64=False):
    flags = KEY_READ
    if wow64:
        flags |= KEY_WOW64_32KEY

    with OpenKey(HKEY_LOCAL_MACHINE, path, 0, flags) as key:
        return QueryValueEx(key, name)[0]


program_files_folders = [
    os.environ['ProgramFiles(x86)'],
    os.environ['ProgramFiles'],
    os.environ['ProgramW6432'],
    "C:\\",
    "D:\\"
]


def gen_search_folders(*subpath):
    return [
        os.path.join(search_folder, *subpath)
            for search_folder in program_files_folders
    ]

config = {
    'tools': {
        'make': "nmake",
    },
    'architecture': 'x86_64',
    'vc_version':   '14.0',
    'build_type': "RelWithDebInfo",
    'ide_projects': True,
    'offline': False,                       # if set, non-mandatory network requests won't be made.
                                            # This is stuff like updating source repositories. The initial
                                            # download of course can't be surpressed.
    'prefer_binary_dependencies': False,    # currently non-functional
    'optimize': False,                      # activate link-time code generation and other optimization.
                                            # This massively increases build time but produces smaller
                                            # binaries and marginally faster code
    'repo_update_frequency': 60 * 60 * 24,  # in seconds
}

config['paths'] = {
    'download':      "{base_dir}\\downloads",
    'build':         "{base_dir}\\build",
    'progress':      "{base_dir}\\progress",
    'graphviz':      os_utils.which('dot.exe'), #path_or_default("dot.exe",   "Graphviz2.38", "bin"),
    'cmake':         os_utils.which('cmake.exe'), #path_or_default("cmake.exe", "CMake", "bin"),
    'git':           os_utils.which('git.exe'), #path_or_default("git.exe",   "Git", "bin"),
    'hg':            os_utils.which('hg.exe'),
    'perl':          os_utils.which('perl.exe'),
    'ruby':          os_utils.which('ruby.exe'),
    'svn':           os_utils.which('svn.exe'),
    '7z':            os_utils.which('7z.exe'),
    # we need a python that matches the build architecture
    'python':        os_utils.which('python27.exe'),
    'visual_studio': os.path.realpath(
        os.path.join(get_from_hklm(r"SOFTWARE\Microsoft\VisualStudio\{}".format(config['vc_version']),
                                   "InstallDir", True),
                     "..", "..", "VC"
                     )
    )
}
config = YAMLConfig('build.yml',config)
assert config.get('paths.graphviz') is not None
assert config.get('paths.cmake') is not None
assert config.get('paths.git') is not None
assert config.get('paths.hg') is not None
assert config.get('paths.perl') is not None
assert config.get('paths.ruby') is not None
assert config.get('paths.svn') is not None
assert config.get('paths.7z') is not None
assert config.get('paths.python') is not None
if missing_prerequisites:
    print '\nMissing prerequisites listed above - cannot continue'
    exit(1)
