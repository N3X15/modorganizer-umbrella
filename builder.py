import hashlib
import os
import shutil
import sys
import time
import codecs
import itertools
import multiprocessing
import argparse

from buildtools import ENV, http, log, os_utils
from buildtools.buildsystem import WindowsCCompiler, MSBuild
from buildtools.config import YAMLConfig
from buildtools.repo.git import GitRepository
from buildtools.wrapper import CMake

script_dir = os.path.abspath(os.path.dirname(__file__))

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


def firstDirIn(dirname):
    return [os.path.join(dirname, d) for d in os.listdir(dirname) if os.path.isdir(os.path.join(dirname, d))][0]

def filesAllExist(files, basedir=''):
    return all([os.path.isfile(os.path.join(basedir,f)) for f in files])

def dlPackagesIn(pkgdefs, superrepo='build'):
    os_utils.ensureDirExists('download')
    for destination, retrievalData in pkgdefs.items():
        destination = os.path.join(superrepo, destination)
        dlType = retrievalData['type']
        if dlType == 'git':
            remote = retrievalData.get('remote', 'origin')
            branch = retrievalData.get('branch', 'master')
            commit = retrievalData.get('commit', None)
            git = GitRepository(destination, retrievalData['uri'], quiet=True, noisy_clone=True)
            with log.info('Checking for updates to %s...', destination):
                if hasntBeenCheckedFor(destination):
                    if git.CheckForUpdates(remote, branch):
                        log.info('Updates detecting, pulling...')
                        git.Pull(remote, branch, commit)
                    timestampDir(destination)
        elif dlType == 'http':
            url = retrievalData['url']
            ext = retrievalData.get('ext', url[url.rfind('.'):])
            filename = os.path.join(script_dir, 'download', retrievalData.get('filename', hashlib.md5(url).hexdigest() + ext))
            if not os.path.isfile(filename):
                with log.info('Downloading %s...', url):
                    http.DownloadFile(url, filename)
            if not os.path.isdir(destination) and not retrievalData.get('download-only', False):
                os_utils.ensureDirExists(destination)
                with os_utils.Chdir(destination):
                    os_utils.decompressFile(filename)


argp=argparse.ArgumentParser()
argp.add_argument('--reconf-qt',action='store_true',help='Cleans and reconfigures Qt.')
args=argp.parse_args()

# This just sets defaults.  Screw with build.yml instead.
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
userconfig = {
    'paths': {
        'executables': {
            '7za': os_utils.which('7za'),
            'cmake': os_utils.which('cmake'),
            'git': os_utils.which('git'),
            'graphviz': 'C:\\Program Files (x86)\\Graphviz2.38\\bin\\dot.exe',  # Default Graphviz2 install
            'hg': os_utils.which('hg'),
            'perl': 'C:\\Perl64\\bin\\perl.exe',  # ActiveState
            'python': 'C:\\Python27\\python.exe',  # python.org
            'ruby': os_utils.which('ruby'),
            'svn': os_utils.which('svn'),
        }
    }
}
config = YAMLConfig('build.yml', config, variables={'nbits': '32'})
config.Load('user-config.yml', merge=True, defaults=userconfig)
EXECUTABLES = config.get('paths.executables')

ENV.appendTo('PATH',os.path.dirname(EXECUTABLES['7za']))
ENV.set('QMAKESPEC',config.get('qt-makespec','win32-msvc2013'))

#: x64 or x86
short_arch = 'x64' if config['architecture'] == 'x86_64' else 'x86'
#: 64 or 32
nbits = "64" if config['architecture'] == 'x86_64' else "32"

superrepo = os.path.join('build', 'modorganizer_super')
if not os.path.isdir(superrepo):
    os_utils.ensureDirExists(superrepo)
    with os_utils.Chdir(superrepo):
        os_utils.cmd([EXECUTABLES['git'], 'init'], show_output=True, critical=True)

prerequisites = YAMLConfig('prerequisites.yml', variables={'nbits':nbits}).cfg
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
projectdefs = YAMLConfig('projects.yml', default=projectdefs).cfg
with log.info('Downloading projects...'):
    dlPackagesIn(projectdefs, superrepo=superrepo)

