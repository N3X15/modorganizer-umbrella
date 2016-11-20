import argparse
import codecs
import hashlib
import itertools
import multiprocessing
import os
import re
import shutil
import sys
import time
import yaml
import fnmatch
from string import Formatter

script_dir = os.path.abspath(os.path.dirname(__file__))

sys.path.append(os.path.join(script_dir, 'lib', 'python-build-tools'))

from buildtools import ENV, http, log, os_utils
from buildtools.buildsystem import MSBuild, WindowsCCompiler
from buildtools.buildsystem.visualstudio import (ProjectType,
                                                 VisualStudio2015Solution,
                                                 VS2015Project)
from buildtools.config import YAMLConfig
from buildtools.repo.git import GitRepository
from buildtools.repo.hg import HgRepository
from buildtools.wrapper import CMake



class FormatDict(dict):
    """
    a dictionary that doesn't throw an exception on access to an unknown key,
    intended to be used for format parameters.

    @author Sebastian Herbord
    """

    def __missing__(self, key):
        return "{" + key + "}"


def hasntBeenCheckedFor(dirname, durationInSeconds=3600):
    tsfile = os.path.join(dirname, '.chk-ts')
    if not os.path.isdir(dirname):
        return True
    if not os.path.isfile(tsfile):
        return True
    mtime = 0
    with open(tsfile, 'r') as f:
        mtime = float(f.read().strip())
    tdiff = int(time.time() - mtime)
    print('tdiff={}'.format(tdiff))
    return tdiff > durationInSeconds


def timestampDir(dirname):
    tsfile = os.path.join(dirname, '.chk-ts')

    with open(tsfile, 'w') as f:
        f.write(str(time.time()))


def firstDirIn(dirname, startswith=''):
    return [os.path.join(dirname, d) for d in os.listdir(dirname) if os.path.isdir(os.path.join(dirname, d)) and (startswith=='' or os.path.basename(d).startswith(startswith))][0]


def filesAllExist(files, basedir=''):
    return all([os.path.isfile(os.path.join(basedir, f)) for f in files])

def dlPackagesIn(pkgdefs, superrepo='build'):
    os_utils.ensureDirExists('download')
    for destination, retrievalData in pkgdefs.items():
        rebuild=args.rebuild_all or destination in args.rebuild
        destination = os.path.join(superrepo, destination)
        dlType = retrievalData['type']
        if dlType == 'git':
            remote = retrievalData.get('remote', 'origin')
            branch = retrievalData.get('branch', 'master')
            commit = retrievalData.get('commit')
            submodules = retrievalData.get('submodules', False)
            submodules_remote = retrievalData.get('submodules_remote', False)
            tag = retrievalData.get('tag')
            if 'uri' not in retrievalData:
                log.critical('uri not in def for %s', destination)
            git = GitRepository(destination, retrievalData['uri'], quiet=True, noisy_clone=True)
            with log.info('Checking for updates to %s...', destination):
                if rebuild or not os.path.isdir(destination):
                    if rebuild or git.CheckForUpdates(remote, branch, tag=tag, commit=commit):
                        log.info('Updates detecting, pulling...')
                        git.Pull(remote, branch, tag=tag, commit=commit, cleanup=True)
                    if submodules:
                        if rebuild:
                            with os_utils.Chdir(destination):
                                os_utils.cmd(['git', 'submodule', 'foreach', '--recursive', 'git clean -dfx'], echo=True, show_output=True, critical=True)
                        git.UpdateSubmodules(submodules_remote)
        elif dlType == 'hg':
            remote = retrievalData.get('remote', 'default')
            branch = retrievalData.get('branch', 'master')
            commit = retrievalData.get('commit', retrievalData.get('tag'))
            if 'uri' not in retrievalData:
                log.critical('uri not in def for %s', destination)
            hg = HgRepository(destination, retrievalData['uri'], quiet=True, noisy_clone=True)
            with log.info('Checking for updates to %s...', destination):
                if rebuild or not os.path.isdir(destination):
                    if rebuild or hg.CheckForUpdates(remote, branch):
                        log.info('Updates detecting, pulling...')
                        hg.Pull(remote, branch, commit, cleanup=True)
        elif dlType == 'http':
            url = retrievalData['url']
            ext = retrievalData.get('ext', url[url.rfind('.'):])
            filename = os.path.join(script_dir, 'download', retrievalData.get('filename', hashlib.md5(url).hexdigest() + ext))
            if not os.path.isfile(filename):
                with log.info('Downloading %s...', url):
                    http.DownloadFile(url, filename)
            if (rebuild or not os.path.isdir(destination)) and not retrievalData.get('download-only', False):
                if rebuild:
                    os_utils.safe_rmtree(destination)
                os_utils.ensureDirExists(destination)
                with os_utils.Chdir(destination):
                    os_utils.decompressFile(filename)


def gen_userfile_content(projdir):
    '''
    @author Sebastian Herbord
    '''
    with codecs.open(os.path.join(script_dir, "CMakeLists.txt.user.template"), 'r') as f:
        res = Formatter().vformat(f.read(), [], FormatDict({
            'build_dir': projdir,
            'environment_id': config['qt.environment_id'],
            'profile_name': config['qt.profile_name'],
            'profile_id': config['qt.profile_id']
        }))
        return res

