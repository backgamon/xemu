# SPDX-License-Identifier: Apache-2.0
# Copyright 2016-2018 The Meson development team
# Copyright © 2023-2024 Intel Corporation

from __future__ import annotations

import argparse, datetime, glob, json, os, platform, shutil, sys, tempfile, time
import cProfile as profile
from pathlib import Path
import typing as T

from . import build, coredata, environment, interpreter, mesonlib, mintro, mlog
from .mesonlib import MesonException

if T.TYPE_CHECKING:
    from typing_extensions import Protocol
    from .coredata import SharedCMDOptions

    class CMDOptions(SharedCMDOptions, Protocol):

        profile: bool
        fatal_warnings: bool
        reconfigure: bool
        wipe: bool
        clearcache: bool
        builddir: str
        sourcedir: str
        pager: bool

git_ignore_file = '''# This file is autogenerated by Meson. If you change or delete it, it won't be recreated.
*
'''

hg_ignore_file = '''# This file is autogenerated by Meson. If you change or delete it, it won't be recreated.
syntax: glob
**/*
'''


# Note: when adding arguments, please also add them to the completion
# scripts in $MESONSRC/data/shell-completions/
def add_arguments(parser: argparse.ArgumentParser) -> None:
    coredata.register_builtin_arguments(parser)
    parser.add_argument('--native-file',
                        default=[],
                        action='append',
                        help='File containing overrides for native compilation environment.')
    parser.add_argument('--cross-file',
                        default=[],
                        action='append',
                        help='File describing cross compilation environment.')
    parser.add_argument('-v', '--version', action='version',
                        version=coredata.version)
    parser.add_argument('--profile-self', action='store_true', dest='profile',
                        help=argparse.SUPPRESS)
    parser.add_argument('--fatal-meson-warnings', action='store_true', dest='fatal_warnings',
                        help='Make all Meson warnings fatal')
    parser.add_argument('--reconfigure', action='store_true',
                        help='Set options and reconfigure the project. Useful when new ' +
                             'options have been added to the project and the default value ' +
                             'is not working.')
    parser.add_argument('--wipe', action='store_true',
                        help='Wipe build directory and reconfigure using previous command line options. ' +
                             'Useful when build directory got corrupted, or when rebuilding with a ' +
                             'newer version of meson.')
    parser.add_argument('--clearcache', action='store_true', default=False,
                        help='Clear cached state (e.g. found dependencies). Since 1.3.0.')
    parser.add_argument('builddir', nargs='?', default=None)
    parser.add_argument('sourcedir', nargs='?', default=None)