os_utils.getVSVars(config.get('paths.visual-studio'), short_arch, os.path.join('build', 'getvsvars.bat'))

# Fixes problems with the wrong Qt being on the PATH. (I usually dev with Qt4.8.7)
ENV.prependTo('PATH',os.path.join(config.get('paths.qt-base'),'bin'))
ENV.removeDuplicatedEntries('PATH',noisy=True)

libdir=os.path.join(script_dir, 'install', 'lib')
os_utils.ensureDirExists(libdir)


# This should probably be dumped into seperate modules or something, but this'll do for now.
zlib_dir = firstDirIn(os.path.join(script_dir, 'build', 'zlib'))
with log.info('Building zlib...'):
    with os_utils.Chdir(zlib_dir):
        cmake = CMake()
        cmake.setFlag('CMAKE_BUILD_TYPE', config.get('cmake.build-type'))
        cmake.setFlag('CMAKE_INSTALL_PREFIX', os.path.join(script_dir, 'install'))
        cmake.generator = 'NMake Makefiles'
        cmake.run(CMAKE=EXECUTABLES['cmake'])
        cmake.build(target='install', CMAKE=EXECUTABLES['cmake'])

winopenssl_dir=os.path.join(script_dir,'build','win{}openssl'.format(nbits))
libeay = "libeay32MD.lib"
ssleay = "ssleay32MD.lib"
libeay_path = os.path.join(winopenssl_dir, "lib", "VC", "static", libeay)
ssleay_path = os.path.join(winopenssl_dir, "lib", "VC", "static", ssleay)
with log.info('Installing Win{}OpenSSL...'.format(nbits)):
    if os.path.isfile(libeay_path) and os.path.isfile(ssleay_path):
        log.info('Skipping; Both libeay and ssleay are present.')
    else:
        log.warn('*'*30)
        log.warn('Because of stuff outside of my control, you will get a UAC prompt (probably) and a warning about command prompts.')
        log.warn('1. Hit "Yes" on the UAC prompt (assuming you get one).')
        log.warn('2. Press "OK" on the warning about command prompts.')
        log.warn('*'*30)
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

        if wait_counter<=0:
            log.error("Unpacking of OpenSSL timed out")
            sys.exit(1) #We timed out and nothing was installed

gtest_dir = os.path.join(script_dir, 'build', 'gtest')
with log.info('Building GTest...'):
    if filesAllExist(['gmock_main.lib', 'gmock.lib', 'gtest_main.lib', 'gtest.lib'], basedir='install/lib'):
        log.info('Skipping; All needed files built.')
    else:
        with os_utils.Chdir(gtest_dir):
            cmake = CMake()
            cmake.setFlag('CMAKE_BUILD_TYPE', config.get('cmake.build-type'))
            cmake.setFlag('CMAKE_INSTALL_PREFIX', os.path.join(script_dir, 'install'))
            cmake.setFlag('gtest_force_shared_crt:BOOL', 'ON')
            cmake.generator = 'NMake Makefiles'
            cmake.run(CMAKE=EXECUTABLES['cmake'])
            cmake.build(CMAKE=EXECUTABLES['cmake'], target='install')

asmjit_dir = os.path.join(script_dir,'build','asmjit')
with log.info('Building asmjit...'):
    if filesAllExist(['asmjit.lib'], basedir='install/lib'):
        log.info('Skipping; All needed files built.')
    else:
        with os_utils.Chdir(asmjit_dir):
            cmake = CMake()
            cmake.setFlag('ASMJIT_STATIC','TRUE')
            cmake.setFlag('ASMJIT_DISABLE_COMPILER','TRUE')
            cmake.setFlag('CMAKE_BUILD_TYPE', config.get('cmake.build-type'))
            cmake.setFlag('CMAKE_INSTALL_PREFIX', os.path.join(script_dir, 'install').replace('\\','/'))
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