class Builder(object):
    def __init__(self, name, cfg, build_dir):
        #: Used for selectively rebuilding projects.
        self.name=name

        self.build_dir=build_dir
        self.builder_meta_file=os.path.join(self.build_dir,'.builder')

        #: Files and directories that must be present in order for the build to be considered a success
        self.expected=[]

        #: Used to generate a snapshot of before/after file states. Useful for building self.expected.
        self.preExisting=[]

        self.build_cfg = cfg
        self.configuration={'build_cfg':self.build_cfg}
        if self.build_cfg is None:
            self.expected=self.build_cfg.get('expected')

    def TryBuild(self):
        if not self.shouldBuild(): return True
        if not self.Build(): return False
        self.Install()
        return self.updateManifest()

    def Build(self):
        '''
        True: Build succeeded.
        False: Build failed.
        '''
        return True

    def Clean(self):
        '''
        Cleans and deconfigures package.
        '''
        pass

    def Install(self):
        if 'manual-install' in self.build_cfg:
            for pattern, destination in self.build_cfg.get('manual-install',{}).items():
                installbasedir = os.path.basename(pattern)
                pattern = os.path.dirname(pattern)
                for installfilename in os.listdir(os.path.join(self.build_dir)):
                    if fnmatch.fnmatch(installfilename, pattern):
                        os_utils.single_copy(installfilename, destination, verbose=True)

    def shouldBuild(self):
        if args.rebuild_all:
            return True

        if self.name in args.get_snapshot:
            log.info('Getting snapshot of %s (%s pre-build)...',script_dir,self.name)
            for root, _, files in os.walk(script_dir):
                for filename in files:
                    self.preExisting.append(os.path.relpath(os.path.abspath(os.path.join(root,filename)),script_dir))
        if self.name in args.rebuild:
            return True

        if not os.path.isdir(self.build_dir):
            return True
        if not os.path.isfile(self.builder_meta_file):
            return True

        manifest={}
        with open(self.builder_meta_file,'r') as f:
            data=yaml.load(f)
            manifest=data.get('manifest',{})

        for expectedFile in self.expected:
            if not os.path.isfile(expectedFile): return True
            relfilepath=os.path.relpath(expectedFile,script_dir)
            if relfilepath not in manifest: return True
            if os.stat(expectedFile).m_time != manifest[relfilepath]: return True
        return False

    def updateManifest(self):
        currentFiles=[]
        newfiles=[]
        if self.name in args.get_snapshot:
            log.info('Getting snapshot of %s (%s post-build)...',script_dir,self.name)
            for root, _, files in os.walk(script_dir):
                for filename in files:
                    currentFiles.append(os.path.relpath(os.path.abspath(os.path.join(root,filename)),script_dir))
            log.info('Comparing...')
            newfiles=[fn.replace('\\','/') for fn in currentFiles if fn not in self.preExisting]

        newmanifest={}
        with log.info('Checking for %d expected files...',len(self.expected)):
            for expectedFile in self.expected:
                if not os.path.isfile(expectedFile):
                    log.error('MISSING %s',expectedFile)
                    return False
                relfilepath=os.path.relpath(expectedFile,self.build_dir)
                newmanifest[relfilepath]=os.stat(expectedFile).m_time
            log.info('All check out!')

        with open(self.builder_meta_file,'w') as f:
            yaml.dump({'configuration':self.configuration,'manifest':newmanifest,'newfiles':newfiles},f,default_flow_style=False)

        return True

class CMakeBuilder(Builder):
    def __init__(self, name, cfg, build_dir):
        super(CMakeBuilder,self).__init__(name,cfg,build_dir)
        self.cmake = CMake()

    def Build(self):
        with os_utils.Chdir(self.build_dir):
            self.cmake.setFlag('CMAKE_BUILD_TYPE', config.get('build-type'))
            cmake_opts=self.build_cfg.get('cmake',{})
            cmake_flags=cmake_opts.get('flags',{})
            for k,v in cmake_flags.items():
                self.cmake.setFlag(k,v)
            #self.cmake.setFlag('CMAKE_INSTALL_PREFIX', self.install_prefix)
            self.cmake.generator = 'NMake Makefiles'
            self.cmake.run(CMAKE=EXECUTABLES['cmake'])
            if cmake_opts.get('build',False):
                target=''
                if cmake_flags.get('CMAKE_INSTALL_PREFIX') is not None:
                    target='install'
                target=cmake_opts.get('build-target',target)
                self.cmake.build(target=target, CMAKE=EXECUTABLES['cmake'])
        return True


argp = argparse.ArgumentParser()
argp.add_argument('--reconf-qt', action='store_true', help='Cleans and reconfigures Qt.')
#argp.add_argument('--force-download', action='store_true', help='Cleans and redownloads projects and dependencies. Use when prerequisites.yml changes.')
argp.add_argument('--rebuild-all', action='store_true', help='Clean and rebuild all projects and dependencies.')
argp.add_argument('--rebuild', action='append', help='Clean and rebuild the dependency or project specified.', default=[])
argp.add_argument('--get-snapshot', action='append', help='Generate file diff based on before and after snapshots of a project. Useful for expectations.', default=[])
args = argp.parse_args()