class MesonApp:
    def __init__(self, options: CMDOptions) -> None:
        self.options = options
        (self.source_dir, self.build_dir) = self.validate_dirs()
        if options.wipe:
            # Make a copy of the cmd line file to make sure we can always
            # restore that file if anything bad happens. For example if
            # configuration fails we need to be able to wipe again.
            restore = []
            with tempfile.TemporaryDirectory() as d:
                for filename in [coredata.get_cmd_line_file(self.build_dir)] + glob.glob(os.path.join(self.build_dir, environment.Environment.private_dir, '*.ini')):
                    try:
                        restore.append((shutil.copy(filename, d), filename))
                    except FileNotFoundError:
                        # validate_dirs() already verified that build_dir has
                        # a partial build or is empty.
                        pass

                coredata.read_cmd_line_file(self.build_dir, options)

                try:
                    # Don't delete the whole tree, just all of the files and
                    # folders in the tree. Otherwise calling wipe form the builddir
                    # will cause a crash
                    for l in os.listdir(self.build_dir):
                        l = os.path.join(self.build_dir, l)
                        if os.path.isdir(l) and not os.path.islink(l):
                            mesonlib.windows_proof_rmtree(l)
                        else:
                            mesonlib.windows_proof_rm(l)
                finally:
                    self.add_vcs_ignore_files(self.build_dir)
                    for b, f in restore:
                        os.makedirs(os.path.dirname(f), exist_ok=True)
                        shutil.move(b, f)

    def has_build_file(self, dirname: str) -> bool:
        fname = os.path.join(dirname, environment.build_filename)
        return os.path.exists(fname)

    def validate_core_dirs(self, dir1: T.Optional[str], dir2: T.Optional[str]) -> T.Tuple[str, str]:
        invalid_msg_prefix = f'Neither source directory {dir1!r} nor build directory {dir2!r}'
        if dir1 is None:
            if dir2 is None:
                if not self.has_build_file('.') and self.has_build_file('..'):
                    dir2 = '..'
                else:
                    raise MesonException('Must specify at least one directory name.')
            dir1 = os.getcwd()
        if dir2 is None:
            dir2 = os.getcwd()
        ndir1 = os.path.abspath(os.path.realpath(dir1))
        ndir2 = os.path.abspath(os.path.realpath(dir2))
        if not os.path.exists(ndir1) and not os.path.exists(ndir2):
            raise MesonException(f'{invalid_msg_prefix} exist.')
        try:
            os.makedirs(ndir1, exist_ok=True)
        except FileExistsError as e:
            raise MesonException(f'{dir1} is not a directory') from e
        try:
            os.makedirs(ndir2, exist_ok=True)
        except FileExistsError as e:
            raise MesonException(f'{dir2} is not a directory') from e
        if os.path.samefile(ndir1, ndir2):
            # Fallback to textual compare if undefined entries found
            has_undefined = any((s.st_ino == 0 and s.st_dev == 0) for s in (os.stat(ndir1), os.stat(ndir2)))
            if not has_undefined or ndir1 == ndir2:
                raise MesonException('Source and build directories must not be the same. Create a pristine build directory.')
        if self.has_build_file(ndir1):
            if self.has_build_file(ndir2):
                raise MesonException(f'Both directories contain a build file {environment.build_filename}.')
            return ndir1, ndir2
        if self.has_build_file(ndir2):
            return ndir2, ndir1
        raise MesonException(f'{invalid_msg_prefix} contain a build file {environment.build_filename}.')

    def add_vcs_ignore_files(self, build_dir: str) -> None:
        with open(os.path.join(build_dir, '.gitignore'), 'w', encoding='utf-8') as ofile:
            ofile.write(git_ignore_file)
        with open(os.path.join(build_dir, '.hgignore'), 'w', encoding='utf-8') as ofile:
            ofile.write(hg_ignore_file)

    def validate_dirs(self) -> T.Tuple[str, str]:
        (src_dir, build_dir) = self.validate_core_dirs(self.options.builddir, self.options.sourcedir)
        if Path(build_dir) in Path(src_dir).parents:
            raise MesonException(f'Build directory {build_dir} cannot be a parent of source directory {src_dir}')
        if not os.listdir(build_dir):
            self.add_vcs_ignore_files(build_dir)
            return src_dir, build_dir
        priv_dir = os.path.join(build_dir, 'meson-private')
        has_valid_build = os.path.exists(os.path.join(priv_dir, 'coredata.dat'))
        has_partial_build = os.path.isdir(priv_dir)
        if has_valid_build:
            if not self.options.reconfigure and not self.options.wipe:
                print('Directory already configured.\n\n'
                      'Just run your build command (e.g. ninja) and Meson will regenerate as necessary.\n'
                      'Run "meson setup --reconfigure to force Meson to regenerate.\n\n'
                      'If build failures persist, run "meson setup --wipe" to rebuild from scratch\n'
                      'using the same options as passed when configuring the build.')
                if self.options.cmd_line_options:
                    from . import mconf
                    raise SystemExit(mconf.run_impl(self.options, build_dir))
                raise SystemExit(0)
        elif not has_partial_build and self.options.wipe:
            raise MesonException(f'Directory is not empty and does not contain a previous build:\n{build_dir}')
        return src_dir, build_dir

    # See class Backend's 'generate' for comments on capture args and returned dictionary.
    def generate(self, capture: bool = False, vslite_ctx: T.Optional[dict] = None) -> T.Optional[dict]:
        env = environment.Environment(self.source_dir, self.build_dir, self.options)
        mlog.initialize(env.get_log_dir(), self.options.fatal_warnings)
        if self.options.profile:
            mlog.set_timestamp_start(time.monotonic())
        if self.options.clearcache:
            env.coredata.clear_cache()
        with mesonlib.BuildDirLock(self.build_dir):
            return self._generate(env, capture, vslite_ctx)

    def _generate(self, env: environment.Environment, capture: bool, vslite_ctx: T.Optional[dict]) -> T.Optional[dict]:
        # Get all user defined options, including options that have been defined
        # during a previous invocation or using meson configure.
        user_defined_options = T.cast('CMDOptions', argparse.Namespace(**vars(self.options)))
        coredata.read_cmd_line_file(self.build_dir, user_defined_options)

        mlog.debug('Build started at', datetime.datetime.now().isoformat())
        mlog.debug('Main binary:', sys.executable)
        mlog.debug('Build Options:', coredata.format_cmd_line_options(user_defined_options))
        mlog.debug('Python system:', platform.system())
        mlog.log(mlog.bold('The Meson build system'))
        mlog.log('Version:', coredata.version)
        mlog.log('Source dir:', mlog.bold(self.source_dir))
        mlog.log('Build dir:', mlog.bold(self.build_dir))
        if env.is_cross_build():
            mlog.log('Build type:', mlog.bold('cross build'))
        else:
            mlog.log('Build type:', mlog.bold('native build'))
        b = build.Build(env)

        intr = interpreter.Interpreter(b, user_defined_options=user_defined_options)
        # Super hack because mlog.log and mlog.debug have different signatures,
        # and there is currently no way to annotate them correctly, unionize them, or
        # even to write `T.Callable[[*mlog.TV_Loggable], None]`
        logger_fun = T.cast('T.Callable[[mlog.TV_Loggable, mlog.TV_Loggable], None]',
                            (mlog.log if env.is_cross_build() else mlog.debug))
        logger_fun('Build machine cpu family:', mlog.bold(env.machines.build.cpu_family))
        logger_fun('Build machine cpu:', mlog.bold(env.machines.build.cpu))
        mlog.log('Host machine cpu family:', mlog.bold(env.machines.host.cpu_family))
        mlog.log('Host machine cpu:', mlog.bold(env.machines.host.cpu))
        logger_fun('Target machine cpu family:', mlog.bold(env.machines.target.cpu_family))
        logger_fun('Target machine cpu:', mlog.bold(env.machines.target.cpu))
        try:
            if self.options.profile:
                fname = os.path.join(self.build_dir, 'meson-logs', 'profile-interpreter.log')
                profile.runctx('intr.run()', globals(), locals(), filename=fname)
            else:
                intr.run()
        except Exception as e:
            mintro.write_meson_info_file(b, [e])
            raise

        cdf: T.Optional[str] = None
        captured_compile_args: T.Optional[dict] = None
        try:
            dumpfile = os.path.join(env.get_scratch_dir(), 'build.dat')
            # We would like to write coredata as late as possible since we use the existence of
            # this file to check if we generated the build file successfully. Since coredata
            # includes settings, the build files must depend on it and appear newer. However, due
            # to various kernel caches, we cannot guarantee that any time in Python is exactly in
            # sync with the time that gets applied to any files. Thus, we dump this file as late as
            # possible, but before build files, and if any error occurs, delete it.
            cdf = env.dump_coredata()

            self.finalize_postconf_hooks(b, intr)
            if self.options.profile:
                fname = f'profile-{intr.backend.name}-backend.log'
                fname = os.path.join(self.build_dir, 'meson-logs', fname)
                profile.runctx('gen_result = intr.backend.generate(capture, vslite_ctx)', globals(), locals(), filename=fname)
                captured_compile_args = locals()['gen_result']
                assert captured_compile_args is None or isinstance(captured_compile_args, dict)
            else:
                captured_compile_args = intr.backend.generate(capture, vslite_ctx)

            build.save(b, dumpfile)
            if env.first_invocation:
                # Use path resolved by coredata because they could have been
                # read from a pipe and wrote into a private file.
                self.options.cross_file = env.coredata.cross_files
                self.options.native_file = env.coredata.config_files
                coredata.write_cmd_line_file(self.build_dir, self.options)
            else:
                coredata.update_cmd_line_file(self.build_dir, self.options)

            # Generate an IDE introspection file with the same syntax as the already existing API
            if self.options.profile:
                fname = os.path.join(self.build_dir, 'meson-logs', 'profile-introspector.log')
                profile.runctx('mintro.generate_introspection_file(b, intr.backend)', globals(), locals(), filename=fname)
            else:
                mintro.generate_introspection_file(b, intr.backend)
            mintro.write_meson_info_file(b, [], True)

            # Post-conf scripts must be run after writing coredata or else introspection fails.
            intr.backend.run_postconf_scripts()

            # collect warnings about unsupported build configurations; must be done after full arg processing
            # by Interpreter() init, but this is most visible at the end
            if env.coredata.options[mesonlib.OptionKey('backend')].value == 'xcode':
                mlog.warning('xcode backend is currently unmaintained, patches welcome')
            if env.coredata.options[mesonlib.OptionKey('layout')].value == 'flat':
                mlog.warning('-Dlayout=flat is unsupported and probably broken. It was a failed experiment at '
                             'making Windows build artifacts runnable while uninstalled, due to PATH considerations, '
                             'but was untested by CI and anyways breaks reasonable use of conflicting targets in different subdirs. '
                             'Please consider using `meson devenv` instead. See https://github.com/mesonbuild/meson/pull/9243 '
                             'for details.')

            if self.options.profile:
                fname = os.path.join(self.build_dir, 'meson-logs', 'profile-startup-modules.json')
                mods = set(sys.modules.keys())
                mesonmods = {mod for mod in mods if (mod+'.').startswith('mesonbuild.')}
                stdmods = sorted(mods - mesonmods)
                data = {'stdlib': {'modules': stdmods, 'count': len(stdmods)}, 'meson': {'modules': sorted(mesonmods), 'count': len(mesonmods)}}
                with open(fname, 'w', encoding='utf-8') as f:
                    json.dump(data, f)

                mlog.log("meson setup completed")  # Display timestamp

        except Exception as e:
            mintro.write_meson_info_file(b, [e])
            if cdf is not None:
                old_cdf = cdf + '.prev'
                if os.path.exists(old_cdf):
                    os.replace(old_cdf, cdf)
                else:
                    os.unlink(cdf)
            raise

        return captured_compile_args

    def finalize_postconf_hooks(self, b: build.Build, intr: interpreter.Interpreter) -> None:
        b.devenv.append(intr.backend.get_devenv())
        for mod in intr.modules.values():
            mod.postconf_hook(b)