qt5_dir = os.path.join(script_dir,'build','qt5')
qt5git_dir = os.path.join(script_dir,'build','qt5-git')
with log.info('Building Qt5...'):
    webkit_env=None
    ENV.prependTo('PATH',os.path.join(qt5_dir, 'bin'))
    if filesAllExist([os.path.join(qt5_dir,'translations','qtdeclarative_uk.qm')]):
        log.info('Skipping; Needed files exist.')
    else:
        with os_utils.Chdir(qt5git_dir):
            skip_list = ["qtactiveqt", "qtandroidextras", "qtenginio",
                        "qtserialport", "qtsvg", "qtwebengine",
                        "qtwayland", "qtdoc", "qtconnectivity", "qtwebkit-examples"]

            os_utils.cmd([EXECUTABLES['perl'], 'init-repository','--module-subset='+','.join(['all']+['-'+x for x in skip_list])], echo=True, show_output=True, critical=False)
            nomake_list = ["tests", "examples"]

            num_jobs = multiprocessing.cpu_count() * 2
            num_jobs/=2

            grep_path=os.path.join(script_dir,'build','grep')
            os_utils.ensureDirExists(grep_path)
            os_utils.copytree(os.path.join(script_dir,'build','grep-dep'),grep_path)
            os_utils.copytree(os.path.join(script_dir,'build','grep-bin'),grep_path)

            #ENV.set('OPENSSL_LIBS', '-lssleay32MD -llibeay32MD -lgdi32 -lUser32')

            webkit_env= ENV.clone()
            webkit_env.appendTo('PATH',os.path.join(grep_path,'bin'))
            webkit_env.appendTo('PATH',os.path.join(script_dir,'build','flex'))
            webkit_env.appendTo('PATH',os.path.join(qt5git_dir, 'gnuwin32','bin'))

            configure_cmd = ['cmd','/c',"configure.bat",
                              "-platform", config.get('qt-makespec','win32-msvc2013'),
                              "-debug-and-release", "-force-debug-info",
                              "-opensource", "-confirm-license",
                              "-mp", "-no-compile-examples",
                              "-no-angle", "-opengl", "desktop",
                              "-ssl", "-openssl-linked",
                              #'OPENSSL_LIBS=-lssleay32MD -llibeay32MD -lgdi32 -lUser32', # No quotes, unless the file has a space in its name.
                              "-I", os.path.join(winopenssl_dir, "include").format(nbits),
                              "-L", os.path.join(winopenssl_dir,'lib').format(nbits), # original
                              #"-L", os.path.join(winopenssl_dir,'lib','VC','static').format(nbits), # Static linking
                              "-prefix", qt5_dir] \
                             + list(itertools.chain(*[("-skip", s) for s in skip_list])) \
                             + list(itertools.chain(*[("-nomake", n) for n in nomake_list]))
            newConfMD5=hashlib.md5(repr(configure_cmd)).hexdigest()
            storedConfMD5=''
            confrecord=os.path.join(qt5git_dir,'.config_cmd')
            if os.path.isfile(confrecord):
                with open(confrecord,'r') as f:
                    storedConfMD5=f.read().strip()
            reconf = False
            if args.reconf_qt:
                log.info('--reconf-qt set, cleaning.')
                reconf=True
            if not reconf and (storedConfMD5!='' and newConfMD5!=storedConfMD5):
                log.info('Build configuration changed, cleaning.')
                reconf=True
            if reconf:
                os_utils.cmd(['git', 'submodule', 'foreach', '--recursive', "git clean -dfx"], echo=True)

            # Windows SDK and -Zc:strictStrings don't mix, so we disable them.
            qmake_spec_file = os.path.join(qt5git_dir,'qtbase','mkspecs',config.get('qt-makespec','win32-msvc2013'),'qmake.conf')
            NEW_LINES=[
                'QMAKE_CXXFLAGS_RELEASE -= -Zc:strictStrings',
                'QMAKE_CFLAGS_RELEASE -= -Zc:strictStrings',
                'QMAKE_CFLAGS -= -Zc:strictStrings',
                'QMAKE_CXXFLAGS -= -Zc:strictStrings'
            ]
            LinesFound=[]
            with open(qmake_spec_file,'r') as f:
                for line in f:
                    s_line = line.strip()
                    if s_line in NEW_LINES:
                        LinesFound.append(s_line)

            with log.info('Patching %s',qmake_spec_file):
                with open(qmake_spec_file,'a') as f:
                    for line in NEW_LINES:
                        if line in LinesFound: continue
                        f.write(line+'\n')

            os_utils.cmd(configure_cmd, echo=True, show_output=True, critical=True)
            with open(confrecord,'w') as f:
                f.write(newConfMD5)
            os_utils.cmd([os.path.join(script_dir,'build','jom','jom.exe'), '-j', str(num_jobs)], echo=True, show_output=True, critical=True)
            os_utils.cmd([config.get('tools.make'), 'install'], echo=True, show_output=True, critical=True)
        '''
        Webkit is apparently turbo-fucked.
        with os_utils.Chdir(qt5_dir):

            webkit_patch = patch.Replace("qtwebkit/Source/WebCore/platform/text/TextEncodingRegistry.cpp",
                                         "#if OS(WINDOWS) && USE(WCHAR_UNICODE)",
                                         "#if OS(WINCE) && USE(WCHAR_UNICODE)")


            os_utils.cmd([EXECUTABLES['perl'], os.path.join('Tools','Scripts','build_webkit'),'--qt','--release'], echo=True, show_output=True, critical=True)
        '''