# This just sets defaults.  Screw with build.yml instead.
config = {
    'tools': {
        'make': "nmake",
    },
    'architecture': 'x86_64',
    'vc_version':   '14.0',
    'build-type': "RelWithDebInfo",
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
userconfig = {
    'paths': {
        'executables':{
            '7za': os_utils.which('7z.exe'),
            'cmake': os_utils.which('cmake.exe'),
            'git': os_utils.which('git.exe'),
            'graphviz': 'C:\\Program Files (x86)\\Graphviz2.38\\bin\\dot.exe',  # Default Graphviz2 install
            'hg': os_utils.which('hg.exe'),
            'perl': 'C:\\Perl64\\bin\\perl.exe',  # ActiveState
            'python': os_utils.which('python.exe'),
            'ruby': os_utils.which('ruby.exe'),
            'svn': os_utils.which('svn.exe'),
        },
        'qt-base': 'C:\\Qt\\Qt5.5.1\\5.5\\msvc2013_64', # Not used. Yet.
    },
    'build': {
        'job-count': multiprocessing.cpu_count() * 2
    }
}

config = YAMLConfig('build.yml', config, variables={'nbits': '32'})
config.Load('user-config.yml', merge=True, defaults=userconfig)
EXECUTABLES = config.get('paths.executables')

ENV.appendTo('PATH', os.path.dirname(EXECUTABLES['7za']))
ENV.set('QMAKESPEC', config.get('qt-makespec', 'win32-msvc2013'))

#: x64 or x86
short_arch = 'x64' if config['architecture'] == 'x86_64' else 'x86'
#: 64 or 32
nbits = "64" if config['architecture'] == 'x86_64' else "32"

superrepo = os.path.join('build', 'modorganizer_super')
if not os.path.isdir(superrepo):
    os_utils.ensureDirExists(superrepo)
    with os_utils.Chdir(superrepo):
        os_utils.cmd([EXECUTABLES['git'], 'init'], show_output=True, critical=True)

ymlvars={'nbits': nbits, 'script_dir':script_dir}
prerequisites = YAMLConfig('prerequisites.yml', variables=ymlvars).cfg
with log.info('Downloading prerequisites...'):
    dlPackagesIn(prerequisites)

# Copied from Unimake.
projs = [
    ("modorganizer-archive",           "archive",           "master",          ["7zip", "Qt5"]),
    ("modorganizer-uibase",            "uibase",            "new_vfs_library", ["Qt5", "boost"]),
    ("modorganizer-lootcli",           "lootcli",           "master",          ["LootApi", "boost"]),
    ("modorganizer-esptk",             "esptk",             "master",          ["boost"]),
    ("modorganizer-bsatk",             "bsatk",             "master",          ["zlib"]),
    ("modorganizer-nxmhandler",        "nxmhandler",        "master",          ["Qt5"]),
    ("modorganizer-helper",            "helper",            "master",          ["Qt5"]),
    ("modorganizer-game_gamebryo",     "game_gamebryo",     "new_vfs_library", ["Qt5", "modorganizer-uibase",
                                                                                "modorganizer-game_features"]),
    ("modorganizer-game_oblivion",     "game_oblivion",     "master",          ["Qt5", "modorganizer-uibase",
                                                                                "modorganizer-game_gamebryo",
                                                                                "modorganizer-game_features"]),
    ("modorganizer-game_fallout3",     "game_fallout3",     "master",          ["Qt5", "modorganizer-uibase",
                                                                                "modorganizer-game_gamebryo",
                                                                                "modorganizer-game_features"]),
    ("modorganizer-game_fallout4",     "game_fallout4",     "master",          ["Qt5", "modorganizer-uibase",
                                                                                "modorganizer-game_gamebryo",
                                                                                "modorganizer-game_features"]),
    ("modorganizer-game_falloutnv",    "game_falloutnv",    "master",          ["Qt5", "modorganizer-uibase",
                                                                                "modorganizer-game_gamebryo",
                                                                                "modorganizer-game_features"]),
    ("modorganizer-game_skyrim",       "game_skyrim",       "master",          ["Qt5", "modorganizer-uibase",
                                                                                "modorganizer-game_gamebryo",
                                                                                "modorganizer-game_features"]),
    ("modorganizer-game_skyrim_se",    "game_skyrimse",     "master",          ["Qt5", "modorganizer-uibase",
                                                                                "modorganizer-game_gamebryo",
                                                                                "modorganizer-game_features"]),
    ("modorganizer-tool_inieditor",    "tool_inieditor",    "master",          ["Qt5", "modorganizer-uibase"]),
    ("modorganizer-tool_inibakery",    "tool_inibakery",    "master",          ["modorganizer-uibase"]),
    ("modorganizer-tool_configurator", "tool_configurator", "master",          ["PyQt5"]),
    ("modorganizer-preview_base",      "preview_base",      "master",          ["Qt5", "modorganizer-uibase"]),
    ("modorganizer-diagnose_basic",    "diagnose_basic",    "master",          ["Qt5", "modorganizer-uibase"]),
    ("modorganizer-check_fnis",        "check_fnis",        "master",          ["Qt5", "modorganizer-uibase"]),
    ("modorganizer-installer_bain",    "installer_bain",    "master",          ["Qt5", "modorganizer-uibase"]),
    ("modorganizer-installer_manual",  "installer_manual",  "master",          ["Qt5", "modorganizer-uibase"]),
    ("modorganizer-installer_bundle",  "installer_bundle",  "master",          ["Qt5", "modorganizer-uibase"]),
    ("modorganizer-installer_quick",   "installer_quick",   "master",          ["Qt5", "modorganizer-uibase"]),
    ("modorganizer-installer_fomod",   "installer_fomod",   "master",          ["Qt5", "modorganizer-uibase"]),
    ("modorganizer-installer_ncc",     "installer_ncc",     "master",          ["Qt5", "modorganizer-uibase", "NCC"]),
    ("modorganizer-bsa_extractor",     "bsa_extractor",     "master",          ["Qt5", "modorganizer-uibase"]),
    ("modorganizer-plugin_python",     "plugin_python",     "master",          ["Qt5", "boost", "modorganizer-uibase",
                                                                                "sip"]),
    ("modorganizer",                   "modorganizer",      "new_vfs_library", ["Qt5", "boost",
                                                                                "modorganizer-uibase", "modorganizer-archive",
                                                                                "modorganizer-bsatk", "modorganizer-esptk",
                                                                                "modorganizer-game_features",
                                                                                "usvfs"]),
]
projectdefs = {}
for projRepo, projdir, branch, _ in projs:
    projectdefs[projdir] = {
        'type': 'git',
        'uri': 'https://github.com/{}/{}'.format('Viomi' if projdir != 'game_skyrimse' else 'TanninOne', projRepo)
    }
    if branch != 'master':
        projectdefs[projdir]['branch'] = branch
projectdefs = YAMLConfig('projects.yml', default=projectdefs, variables=ymlvars, ordered_dicts=True).cfg
with log.info('Downloading projects...'):
    dlPackagesIn(projectdefs, superrepo=superrepo)

os_utils.getVSVars(config.get('paths.visual-studio'), short_arch, os.path.join('build', 'getvsvars.bat'))
# print(ENV.get('LIBPATH'))

# Fixes problems with the wrong Qt being on the PATH. (I usually dev with Qt4.8.7)
ENV.prependTo('PATH', os.path.join(config.get('paths.qt-base'), 'bin'))
ENV.removeDuplicatedEntries('PATH', noisy=True)

libdir = os.path.join(script_dir, 'install', 'lib')
includedir = os.path.join(script_dir, 'install', 'include')
ENV.prependTo('LIBPATH', libdir)
ENV.prependTo('LIB', libdir)
ENV.prependTo('INCLUDE', includedir)
os_utils.ensureDirExists(libdir)

# This part written mostly by Sebastian. (Qt profile detection)
qtappdata_root = os.path.join(ENV.get('APPDATA'), 'QtProject')
qtcreator_config_path = os.path.join(qtappdata_root, 'qtcreator.ini')
try:
    if os.path.isfile(qtcreator_config_path):
        from ConfigParser import RawConfigParser
        parser = RawConfigParser()
        parser.read(os.path.join(qtcreator_config_path, "qtcreator.ini"))
        config['qt.environment_id'] = parser.get('ProjectExplorer', 'Settings\\EnvironmentId')
        config['qt.environment_id'] = re.sub(r"@ByteArray\((.*)\)", r"\1", config['qt_environment_id'])

        import xml.etree.ElementTree as ET
        tree = ET.parse(os.path.join(qtappdata_root, "qtcreator", "profiles.xml"))
        root = tree.getroot()

        profiles = []

        for profile in root.findall("data/valuemap"):
            profiles.append((profile.find("value[@key='PE.Profile.Id']").text,
                             profile.find("value[@key='PE.Profile.Name']").text))

        arch = nbits + 'bit'
        profiles = filter(lambda x: arch in x[1], sorted(profiles, reverse=True))[0]
        config['qt.profile.id'] = profiles[0]
        config['qt.profile.name'] = profiles[1].replace("%{Qt:Version}", "5.5.1")
except Exception as e:
    log.error(e)

#####################################
# PREREQUISITES
#####################################
# This should probably be dumped into seperate modules or something, but this'll do for now.
CMakeBuilder('zlib', prerequisites['zlib'], firstDirIn(os.path.join(script_dir, 'build', 'zlib'), startswith='zlib-')).TryBuild()

'''
zlib_dir = firstDirIn(os.path.join(script_dir, 'build', 'zlib'), startswith='zlib-')
with log.info('Building zlib...'):
    with os_utils.Chdir(zlib_dir):
        cmake = CMake()
        cmake.setFlag('CMAKE_BUILD_TYPE', config.get('cmake.build-type'))
        cmake.setFlag('CMAKE_INSTALL_PREFIX', os.path.join(script_dir, 'build', 'zlib')) # This LOOKS wrong but it's actually fine.
        cmake.generator = 'NMake Makefiles'
        cmake.run(CMAKE=EXECUTABLES['cmake'])
        cmake.build(target='install', CMAKE=EXECUTABLES['cmake'])
'''

winopenssl_dir = os.path.join(script_dir, 'build', 'win{}openssl'.format(nbits))
libeay = "libeay32MD.lib"
ssleay = "ssleay32MD.lib"
libeay_path = os.path.join(winopenssl_dir, "lib", "VC", "static", libeay)
ssleay_path = os.path.join(winopenssl_dir, "lib", "VC", "static", ssleay)
with log.info('Installing Win{}OpenSSL...'.format(nbits)):
    if not args.rebuild_all and os.path.isfile(libeay_path) and os.path.isfile(ssleay_path):
        log.info('Skipping; Both libeay and ssleay are present.')
    else:
        log.warn('*' * 30)
        log.warn('Because of stuff outside of my control, you will get a UAC prompt (probably) and a warning about command prompts.')
        log.warn('1. Hit "Yes" on the UAC prompt (assuming you get one).')
        log.warn('2. Press "OK" on the warning about command prompts.')
        log.warn('*' * 30)
        os_utils.cmd([os.path.join(script_dir, 'download', prerequisites['win{}openssl'.format(nbits)]['filename']), "/VERYSILENT", "/DIR={}".format(winopenssl_dir)], echo=True, critical=True, show_output=False)
        wait_counter = 15
        while wait_counter > 0:
            if os.path.isfile(libeay_path) and os.path.isfile(ssleay_path):
                break
            else:
                time.sleep(1.0)
                wait_counter -= 1
        # wait a bit longer because the installer may have been in the process of writing the file
        time.sleep(1.0)

        if wait_counter <= 0:
            log.error("Unpacking of OpenSSL timed out")
            sys.exit(1)  # We timed out and nothing was installed

CMakeBuilder('googletest', prerequisites['googletest'], os.path.join(script_dir, 'build', 'googletest')).TryBuild()
'''
gtest_dir = os.path.join(script_dir, 'build', 'googletest')
with log.info('Building GoogleTest...'):
    if not args.rebuild_all and filesAllExist(['gmock_main.lib', 'gmock.lib', 'gtest_main.lib', 'gtest.lib'], basedir='install/lib'):
        log.info('Skipping; All needed files built.')
    else:
        with os_utils.Chdir(gtest_dir):
            cmake = CMake()
            cmake.setFlag('CMAKE_BUILD_TYPE', config.get('cmake.build-type'))
            cmake.setFlag('CMAKE_INSTALL_PREFIX', os.path.join(script_dir, 'install'))
            cmake.setFlag('gtest_force_shared_crt:BOOL', 'ON')
            cmake.generator = 'NMake Makefiles'
            cmake.run(CMAKE=EXECUTABLES['cmake'])
            cmake.build(CMAKE=EXECUTABLES['cmake'])
'''

asmjit_dir = os.path.join(script_dir, 'build', 'asmjit')
with log.info('Building asmjit...'):
    if not args.rebuild_all and filesAllExist(['asmjit.lib'], basedir='install/lib'):
        log.info('Skipping; All needed files built.')
    else:
        with os_utils.Chdir(asmjit_dir):
            cmake = CMake()
            cmake.setFlag('ASMJIT_STATIC', 'TRUE')
            cmake.setFlag('ASMJIT_DISABLE_COMPILER', 'TRUE')
            cmake.setFlag('CMAKE_BUILD_TYPE', config.get('cmake.build-type'))
            cmake.setFlag('CMAKE_INSTALL_PREFIX', os.path.join(script_dir, 'install').replace('\\', '/'))
            cmake.generator = 'NMake Makefiles'
            cmake.run(CMAKE=EXECUTABLES['cmake'])
            cmake.build(CMAKE=EXECUTABLES['cmake'], target='install')

# MUST be built with system Python because it includes pyexpat.
udis_dir = firstDirIn(os.path.join(script_dir, 'build', 'udis86'))
with log.info('Building udis...'):
    with os_utils.Chdir(udis_dir):
        os_utils.cmd([EXECUTABLES['python'], 'scripts/ud_itab.py', 'docs/x86/optable.xml', 'libudis86'], echo=True, show_output=True, critical=True)
        cpp = WindowsCCompiler('libudis86.lib')
        cpp.files = ["libudis86/decode.c",
                     "libudis86/itab.c",
                     "libudis86/syn.c",
                     "libudis86/syn-att.c",
                     "libudis86/syn-intel.c",
                     "libudis86/udis86.c"]
        cpp.compiler = ENV.which('cl')
        cpp.linker = ENV.which('link')
        cpp.compile()

        os_utils.single_copy(os.path.join(udis_dir, 'libudis86.lib'), libdir)

qt5_dir = os.path.join(script_dir, 'build', 'qt5')
qt5git_dir = os.path.join(script_dir, 'build', 'qt5-git')
with log.info('Building Qt5...'):
    webkit_env = None
    ENV.prependTo('PATH', os.path.join(qt5_dir, 'bin'))
    if (args.rebuild_all or 'qt' in args.rebuild) and filesAllExist([os.path.join(qt5_dir, 'translations', 'qtdeclarative_uk.qm')]):
        log.info('Skipping; Needed files exist.')
    else:
        with os_utils.Chdir(qt5git_dir):
            skip_list = ["qtactiveqt", "qtandroidextras", "qtenginio",
                         "qtserialport", "qtsvg", "qtwebengine",
                         "qtwayland", "qtdoc", "qtconnectivity", "qtwebkit-examples"]

            os_utils.cmd([EXECUTABLES['perl'], 'init-repository', '--module-subset=' + ','.join(['all'] + ['-' + x for x in skip_list])], echo=True, show_output=True, critical=False)
            nomake_list = ["tests", "examples"]

            num_jobs = config.get('build.job-count', multiprocessing.cpu_count() * 2)
            log.info('jom -j (maximum job count) set to %d.  If you want fewer, please set build.job-count in user-config.yml.')

            grep_path = os.path.join(script_dir, 'build', 'grep')
            os_utils.ensureDirExists(grep_path)
            os_utils.copytree(os.path.join(script_dir, 'build', 'grep-dep'), grep_path)
            os_utils.copytree(os.path.join(script_dir, 'build', 'grep-bin'), grep_path)

            #ENV.set('OPENSSL_LIBS', '-lssleay32MD -llibeay32MD -lgdi32 -lUser32')

            webkit_env = ENV.clone()
            webkit_env.appendTo('PATH', os.path.join(grep_path, 'bin'))
            webkit_env.appendTo('PATH', os.path.join(script_dir, 'build', 'flex'))
            webkit_env.appendTo('PATH', os.path.join(qt5git_dir, 'gnuwin32', 'bin'))

            configure_cmd = ['cmd', '/c', "configure.bat",
                             "-platform", config.get('qt-makespec', 'win32-msvc2013'),
                             "-debug-and-release", "-force-debug-info",
                             "-opensource", "-confirm-license",
                             "-mp", "-no-compile-examples",
                             "-no-angle", "-opengl", "desktop",
                             "-ssl", "-openssl-linked",
                             #'OPENSSL_LIBS=-lssleay32MD -llibeay32MD -lgdi32 -lUser32', # No quotes, unless the file has a space in its name.
                             "-I", os.path.join(winopenssl_dir, "include").format(nbits),
                             "-L", os.path.join(winopenssl_dir, 'lib').format(nbits),  # original
                             #"-L", os.path.join(winopenssl_dir,'lib','VC','static').format(nbits), # Static linking
                             "-prefix", qt5_dir] \
                + list(itertools.chain(*[("-skip", s) for s in skip_list])) \
                + list(itertools.chain(*[("-nomake", n) for n in nomake_list]))
            newConfMD5 = hashlib.md5(repr(configure_cmd)).hexdigest()
            storedConfMD5 = ''
            confrecord = os.path.join(qt5git_dir, '.config_cmd')
            if os.path.isfile(confrecord):
                with open(confrecord, 'r') as f:
                    storedConfMD5 = f.read().strip()
            reconf = False
            if args.rebuild_all:
                log.info('--reconf-qt set, cleaning.')
                reconf = True
            if not reconf and (storedConfMD5 != '' and newConfMD5 != storedConfMD5):
                log.info('Build configuration changed, cleaning.')
                reconf = True
            if reconf:
                os_utils.cmd(['git', 'submodule', 'foreach', '--recursive', "git clean -dfx"], echo=True)

            # Windows SDK and -Zc:strictStrings don't mix, so we disable them.
            qmake_spec_file = os.path.join(qt5git_dir, 'qtbase', 'mkspecs', config.get('qt-makespec', 'win32-msvc2013'), 'qmake.conf')
            NEW_LINES = [
                'QMAKE_CXXFLAGS_RELEASE -= -Zc:strictStrings',
                'QMAKE_CFLAGS_RELEASE -= -Zc:strictStrings',
                'QMAKE_CFLAGS -= -Zc:strictStrings',
                'QMAKE_CXXFLAGS -= -Zc:strictStrings'
            ]
            LinesFound = []
            with open(qmake_spec_file, 'r') as f:
                for line in f:
                    s_line = line.strip()
                    if s_line in NEW_LINES:
                        LinesFound.append(s_line)

            with log.info('Patching %s', qmake_spec_file):
                with open(qmake_spec_file, 'a') as f:
                    for line in NEW_LINES:
                        if line in LinesFound:
                            continue
                        f.write(line + '\n')

            os_utils.cmd(configure_cmd, echo=True, show_output=True, critical=True)
            with open(confrecord, 'w') as f:
                f.write(newConfMD5)
            os_utils.cmd([os.path.join(script_dir, 'build', 'jom', 'jom.exe'), '-j', str(num_jobs)], echo=True, show_output=True, critical=True)
            os_utils.cmd([config.get('tools.make'), 'install'], echo=True, show_output=True, critical=True)
        '''
        Webkit is apparently turbo-fucked.
        with os_utils.Chdir(qt5_dir):

            webkit_patch = patch.Replace("qtwebkit/Source/WebCore/platform/text/TextEncodingRegistry.cpp",
                                         "#if OS(WINDOWS) && USE(WCHAR_UNICODE)",
                                         "#if OS(WINCE) && USE(WCHAR_UNICODE)")


            os_utils.cmd([EXECUTABLES['perl'], os.path.join('Tools','Scripts','build_webkit'),'--qt','--release'], echo=True, show_output=True, critical=True)
        '''


python_dir = firstDirIn(os.path.join(script_dir, 'build', 'python'))
EXECUTABLES['python'] = ''
with log.info('Building Python 2.7...'):
    path_segments = [python_dir, "PCbuild"]
    if config['architecture'] == "x86_64":
        path_segments.append("amd64")
    basedir = os.path.join(*path_segments)
    if not args.rebuild_all and filesAllExist([
        os.path.join(script_dir, 'install', 'lib', 'python27.lib'),
        os.path.join(includedir, 'pyconfig.h')
    ]):
        log.info('Skipping; All files present.')
    else:
        with os_utils.Chdir(python_dir):
            os_utils.cmd(['cmd', '/c', os.path.join('PCBuild', 'get_externals.bat')], echo=True, critical=True)

            # Python 2.7.12 doesn't need upgrades, so no more popups!

            msb = MSBuild()
            msb.solution = 'PCBuild/PCBuild.sln'
            msb.configuration = 'Release'
            msb.platform = short_arch
            msb.run(ENV.which('msbuild'), project='python')
            for filename in os.listdir(basedir):
                print(filename)
                if filename.endswith('.lib'):
                    os_utils.single_copy(os.path.join(basedir, filename), libdir)
            os_utils.single_copy(os.path.join(python_dir, 'PC', 'pyconfig.h'), includedir)
    EXECUTABLES['python'] = os.path.join(*path_segments + ['python.exe'])

boost_dir = firstDirIn(os.path.join(script_dir, 'build', 'boost'))
with log.info('Building Boost...'):
    if not args.rebuild_all and os.path.isdir(os.path.join(boost_dir, 'stage', 'lib')):
        log.info('Skipping; All needed files built.')
    else:
        with os_utils.Chdir(boost_dir):
            boost_components = [
                "date_time",
                "coroutine",
                "filesystem",
                "python",
                "thread",
                "log",
                "locale"
            ]
            log.info('Writing user-config.jam...')
            with codecs.open('user-config.jam', 'w', encoding='utf-8') as f:
                f.write((("using python : 2.7 : {2}\n"
                          "  : {0}\\include\n"
                          "  : {0}\\lib\n"
                          "  : <address-model>{1} ;").format(python_dir, nbits, EXECUTABLES['python'])))
            os_utils.cmd(['cmd', '/c', 'bootstrap.bat'], echo=True, show_output=True, critical=True)
            os_utils.cmd(['b2.exe', 'address-model=' + nbits, 'toolset=msvc-14.0', 'link=shared'] + ["--with-{0}".format(component) for component in boost_components], echo=True, show_output=True, critical=True)

sip_dir = firstDirIn(os.path.join(script_dir, 'build', 'sip'))
with log.info('Building sip...'):
    if not args.rebuild_all and filesAllExist([os.path.join(python_dir, 'sip.exe')]):
        log.info('Skipping; All needed files built.')
    else:
        with os_utils.Chdir(sip_dir):
            ENV.appendTo('LIBPATH', os.path.join(script_dir, 'install', 'lib'))
            ENV.appendTo('LIB', os.path.join(script_dir, 'install', 'lib'))
            os_utils.cmd([EXECUTABLES['python'], "configure.py",
                          "-b", python_dir,
                          "-d", os.path.join(python_dir, "Lib", "site-packages"),
                          "-v", os.path.join(python_dir, "sip"),
                          "-e", os.path.join(python_dir, "include")
                          ], echo=True, critical=True, show_output=True, env=ENV)
            os_utils.cmd([config.get('tools.make'), 'install'], echo=True, critical=True, env=ENV)

pyqt_dir = firstDirIn(os.path.join(script_dir, 'build', 'pyqt'))
with log.info('Building PyQt5...'):
    if not args.rebuild_all and filesAllExist([os.path.join(python_dir, 'pyuic5.bat')]):
        log.info('Skipping; All needed files built.')
    else:
        with os_utils.Chdir(pyqt_dir):
            pyqt5_env = ENV.clone()
            pyqt5_env.appendTo('PATH', os.path.join(sip_dir, 'sipgen'))
            os_utils.cmd([EXECUTABLES['python'], "configure.py", "--confirm-license", '--verbose',
                          "-b", python_dir,
                          "-d", os.path.join(python_dir, "Lib", "site-packages"),
                          "-v", os.path.join(python_dir, "sip", "PyQt5"),
                          '--spec', config.get('qt-makespec'),
                          "--sip-incdir", os.path.join(python_dir, "Include")], echo=True, critical=True, show_output=True)
            os_utils.cmd([config.get('tools.make'), 'install'], env=pyqt5_env, echo=True, critical=True)
            os_utils.single_copy(os.path.join(qt5_dir, "bin", "Qt5Core.dll"), python_dir)
            os_utils.single_copy(os.path.join(qt5_dir, "bin", "Qt5Xml.dll"), python_dir)

lootapi_dir = firstDirIn(os.path.join(script_dir, 'build', 'loot-api'))
with log.info('Installing Loot API...'):
    lootdest = os.path.join(script_dir, 'install', 'bin', 'loot')
    os_utils.ensureDirExists(lootdest)
    os_utils.single_copy(os.path.join(lootapi_dir, 'loot_api.dll'), os.path.join(script_dir, 'install', 'bin', 'loot'))

# TODO: Replace with mopm in the future
nmm_dir = os.path.join(script_dir, 'build', 'ncc', 'NMM')
ncc_dir = os.path.join(script_dir, 'build', 'ncc', 'NexusClientCli')
with log.info('Building NCC...'):
    if not args.rebuild_all and filesAllExist([os.path.join(script_dir, 'install', 'bin', 'ncc', 'NexusClientCLI.exe')]):
        log.info('Skipping; All needed files built.')
    else:
        # We patch it LIVE now.
        # with os_utils.Chdir(ncc_dir):
        #    os_utils.single_copy(os.path.join(ncc_dir,'NexusClient.sln'), nmm_dir, ignore_mtime=True)
        with os_utils.Chdir(nmm_dir):
            # And this is why I use buildtools everywhere: Because it has shit like this.
            sln = VisualStudio2015Solution()
            sln.LoadFromFile('NexusClient.sln')
            ncc_csproj = os.path.relpath(os.path.join(ncc_dir, 'NexusClientCLI', 'NexusClientCLI.csproj'))
            if not os.path.isfile(ncc_csproj):
                log.critical('NOT FOUND: %s', ncc_csproj)
            else:
                log.info('FOUND: %s', ncc_csproj)
            changed = False
            projfile = VS2015Project()
            projfile.LoadFromFile(ncc_csproj)
            projguid = projfile.PropertyGroups[0].element.find('ProjectGuid').text
            log.info('ProjectGuid = %s', projguid)
            if "NexusClientCli" not in sln.projectsByName:
                newproj = sln.AddProject('NexusClientCli', ProjectType.CSHARP_PROJECT, ncc_csproj, guid=projguid)
                log.info('Adding project %s (%s) to NexusClient.sln', newproj.name, newproj.guid)
                changed = True
            else:
                newproj = sln.projectsByName['NexusClientCli']
                log.info('Project %s (%s) already exists in NexusClient.sln', newproj.name, newproj.guid)
                if newproj.projectfile != ncc_csproj:
                    log.info('Changing projectfile: %s -> %s', newproj.projectfile, ncc_csproj)
                    newproj.projectfile = ncc_csproj
                    changed = True
            if changed:
                log.info('Writing NexusClientCli.sln')
                sln.SaveToFile('NexusClientCli.sln')  # So we don't get conflicts when pulling.

            '''
            MSBuild doesn't build properly due to https://github.com/Microsoft/msbuild/issues/417
            msb = MSBuild()
            msb.solution = os.path.join(nmm_dir,'NexusClientCli.sln')
            msb.platform = 'AnyCPU' # 'Any CPU' will not build FOMod etc.
            msb.configuration = 'Debug' if config.get('build-type') == 'Debug' else 'Release'
            msb.run(ENV.which('msbuild'), project='NexusClientCli', env=ENV)
            '''
            os_utils.cmd(['devenv', 'NexusClientCli.sln', '/build', 'Debug' if config.get('build-type') == 'Debug' else 'Release'], echo=True, show_output=True, critical=True)

        with os_utils.Chdir(ncc_dir):
            # The Powershell shit is broken and outdated, so I'm just going to copy everything.
            #debugOrRelease = "-debug" if config['build-type'] == "Debug" else "-release"
            #os_utils.cmd(['powershell', '.\\publish.ps1', debugOrRelease, '-outputPath', os.path.join(script_dir, 'install', 'bin')], echo=True, critical=True)
            debugOrRelease = 'Debug' if config.get('build-type') == 'Debug' else 'Release'
            os_utils.copytree(os.path.join(nmm_dir, 'bin', debugOrRelease), os.path.join(script_dir, 'install', 'bin', 'ncc'), ignore=('.pdb', '.xml'), verbose=True)

with log.info('Installing Spdlog...'):
    spdlog_dir = os.path.join(script_dir, 'build', 'spdlog')
    os_utils.copytree(os.path.join(spdlog_dir, 'include'), os.path.join(script_dir, 'install', 'include'), verbose=True)

###########################################
# PROJECTS
###########################################

cmake_parameters = {}
cmake_parameters['CMAKE_BUILD_TYPE'] = config["build-type"]
cmake_parameters['DEPENDENCIES_DIR'] = os.path.join(script_dir, 'build')
cmake_parameters['CMAKE_INSTALL_PREFIX:PATH'] = os.path.join(script_dir, 'install')


if config.get('optimize', False):
    cmake_parameters['OPTIMIZE_LINK_FLAGS'] = '/LTCG /INCREMENTAL:NO /OPT:REF /OPT:ICF'


usvfs_dir = os.path.join(superrepo, 'usvfs')
with log.info('Building USVFS...'):
    with os_utils.Chdir(usvfs_dir):
        cmake = CMake()
        cmake.flags = cmake_parameters.copy()
        cmake.setFlag('PROJ_ARCH', short_arch)
        cmake.generator = 'NMake Makefiles'  # Was CodeBlocks - NMake Makefiles
        cmake.run(CMAKE=EXECUTABLES['cmake'])
        cmake.build(CMAKE=EXECUTABLES['cmake'], target='install')

projectBuildInfo = {}
for projectName, projectCfg in projectdefs.items():
    projdir = os.path.join(superrepo, projectName)
    projectBuildInfo[projectName] = {'config': projectCfg, 'dir': projdir}
    with log.info('Building %s...', projectName):
        with os_utils.Chdir(projdir):
            cmake = CMake()
            cmake.flags = cmake_parameters.copy()
            cmake.setFlag('MO_ARCH', short_arch)
            cmake.setFlag('MO_NBITS', nbits)
            cmake.generator = 'NMake Makefiles'  # Was CodeBlocks - NMake Makefiles
            cmake.run(CMAKE=EXECUTABLES['cmake'])
            with codecs.open('CMakeLists.txt.user', 'w', encoding='utf-8') as f:
                f.write(gen_userfile_content(projdir))
            cmake.build(CMAKE=EXECUTABLES['cmake'], target='install')