def run_genvslite_setup(options: CMDOptions) -> None:
    # With --genvslite, we essentially want to invoke multiple 'setup' iterations. I.e. -
    #    meson setup ... builddirprefix_debug
    #    meson setup ... builddirprefix_debugoptimized
    #    meson setup ... builddirprefix_release
    # along with also setting up a new, thin/lite visual studio solution and projects with the multiple debug/opt/release configurations that
    # invoke the appropriate 'meson compile ...' build commands upon the normal visual studio build/rebuild/clean actions, instead of using
    # the native VS/msbuild system.
    builddir_prefix = options.builddir
    genvsliteval = options.cmd_line_options.pop(mesonlib.OptionKey('genvslite'))
    # The command line may specify a '--backend' option, which doesn't make sense in conjunction with
    # '--genvslite', where we always want to use a ninja back end -
    k_backend = mesonlib.OptionKey('backend')
    if k_backend in options.cmd_line_options.keys():
        if options.cmd_line_options[k_backend] != 'ninja':
            raise MesonException('Explicitly specifying a backend option with \'genvslite\' is not necessary '
                                 '(the ninja backend is always used) but specifying a non-ninja backend '
                                 'conflicts with a \'genvslite\' setup')
    else:
        options.cmd_line_options[k_backend] = 'ninja'
    buildtypes_list = coredata.get_genvs_default_buildtype_list()
    vslite_ctx = {}

    for buildtypestr in buildtypes_list:
        options.builddir = f'{builddir_prefix}_{buildtypestr}' # E.g. builddir_release
        options.cmd_line_options[mesonlib.OptionKey('buildtype')] = buildtypestr
        app = MesonApp(options)
        vslite_ctx[buildtypestr] = app.generate(capture=True)
    #Now for generating the 'lite' solution and project files, which will use these builds we've just set up, above.
    options.builddir = f'{builddir_prefix}_vs'
    options.cmd_line_options[mesonlib.OptionKey('genvslite')] = genvsliteval
    app = MesonApp(options)
    app.generate(capture=False, vslite_ctx=vslite_ctx)

def run(options: T.Union[CMDOptions, T.List[str]]) -> int:
    if isinstance(options, list):
        parser = argparse.ArgumentParser()
        add_arguments(parser)
        options = T.cast('CMDOptions', parser.parse_args(options))
    coredata.parse_cmd_line_options(options)

    # Msetup doesn't actually use this option, but we pass msetup options to
    # mconf, and it does. We won't actally hit the path that uses it, but don't
    # lie
    options.pager = False

    if mesonlib.OptionKey('genvslite') in options.cmd_line_options.keys():
        run_genvslite_setup(options)
    else:
        app = MesonApp(options)
        app.generate()

    return 0