python_dir = firstDirIn(os.path.join(script_dir,'build','python'))
EXECUTABLES['python']=''
with log.info('Building Python 2.7...'):
    with os_utils.Chdir(python_dir):
        os_utils.cmd(['cmd','/c',os.path.join('PCBuild','get_externals.bat')], echo=True, critical=True)

        # Python 2.7.12 doesn't need upgrades, so no more popups!

        msb=MSBuild()
        msb.solution='PCBuild/PCBuild.sln'
        msb.configuration='Release'
        msb.platform=short_arch
        msb.run(ENV.which('msbuild'), project='python')

        path_segments = [python_dir, "PCbuild"]
        if config['architecture'] == "x86_64":
            path_segments.append("amd64")
        basedir = os.path.join(*path_segments)
        for filename in os.listdir(basedir):
            if filename.endswith('.lib'):
                os_utils.single_copy(os.path.join(basedir,filename), libdir)
        EXECUTABLES['python']=os.path.join(*path_segments+['python.exe'])



boost_dir = firstDirIn(os.path.join(script_dir, 'build', 'boost'))
with log.info('Building Boost...'):
    if os.path.isdir(os.path.join(boost_dir,'stage','lib')):
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
            with codecs.open('user-config.jam','w',encoding='utf-8') as f:
                f.write((("using python : 2.7 : {2}\n"
                               "  : {0}\\include\n"
                               "  : {0}\\lib\n"
                               "  : <address-model>{1} ;").format(python_dir,nbits,EXECUTABLES['python'])))
            os_utils.cmd(['cmd','/c','bootstrap.bat'], echo=True, show_output=True, critical=True)
            os_utils.cmd(['b2.exe','address-model='+nbits, 'toolset=msvc-12.0', 'link=shared']+["--with-{0}".format(component) for component in boost_components], echo=True, show_output=True, critical=True)

sip_dir = firstDirIn(os.path.join(script_dir,'build','sip'))
with log.info('Building sip...'):
    with os_utils.Chdir(sip_dir):
        os_utils.cmd([EXECUTABLES['python'], "configure.py",
                  "-b", python_dir,
                  "-d", os.path.join(python_dir, "Lib", "site-packages"),
                  "-v", os.path.join(python_dir, "sip"),
                  "-e", os.path.join(python_dir, "include")
                  ], echo=True, critical=True, show_output=True)

pyqt_dir = firstDirIn(os.path.join(script_dir,'build','pyqt'))
with log.info('Building sip...'):
    with os_utils.Chdir(pyqt_dir):
        pyqt5_env=ENV.clone()
        pyqt5_env.appendTo('PATH', os.path.join(sip_dir, 'sipgen'))
        os_utils.cmd([EXECUTABLES['python'], "configure.py", "--confirm-license", '--verbose',
                      "-b", python_dir,
                      "-d", os.path.join(python_dir, "Lib", "site-packages"),
                      "-v", os.path.join(python_dir, "sip", "PyQt5"),
                      '--spec', config.get('qt-makespec'),
                      "--sip-incdir", os.path.join(python_dir, "Include")], echo=True, critical=True, show_output=True)
