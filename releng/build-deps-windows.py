#!/usr/bin/env python3

import argparse
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import os
from pathlib import Path, PurePath
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Callable, Dict, List, Tuple
import urllib.request

from deps import read_dependency_parameters, Bundle, DependencyParameters, PackageSpec
import v8
import winenv


class PackageRole(Enum):
    TOOL = 1,
    LIBRARY = 2,


Package = Tuple[str, PackageRole, List[str]]


class SourceState(Enum):
    PRISTINE = 1,
    MODIFIED = 2,


EnvDir = str
ShellEnv = Dict[str, str]


@dataclass
class MesonEnv:
    path: str
    shell_env: ShellEnv



class MissingDependencyError(Exception):
    pass


ARCHITECTURES = {
    PackageRole.TOOL: ['x86'],
    PackageRole.LIBRARY: ['x86_64', 'x86'],
}
CONFIGURATIONS = {
    PackageRole.TOOL: ['Release'],
    PackageRole.LIBRARY: ['Debug', 'Release'],
}
RUNTIMES = {
    PackageRole.TOOL: ['static'],
    PackageRole.LIBRARY: ['static', 'dynamic'],
}
COMPRESSION_LEVEL = 9

RELENG_DIR = Path(__file__).parent.resolve()
ROOT_DIR = RELENG_DIR.parent
DEPS_DIR = ROOT_DIR / "deps"
BOOTSTRAP_TOOLCHAIN_DIR = ROOT_DIR / "build" / "fts-toolchain-windows"

MESON = RELENG_DIR / "meson" / "meson.py"
NINJA = BOOTSTRAP_TOOLCHAIN_DIR / "bin" / "ninja.exe"

ALL_PACKAGES: List[Package] = [
    ("zlib", PackageRole.LIBRARY, []),
    ("brotli", PackageRole.LIBRARY, []),
    ("minizip", PackageRole.LIBRARY, []),
    ("libffi", PackageRole.LIBRARY, []),
    ("glib", PackageRole.LIBRARY, []),
    ("pkg-config", PackageRole.TOOL, []),
    ("vala", PackageRole.TOOL, []),
    ("sqlite", PackageRole.LIBRARY, []),
    ("openssl", PackageRole.LIBRARY, []),
    ("glib-networking", PackageRole.LIBRARY, []),
    ("libnice", PackageRole.LIBRARY, []),
    ("usrsctp", PackageRole.LIBRARY, []),
    ("libgee", PackageRole.LIBRARY, []),
    ("json-glib", PackageRole.LIBRARY, []),
    ("libpsl", PackageRole.LIBRARY, []),
    ("libxml2", PackageRole.LIBRARY, []),
    ("libsoup", PackageRole.LIBRARY, []),
    ("capstone", PackageRole.LIBRARY, []),
    ("quickjs", PackageRole.LIBRARY, []),
    ("tinycc", PackageRole.LIBRARY, []),
    ("v8", PackageRole.LIBRARY, []),
]

ALL_BUNDLES = {
    Bundle.TOOLCHAIN: [
        "zlib",
        "libffi",
        "glib",
        "pkg-config",
        "vala",
    ],
    Bundle.SDK: [
        "zlib",
        "brotli",
        "minizip",
        "libffi",
        "glib",
        "sqlite",
        "openssl",
        "glib-networking",
        "libnice",
        "usrsctp",
        "libgee",
        "json-glib",
        "libpsl",
        "libxml2",
        "libsoup",
        "capstone",
        "quickjs",
        "tinycc",
        "v8",
    ],
}

HOST_DEFINES = {
    "capstone_archs": "x86",
}


cached_meson_params = {}
cached_target_glib = None
cached_bootstrap_valac = None

build_arch = 'x86_64' if platform.machine().endswith("64") else 'x86'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", help="only build one specific bundle",
                        default=None, choices=[name.lower() for name in Bundle.__members__])
    parser.add_argument("--v8", help="whether to include V8 in the SDK",
                        default='enabled', choices=['enabled', 'disabled'])

    arguments = parser.parse_args()

    if arguments.bundle is None:
        bundle_ids = [Bundle.TOOLCHAIN, Bundle.SDK]
    else:
        bundle_ids = [Bundle[arguments.bundle.upper()]]

    selected = set([pkg_name for bundle_id in bundle_ids for pkg_name in ALL_BUNDLES[bundle_id]])
    packages = [pkg for pkg in ALL_PACKAGES if pkg[0] in selected]
    if arguments.v8 == 'disabled':
        packages = [pkg for pkg in packages if pkg[0] != "v8"]

    params = read_dependency_parameters(HOST_DEFINES)

    started_at = time.time()
    sync_ended_at = None
    build_ended_at = None
    packaging_ended_at = None
    try:
        synchronize(packages, params)
        sync_ended_at = time.time()

        build(packages, params)
        build_ended_at = time.time()

        package(bundle_ids, params)
        packaging_ended_at = time.time()
    except subprocess.CalledProcessError as e:
        print(e, file=sys.stderr)
        if e.stdout is not None:
            print("\n=== stdout ===\n" + e.stdout, file=sys.stderr)
        if e.stderr is not None:
            print("\n=== stderr ===\n" + e.stderr, file=sys.stderr)
        sys.exit(1)
    finally:
        ended_at = time.time()

        if sync_ended_at is not None:
            print("")
            print("*** TIME SPENT")
            print("")
            print("      Total: {}".format(format_duration(ended_at - started_at)))

        if sync_ended_at is not None:
            print("       Sync: {}".format(format_duration(sync_ended_at - started_at)))

        if build_ended_at is not None:
            print("      Build: {}".format(format_duration(build_ended_at - sync_ended_at)))

        if packaging_ended_at is not None:
            print("  Packaging: {}".format(format_duration(packaging_ended_at - build_ended_at)))


def synchronize(packages: List[Package], params: DependencyParameters):
    toolchain_state = ensure_bootstrap_toolchain(params.bootstrap_version)
    if toolchain_state == SourceState.MODIFIED:
        wipe_build_state()

    check_environment()

    for name, _, _ in packages:
        pkg_state = grab_and_prepare(name, params.get_package_spec(name), params)
        if pkg_state == SourceState.MODIFIED:
            wipe_build_state()

def check_environment():
    try:
        winenv.get_msvs_installation_dir()
        winenv.get_winxp_sdk()
        winenv.get_win10_sdk()
    except MissingDependencyError as e:
        print("ERROR: {}".format(e), file=sys.stderr)
        sys.exit(1)

    for tool in ["7z", "git", "nasm", "patch", "py"]:
        if shutil.which(tool) is None:
            print("ERROR: {} not found on PATH".format(tool), file=sys.stderr)
            sys.exit(1)

def grab_and_prepare(name: str, spec: PackageSpec, params: DependencyParameters) -> SourceState:
    if spec.recipe == 'meson' or name == 'openssl':
        return grab_and_prepare_regular_package(name, spec)

    assert name == 'v8'
    return grab_and_prepare_v8_package(spec, params.get_package_spec("depot_tools"))

def grab_and_prepare_regular_package(name: str, spec: PackageSpec) -> SourceState:
    if spec.hash == "":
        return grab_and_prepare_regular_git_package(name, spec)
    else:
        return grab_and_prepare_regular_tarball_package(name, spec)

def grab_and_prepare_regular_git_package(name: str, spec: PackageSpec) -> SourceState:
    assert spec.patches == []

    source_dir = DEPS_DIR / name
    if source_dir.exists():
        if query_git_head(source_dir) == spec.version:
            source_state = SourceState.PRISTINE
        else:
            print()
            print("{name}: synchronizing".format(name=name))
            perform("git", "fetch", "-q", cwd=source_dir)
            perform("git", "checkout", "-q", spec.version, cwd=source_dir)
            source_state = SourceState.MODIFIED
    else:
        print()
        print("{name}: cloning into deps\\{name}".format(name=name))
        DEPS_DIR.mkdir(parents=True, exist_ok=True)
        perform("git", "clone", "-q", "--recurse-submodules", spec.url, name, cwd=DEPS_DIR)
        perform("git", "checkout", "-q", spec.version, cwd=source_dir)
        source_state = SourceState.PRISTINE

    return source_state

def grab_and_prepare_regular_tarball_package(name: str, spec: PackageSpec) -> SourceState:
    version_file = DEPS_DIR / (name + "-version.txt")
    try:
        current_version = version_file.read_text(encoding='utf-8').strip()
        if current_version == spec.version:
            return SourceState.PRISTINE
    except:
        pass

    source_dir = DEPS_DIR / name
    if source_dir.exists():
        shutil.rmtree(source_dir)
        source_state = SourceState.MODIFIED
    else:
        source_state = SourceState.PRISTINE

    archive_path = None
    sha256 = hashlib.sha256()
    try:
        print("> Downloading", spec.url)

        with urllib.request.urlopen(spec.url) as response, tempfile.NamedTemporaryFile(delete=False) as archive:
            archive_path = Path(archive.name)
            while True:
                chunk = response.read(65536)
                if len(chunk) == 0:
                    break
                archive.write(chunk)
                sha256.update(chunk)

        digest = sha256.hexdigest()
        if digest != spec.hash:
            raise ValueError("{} tarball is corrupted; its hash is {}".format(name, digest))

        print("> Extracting", spec.url)

        staging_dir = source_dir / "__staging__"
        staging_dir.mkdir(parents=True)

        uncompress = subprocess.Popen(["7z", "x", "-tgzip", "-so", archive_path],
                                      stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL)
        extract = subprocess.Popen(["7z", "x", "-ttar", "-si"],
                                   stdin=uncompress.stdout,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT,
                                   encoding='utf-8',
                                   cwd=staging_dir)
        uncompress.stdout.close()
        output = extract.communicate()[0]
        if extract.returncode != 0:
            raise ValueError("{} extraction failed: {}".format(name, output))

        for entry in staging_dir.glob(name + "*/*"):
            shutil.move(str(entry), source_dir)

        shutil.rmtree(staging_dir)
    finally:
        if archive_path is not None:
            try:
                archive_path.unlink()
            except:
                pass

    for patch_name in spec.patches:
        print("> Applying", patch_name)
        patch_path = Path(RELENG_DIR / "patches" / patch_name)
        patch_data = patch_path.read_text(encoding='utf-8')
        p = subprocess.Popen(["patch", "-p1"],
                             stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT,
                             encoding='utf-8',
                             cwd=source_dir)
        output = p.communicate(patch_data)[0]
        if p.returncode != 0:
            raise ValueError("unable to apply {}: {}".format(patch_name, output))

    version_file.write_text(spec.version + "\n", encoding='utf-8')

    return source_state

def grab_and_prepare_v8_package(v8_spec: PackageSpec, depot_spec: PackageSpec) -> SourceState:
    assert v8_spec.hash == ""
    assert v8_spec.patches == []
    assert v8_spec.deps == []
    assert v8_spec.deps_for_build == []

    assert depot_spec.deps == []
    assert depot_spec.deps_for_build == []
    grab_and_prepare_regular_package("depot_tools", depot_spec)
    depot_dir = DEPS_DIR / "depot_tools"
    gclient = depot_dir / "gclient.bat"
    env = make_v8_env(depot_dir)
    metrics_cfg = depot_dir / "metrics.cfg"
    if not metrics_cfg.exists():
        metrics_cfg.write_text("""{"is-googler": false, "countdown": 10, "opt-in": null, "version": 1}""", encoding='utf-8')

    checkout_dir = DEPS_DIR / "v8-checkout"
    checkout_dir.mkdir(parents=True, exist_ok=True)

    source_dir = checkout_dir / "v8"
    source_exists = source_dir.exists()
    if source_exists and query_git_head(source_dir) == v8_spec.version:
        return SourceState.PRISTINE

    print()
    if source_exists:
        print("v8: synchronizing")
        source_state = SourceState.MODIFIED
    else:
        print("v8: cloning into deps\\v8-checkout")
        source_state = SourceState.PRISTINE

    spec = """solutions = [ {{ "url": "{url}@{version}", "managed": False, "name": "v8", "deps_file": "DEPS", "custom_deps": {{}}, }}, ]""" \
        .format(url=v8_spec.url, version=v8_spec.version)
    perform(gclient, "config", "--spec", spec, cwd=checkout_dir, env=env)

    perform(gclient, "sync", cwd=checkout_dir, env=env)

    return source_state


def wipe_build_state():
    print("*** Wiping build state")
    locations = [
        ("existing packages", get_prefix_root()),
        ("build directories", get_tmp_root()),
    ]
    for description, path in locations:
        if path.exists():
            print("Wiping", description)
            shutil.rmtree(path)


def build(packages: List[Package], params: DependencyParameters):
    for name, role, extra_options in packages:
        build_package(name, role, params.get_package_spec(name), extra_options)

def build_package(name: str, role: PackageRole, spec: PackageSpec, extra_options: List[str]):
    archs = ARCHITECTURES[role]
    configs = CONFIGURATIONS[role]
    runtimes = RUNTIMES[role]

    for arch in archs:
        for config in configs:
            for runtime in runtimes:
                manifest_path = get_manifest_path(name, arch, config, runtime)
                if manifest_path.exists():
                    continue

                print()
                print("*** Building {} with arch={} runtime={} config={} spec={}".format(name, arch, config, runtime, spec))

                if spec.recipe == 'meson':
                    build_using_meson(name, arch, config, runtime, spec, extra_options)
                else:
                    if name == "openssl":
                        build_openssl(arch, config, runtime, spec, extra_options)
                    else:
                        assert name == "v8"
                        assert spec.recipe == 'custom'
                        build_v8(arch, config, runtime, spec, extra_options)

                assert manifest_path.exists()

def build_using_meson(name: str, arch: str, config: str, runtime: str, spec: PackageSpec, extra_options: List[str]):
    env_dir, shell_env = get_meson_params(arch, config, runtime)

    source_dir = DEPS_DIR / name
    build_dir = env_dir / name
    prefix = get_prefix_path(arch, config, runtime)
    optimization = 's' if config == 'Release' else '0'
    ndebug = 'true' if config == 'Release' else 'false'

    if build_dir.exists():
        shutil.rmtree(build_dir)

    perform(
        "py", "-3", MESON,
        build_dir,
        "--prefix", prefix,
        "--default-library", "static",
        "--backend", "ninja",
        "-Doptimization=" + optimization,
        "-Db_ndebug=" + ndebug,
        "-Db_vscrt=" + vscrt_from_configuration_and_runtime(config, runtime),
        *spec.options,
        *extra_options,
        cwd=source_dir,
        env=shell_env
    )

    perform(NINJA, "install", cwd=build_dir, env=shell_env)

    manifest_lines = []
    install_locations = json.loads(subprocess.check_output([
            "py", "-3", MESON,
            "introspect",
            "--installed"
        ],
        cwd=build_dir,
        encoding='utf-8',
        env=shell_env))
    for installed_path in install_locations.values():
        manifest_lines.append(Path(installed_path).relative_to(prefix).as_posix())
    manifest_lines.sort()
    manifest_path = get_manifest_path(name, arch, config, runtime)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text("\n".join(manifest_lines), encoding='utf-8')

def get_meson_params(arch: str, config: str, runtime: str) -> Tuple[EnvDir, ShellEnv]:
    global cached_meson_params

    identifier = ":".join([arch, config, runtime])

    params = cached_meson_params.get(identifier, None)
    if params is None:
        params = generate_meson_params(arch, config, runtime)
        cached_meson_params[identifier] = params

    return params

def generate_meson_params(arch: str, config: str, runtime: str) -> Tuple[EnvDir, ShellEnv]:
    env = generate_meson_env(arch, config, runtime)
    return (env.path, env.shell_env)

def generate_meson_env(arch: str, config: str, runtime: str) -> MesonEnv:
    prefix = get_prefix_path(arch, config, runtime)
    env_dir = get_tmp_path(arch, config, runtime)
    env_dir.mkdir(parents=True, exist_ok=True)

    vc_dir = Path(winenv.get_msvs_installation_dir()) / "VC"
    vc_install_dir = str(vc_dir) + "\\"

    msvc_platform = msvc_platform_from_arch(arch)
    msvc_dir = Path(winenv.get_msvc_tool_dir())
    msvc_bin_dir = msvc_dir / "bin" / ("Host" + msvc_platform_from_arch(build_arch)) / msvc_platform

    msvc_dll_dirs = []
    if arch != build_arch:
        build_msvc_platform = msvc_platform_from_arch(build_arch)
        msvc_dll_dirs.append(msvc_dir / "bin" / ("Host" + build_msvc_platform) / build_msvc_platform)

    (winxp_sdk_dir, winxp_sdk_version) = winenv.get_winxp_sdk()
    winxp_sdk_dir = Path(winxp_sdk_dir)
    if arch == 'x86':
        winxp_bin_dir = winxp_sdk_dir / "Bin"
        winxp_lib_dir = winxp_sdk_dir / "Lib"
    else:
        winxp_bin_dir = winxp_sdk_dir / "Bin" / msvc_platform
        winxp_lib_dir = winxp_sdk_dir / "Lib" / msvc_platform

    clflags = "/D" + " /D".join([
      "_USING_V110_SDK71_",
      "_UNICODE",
      "UNICODE",
    ])

    platform_cflags = []
    if arch == 'x86':
        platform_cflags += ["/arch:SSE2"]

    cflags = " ".join(platform_cflags)

    cxxflags = " ".join(platform_cflags + [
        # Relax C++11 compliance for XP compatibility.
        "/Zc:threadSafeInit-",
    ])

    (win10_sdk_dir, win10_sdk_version) = winenv.get_win10_sdk()
    win10_sdk_dir = Path(win10_sdk_dir)

    m4_path = BOOTSTRAP_TOOLCHAIN_DIR / "bin" / "m4.exe"
    bison_pkgdatadir = BOOTSTRAP_TOOLCHAIN_DIR / "share" / "bison"

    vala_flags = "--target-glib=2.56"

    exe_path = ";".join([str(path) for path in [
        prefix / "bin",
        env_dir,
        BOOTSTRAP_TOOLCHAIN_DIR / "bin",
        winxp_bin_dir,
        msvc_bin_dir,
    ] + msvc_dll_dirs])

    include_path = ";".join([str(path) for path in [
        msvc_dir / "include",
        msvc_dir / "atlmfc" / "include",
        vc_dir / "Auxiliary" / "VS" / "include",
        win10_sdk_dir / "Include" / win10_sdk_version / "ucrt",
        winxp_sdk_dir / "Include",
    ]])

    library_path = ";".join([str(path) for path in [
        msvc_dir / "lib" / msvc_platform,
        msvc_dir / "atlmfc" / "lib" / msvc_platform,
        vc_dir / "Auxiliary" / "VS" / "lib" / msvc_platform,
        win10_sdk_dir / "Lib" / win10_sdk_version / "ucrt" / msvc_platform,
        winxp_lib_dir,
    ]])

    env_path = env_dir / "env.bat"
    env_path.write_text("""@ECHO OFF
set PATH={exe_path};%PATH%
set INCLUDE={include_path}
set LIB={library_path}
set CL={clflags}
set CFLAGS={cflags}
set CXXFLAGS={cxxflags}
set VCINSTALLDIR={vc_install_dir}
set Platform={platform}
set VALA={valac}
set VALAFLAGS={vala_flags}
set DEPOT_TOOLS_WIN_TOOLCHAIN=0
""".format(
            exe_path=exe_path,
            include_path=include_path,
            library_path=library_path,
            clflags=clflags,
            cflags=cflags,
            cxxflags=cxxflags,
            vc_install_dir=vc_install_dir,
            platform=msvc_platform,
            valac=detect_bootstrap_valac(),
            vala_flags=vala_flags,
        ),
        encoding='utf-8')

    rc_path = winxp_bin_dir / "rc.exe"
    rc_wrapper_path = env_dir / "rc.bat"
    rc_wrapper_path.write_text("""@ECHO OFF
SETLOCAL EnableExtensions
SET _res=0
"{rc_path}" {flags} %* || SET _res=1
ENDLOCAL & SET _res=%_res%
EXIT /B %_res%""".format(rc_path=rc_path, flags=clflags), encoding='utf-8')

    (env_dir / "meson.bat").write_text("""@ECHO OFF
SETLOCAL EnableExtensions
SET _res=0
py -3 "{meson_path}" %* || SET _res=1
ENDLOCAL & SET _res=%_res%
EXIT /B %_res%""".format(meson_path=MESON), encoding='utf-8')

    pkgconfig_path = BOOTSTRAP_TOOLCHAIN_DIR / "bin" / "pkg-config.exe"
    pkgconfig_lib_dir = prefix / "lib" / "pkgconfig"
    pkgconfig_wrapper_path = env_dir / "pkg-config.bat"
    pkgconfig_wrapper_path.write_text("""@ECHO OFF
SETLOCAL EnableExtensions
SET _res=0
SET PKG_CONFIG_PATH={pkgconfig_lib_dir}
"{pkgconfig_path}" --static %* || SET _res=1
ENDLOCAL & SET _res=%_res%
EXIT /B %_res%""".format(
            pkgconfig_path=pkgconfig_path,
            pkgconfig_lib_dir=pkgconfig_lib_dir,
        ),
        encoding='utf-8')

    flex_path = BOOTSTRAP_TOOLCHAIN_DIR / "bin" / "flex.exe"
    flex_wrapper_path = env_dir / "flex.py"
    (env_dir / "flex.bat").write_text("""@ECHO OFF
SETLOCAL EnableExtensions
SET _res=0
py -3 "{wrapper_path}" %* || SET _res=1
ENDLOCAL & SET _res=%_res%
EXIT /B %_res%""".format(wrapper_path=flex_wrapper_path), encoding='utf-8')
    flex_wrapper_path.write_text("""import subprocess
import sys

args = [arg.replace("/", "\\\\") for arg in sys.argv[1:]]
sys.exit(subprocess.call([r"{flex_path}"] + args))
""".format(flex_path=flex_path), encoding='utf-8')

    bison_path = BOOTSTRAP_TOOLCHAIN_DIR / "bin" / "bison.exe"
    bison_wrapper_path = env_dir / "bison.py"
    (env_dir / "bison.bat").write_text("""@ECHO OFF
SETLOCAL EnableExtensions
SET _res=0
py -3 "{wrapper_path}" %* || SET _res=1
ENDLOCAL & SET _res=%_res%
EXIT /B %_res%""".format(wrapper_path=bison_wrapper_path), encoding='utf-8')
    bison_wrapper_path.write_text("""\
import os
import subprocess
import sys

os.environ["BISON_PKGDATADIR"] = r"{bison_pkgdatadir}"
os.environ["M4"] = r"{m4_path}"

args = [arg.replace("/", "\\\\") for arg in sys.argv[1:]]
sys.exit(subprocess.call([r"{bison_path}"] + args))
""".format(
            bison_path=bison_path,
            bison_pkgdatadir=bison_pkgdatadir,
            m4_path=m4_path
        ),
        encoding='utf-8')

    shell_env = {}
    shell_env.update(os.environ)
    shell_env["PATH"] = exe_path + ";" + shell_env["PATH"]
    shell_env["INCLUDE"] = include_path
    shell_env["LIB"] = library_path
    shell_env["CL"] = clflags
    shell_env["CFLAGS"] = cflags
    shell_env["CXXFLAGS"] = cxxflags
    shell_env["VCINSTALLDIR"] = vc_install_dir
    shell_env["Platform"] = msvc_platform
    shell_env["VALAC"] = detect_bootstrap_valac()
    shell_env["VALAFLAGS"] = vala_flags

    return MesonEnv(env_dir, shell_env)

def detect_bootstrap_valac() -> str:
    global cached_bootstrap_valac
    if cached_bootstrap_valac is None:
        compilers = (BOOTSTRAP_TOOLCHAIN_DIR / "bin").glob("valac*.exe")
        cached_bootstrap_valac = next(compilers).name
    return cached_bootstrap_valac


def build_openssl(arch: str, config: str, runtime: str, spec: PackageSpec, extra_options: List[str]):
    env_dir, shell_env = get_meson_params(arch, config, runtime)
    shell_env = dict(shell_env)
    del shell_env['CFLAGS']
    del shell_env['CXXFLAGS']

    source_dir = DEPS_DIR / "openssl"
    build_dir = env_dir / "openssl"
    prefix = get_prefix_path(arch, config, runtime)

    if build_dir.exists():
        shutil.rmtree(build_dir)
    shutil.copytree(source_dir, build_dir)

    runtime_flag = "/MD" if runtime == 'dynamic' else "/MT"
    if config == "Debug":
        runtime_flag += "d"

    config_path = build_dir / "Configurations" / "10-main.conf"
    config_data = config_path.read_text(encoding='utf-8')
    flags_to_replace = [
        "/MTd",
        "/MT /Zl",
        "/MT",
        "/MDd",
        "/MD",
    ]
    placeholder = "XXXX"
    for flag in flags_to_replace:
        config_data = config_data.replace(flag, placeholder)
    config_data = config_data.replace(placeholder, runtime_flag)
    config_data = config_data.replace("/Zi /Fdossl_static.pdb", "/Z7")
    config_path.write_text(config_data, encoding='utf-8')

    options = [
        "--prefix=" + str(prefix),
        "--release" if config == "Release" else "--debug",
    ]
    options += [option for option in spec.options if not option.startswith("--openssldir")]
    options += extra_options

    os_compiler = "VC-WIN64A" if arch == "x86_64" else "VC-WIN32"

    perform("perl", "Configure", *options, os_compiler, cwd=build_dir, env=shell_env)

    nmake = shutil.which("nmake", path=shell_env["PATH"])
    perform(nmake, "depend", cwd=build_dir, env=shell_env)
    perform(nmake, "build_libs", cwd=build_dir, env=shell_env)

    manifest_lines = []

    install_output = perform(nmake, "install_dev", cwd=build_dir, env=shell_env, capture_output=True, encoding='utf-8').stdout
    copy_messages = [line for line in install_output.split("\n") if line.startswith("Copying: ")]
    prefix_length = len(str(prefix)) + 1
    for m in copy_messages:
        entry = m[m.index(" to ") + 4:][prefix_length:]
        manifest_lines.append(entry)

    pkgconfig_dir = prefix / "lib" / "pkgconfig"
    pkgconfig_dir.mkdir(parents=True, exist_ok=True)
    (pkgconfig_dir / "openssl.pc").write_text("""\
prefix={prefix}
exec_prefix=${{prefix}}
libdir=${{exec_prefix}}/lib
includedir=${{prefix}}/include

Name: OpenSSL
Description: Secure Sockets Layer and cryptography libraries and tools
Version: {version}
Requires: libssl libcrypto""" \
        .format(
            prefix=prefix.as_posix(),
            version=spec.version,
        ),
        encoding='utf-8')
    (pkgconfig_dir / "libssl.pc").write_text("""\
prefix={prefix}
exec_prefix=${{prefix}}
libdir=${{exec_prefix}}/lib
includedir=${{prefix}}/include

Name: OpenSSL-libssl
Description: Secure Sockets Layer and cryptography libraries
Version: {version}
Requires.private: libcrypto
Libs: -L${{libdir}} -lssl
Cflags: -I${{includedir}}""" \
        .format(
            prefix=prefix.as_posix(),
            version=spec.version,
        ),
        encoding='utf-8')
    (pkgconfig_dir / "libcrypto.pc").write_text("""\
prefix={prefix}
exec_prefix=${{prefix}}
libdir=${{exec_prefix}}/lib
includedir=${{prefix}}/include
enginesdir=${{libdir}}/engines-1.1

Name: OpenSSL-libcrypto
Description: OpenSSL cryptography library
Version: {version}
Libs: -L${{libdir}} -lcrypto
Cflags: -I${{includedir}}""" \
        .format(
            prefix=prefix.as_posix(),
            version=spec.version,
        ),
        encoding='utf-8')
    manifest_lines += [
        "lib/pkgconfig/openssl.pc",
        "lib/pkgconfig/libssl.pc",
        "lib/pkgconfig/libcrypto.pc",
    ]

    manifest_lines.sort()
    manifest_path = get_manifest_path("openssl", arch, config, runtime)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text("\n".join(manifest_lines), encoding='utf-8')


def build_v8(arch: str, config: str, runtime: str, spec: PackageSpec, extra_options: List[str]):
    depot_dir = DEPS_DIR / "depot_tools"
    gn = depot_dir / "gn.bat"
    env = make_v8_env(depot_dir)

    source_dir = DEPS_DIR / "v8-checkout" / "v8"

    build_dir = get_tmp_path(arch, config, runtime) / "v8"
    if not (build_dir / "build.ninja").exists():
        if build_dir.exists():
            shutil.rmtree(build_dir)

        if config == 'Release':
            configuration_args = [
                "is_official_build=true",
                "is_debug=false",
                "v8_enable_v8_checks=false",
            ]
        else:
            configuration_args = [
                "is_debug=true",
                "v8_enable_v8_checks=true",
            ]

        (win10_sdk_dir, win10_sdk_version) = winenv.get_win10_sdk()

        args = " ".join([
            "target_cpu=\"{}\"".format(msvc_platform_from_arch(arch)),
        ] + configuration_args + [
            "use_crt=\"{}\"".format(runtime),
            "is_clang=false",
            "visual_studio_path=\"{}\"".format(winenv.get_msvs_installation_dir()),
            "visual_studio_version=\"{}\"".format(winenv.get_msvs_version()),
            "wdk_path=\"{}\"".format(win10_sdk_dir),
            "windows_sdk_path=\"{}\"".format(win10_sdk_dir),
            "symbol_level=0",
            "strip_absolute_paths_from_debug_symbols=true",
        ] + spec.options + extra_options)

        perform(gn, "gen", PurePath("..", "..", "..") / build_dir.relative_to(ROOT_DIR),
                "--args=" + args, cwd=source_dir, env=env)

    monolith_path = build_dir / "obj" / "v8_monolith.lib"
    perform(NINJA, "v8_monolith", cwd=build_dir, env=env)

    version, api_version = v8.detect_version(source_dir)

    prefix = get_prefix_path(arch, config, runtime)

    include_dir = prefix / "include" / ("v8-" + api_version) / "v8"
    header_dirs = [
        source_dir / "include",
        build_dir / "gen" / "include",
    ]
    for header_dir in header_dirs:
        header_files = [PurePath(path.relative_to(header_dir)) for path in header_dir.glob("**/*.h")]
        copy_files(header_dir, header_files, include_dir)

    v8.patch_config_header(include_dir / "v8config.h", source_dir, build_dir, gn, env)

    lib_dir = prefix / "lib"

    pkgconfig_dir = lib_dir / "pkgconfig"
    pkgconfig_dir.mkdir(parents=True, exist_ok=True)

    libv8_path = lib_dir / "libv8-{}.a".format(api_version)
    shutil.copyfile(monolith_path, libv8_path)

    (pkgconfig_dir / "v8-{}.pc".format(api_version)).write_text("""\
prefix={prefix}
libdir=${{prefix}}/lib
includedir=${{prefix}}/include/v8-{api_version}

Name: V8
Description: V8 JavaScript Engine
Version: {version}
Libs: -L${{libdir}} -lv8-{api_version}
Libs.private: {libs_private}
Cflags: -I${{includedir}} -I${{includedir}}/v8""" \
        .format(
            prefix=prefix.as_posix(),
            version=version,
            api_version=api_version,
            libs_private="-lshlwapi -lwinmm"
        ),
        encoding='utf-8')

    manifest_lines = [line.format(api_version=api_version) for line in [
        "lib/libv8-{api_version}.a",
        "lib/pkgconfig/v8-{api_version}.pc",
    ]]
    for header in include_dir.glob("**/*"):
        manifest_lines.append(str(header.relative_to(prefix).as_posix()))
    manifest_lines.sort()
    manifest_path = get_manifest_path("v8", arch, config, runtime)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text("\n".join(manifest_lines), encoding='utf-8')

def make_v8_env(depot_dir: Path) -> ShellEnv:
    env = {}
    env.update(os.environ)
    env["PATH"] = str(depot_dir) + ";" + env["PATH"]
    env["DEPOT_TOOLS_WIN_TOOLCHAIN"] = "0"
    return env


def package(bundle_ids: List[Bundle], params: DependencyParameters):
    with tempfile.TemporaryDirectory(prefix="frida-deps") as tempdir:
        tempdir = Path(tempdir)

        toolchain_filename = "toolchain-windows-x86.exe"
        toolchain_path = ROOT_DIR / "build" / toolchain_filename

        sdk_filename = "sdk-windows-any.exe"
        sdk_path = ROOT_DIR / "build" / sdk_filename

        print("About to assemble:")
        if Bundle.TOOLCHAIN in bundle_ids:
            print("\t* " + toolchain_filename)
        if Bundle.SDK in bundle_ids:
            print("\t* " + sdk_filename)

        print()
        print("Determining what to include...")

        prefixes_dir = get_prefix_root()

        toolchain_files = []
        toolchain_mixin_files = []
        if Bundle.TOOLCHAIN in bundle_ids:
            for root, dirs, files in os.walk(get_prefix_path('x86', 'Release', 'static')):
                relpath = PurePath(root).relative_to(prefixes_dir)
                all_files = [relpath / f for f in files]
                toolchain_files += [f for f in all_files if file_is_vala_toolchain_related(f) or \
                        f.name in ["pkg-config.exe", "glib-genmarshal", "glib-mkenums"] or \
                        f.parent.name == "manifest"]
            toolchain_files.sort()

            for root, dirs, files in os.walk(BOOTSTRAP_TOOLCHAIN_DIR):
                relpath = PurePath(root).relative_to(BOOTSTRAP_TOOLCHAIN_DIR)
                all_files = [relpath / f for f in files]
                toolchain_mixin_files += [f for f in all_files if not (file_is_vala_toolchain_related(f) or \
                        f.parent.name == "manifest")]
            toolchain_mixin_files.sort()

        sdk_built_files = []
        if Bundle.SDK in bundle_ids:
            for prefix in prefixes_dir.glob("*-static"):
                for root, dirs, files in os.walk(prefix):
                    relpath = PurePath(root).relative_to(prefixes_dir)
                    all_files = [relpath / f for f in files]
                    sdk_built_files += [f for f in all_files if file_is_sdk_related(f)]
                sdk_built_files += [f.relative_to(prefixes_dir) for f in \
                        (prefix.parent / (prefix.name[:-7] + "-dynamic") / "lib").glob("**/*.a")]
            sdk_built_files.sort()

        print("Copying files...")
        if Bundle.TOOLCHAIN in bundle_ids:
            toolchain_tempdir = tempdir / "toolchain-windows"
            copy_files(BOOTSTRAP_TOOLCHAIN_DIR, toolchain_mixin_files, toolchain_tempdir)
            copy_files(prefixes_dir, toolchain_files, toolchain_tempdir, transform_toolchain_dest)
            fix_manifests(toolchain_tempdir)
            (toolchain_tempdir / "VERSION.txt").write_text(params.deps_version + "\n", encoding='utf-8')

        if Bundle.SDK in bundle_ids:
            sdk_tempdir = tempdir / "sdk-windows"
            copy_files(prefixes_dir, sdk_built_files, sdk_tempdir, transform_sdk_dest)
            fix_manifests(sdk_tempdir)
            (sdk_tempdir / "VERSION.txt").write_text(params.deps_version + "\n", encoding='utf-8')

        print("Compressing...")
        compression_switches = ["a", "-mx{}".format(COMPRESSION_LEVEL), "-sfx7zCon.sfx"]

        if Bundle.TOOLCHAIN in bundle_ids:
            toolchain_path.unlink(missing_ok=True)
            perform("7z", *compression_switches, "-r", toolchain_path, "toolchain-windows", cwd=tempdir)

        if Bundle.SDK in bundle_ids:
            sdk_path.unlink(missing_ok=True)
            perform("7z", *compression_switches, "-r", sdk_path, "sdk-windows", cwd=tempdir)

        print("All done.")

def fix_manifests(root: Path):
    for manifest_path in root.glob("**/manifest/*.pkg"):
        manifest_lines = []

        prefix = manifest_path.parent.parent
        for entry in manifest_path.read_text(encoding='utf-8').strip().split("\n"):
            if prefix.joinpath(entry).exists():
                manifest_lines.append(entry)

            if entry.startswith("lib/") and entry.endswith(".a"):
                dynamic_entry = "lib-dynamic/" + entry[4:]
                if prefix.joinpath(dynamic_entry).exists():
                    manifest_lines.append(dynamic_entry)

        if len(manifest_lines) > 0:
            manifest_lines.sort()
            manifest_path.write_text("\n".join(manifest_lines), encoding='utf-8')
        else:
            manifest_path.unlink()

def file_is_sdk_related(candidate: PurePath) -> bool:
    parts = candidate.parts
    subdir = parts[1]

    if subdir == "bin":
        return False

    if subdir == "lib":
        filename = candidate.name
        if "vala" in parts[1:] or "vala" in filename or "vapigen" in filename:
            return False

    suffix = candidate.suffix

    if suffix == ".h" and candidate.stem.startswith("vala"):
        return False

    if suffix in [".vapi", ".deps"]:
        return not is_vala_toolchain_vapi_directory(candidate.parent)

    return "share" not in parts

def file_is_vala_toolchain_related(candidate: PurePath) -> bool:
    if candidate.suffix in [".vapi", ".deps"]:
        return is_vala_toolchain_vapi_directory(candidate.parent)
    return candidate.name.startswith("valac-") and candidate.suffix == ".exe"

def is_vala_toolchain_vapi_directory(directory: PurePath) -> bool:
    parts = directory.parts[-3:]
    if len(parts) != 3:
        print("D parts:", parts)
        return False
    return parts[0] == "share" and \
        parts[1].startswith("vala-") and \
        parts[2] == "vapi"

def transform_identity(srcfile: PurePath) -> PurePath:
    return srcfile

def transform_sdk_dest(srcfile: PurePath) -> PurePath:
    parts = srcfile.parent.parts
    rootdir = parts[0]
    subpath = PurePath(*parts[1:])

    arch, config, runtime = rootdir.split("-")
    rootdir = "-".join([
        msvs_platform_from_arch(arch),
        config.title()
    ])

    if runtime == 'dynamic' and subpath.parts[0] == "lib":
        subpath = PurePath("lib-dynamic").joinpath(*subpath.parts[1:])

    return PurePath(rootdir) / subpath / srcfile.name

def transform_toolchain_dest(srcfile: PurePath) -> PurePath:
    return PurePath(*srcfile.parts[1:])


def ensure_bootstrap_toolchain(bootstrap_version: str) -> SourceState:
    if BOOTSTRAP_TOOLCHAIN_DIR.exists():
        try:
            version = (BOOTSTRAP_TOOLCHAIN_DIR / "VERSION.txt").read_text(encoding='utf-8').strip()
            if version == bootstrap_version:
                return SourceState.PRISTINE
        except:
            pass
        shutil.rmtree(BOOTSTRAP_TOOLCHAIN_DIR)

        source_state = SourceState.MODIFIED
    else:
        source_state = SourceState.PRISTINE

    print("Downloading bootstrap toolchain...")
    with urllib.request.urlopen("https://build.frida.re/deps/{version}/toolchain-windows-x86.exe" \
            .format(version=bootstrap_version)) as response, \
            tempfile.NamedTemporaryFile(suffix=".exe", delete=False) as archive:
        shutil.copyfileobj(response, archive)
        toolchain_archive_path = archive.name

    print("Extracting bootstrap toolchain...")
    try:
        tempdir = Path(tempfile.mkdtemp(prefix="frida-bootstrap-toolchain"))
        try:
            try:
                subprocess.check_output([
                    toolchain_archive_path,
                    "-o" + str(tempdir),
                    "-y"
                ])
            except subprocess.CalledProcessError as e:
                print("Oops:", e.output.decode('utf-8'))
                raise e
            shutil.move(tempdir / "toolchain-windows", BOOTSTRAP_TOOLCHAIN_DIR)
        finally:
            shutil.rmtree(tempdir)
    finally:
        os.unlink(toolchain_archive_path)

    return source_state

def get_prefix_root() -> Path:
    return ROOT_DIR / "build" / "fts-windows"

def get_prefix_path(arch: str, config: str, runtime: str) -> Path:
    return get_prefix_root() / "{}-{}-{}".format(arch, config.lower(), runtime)

def get_manifest_path(name: str, arch: str, config: str, runtime: str) -> Path:
    return get_prefix_path(arch, config, runtime) / "manifest" / (name + ".pkg")

def get_tmp_root() -> Path:
    return ROOT_DIR / "build" / "fts-tmp-windows"

def get_tmp_path(arch: str, config: str, runtime: str) -> Path:
    return get_tmp_root() / "{}-{}-{}".format(arch, config.lower(), runtime)

def msvs_platform_from_arch(arch: str) -> str:
    return 'x64' if arch == 'x86_64' else 'Win32'

def msvc_platform_from_arch(arch: str) -> str:
    return 'x64' if arch == 'x86_64' else 'x86'

def vscrt_from_configuration_and_runtime(config: str, runtime: str) -> str:
    result = "md" if runtime == 'dynamic' else "mt"
    if config == 'Debug':
        result += "d"
    return result


def perform(*args, **kwargs):
    print(">", " ".join([str(arg) for arg in args]))
    return subprocess.run(args, check=True, **kwargs)

def query_git_head(repo_path: str) -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_path, encoding='utf-8').strip()

def copy_files(fromdir: Path, files: List[PurePath], todir: Path, transformdest: Callable[[PurePath], PurePath] = transform_identity):
    for filename in files:
        src = fromdir / filename
        dst = todir / transformdest(filename)
        dstdir = dst.parent
        dstdir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)

def format_duration(duration_in_seconds: float) -> str:
    hours, remainder = divmod(duration_in_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return "{:02d}:{:02d}:{:02d}".format(int(hours), int(minutes), int(seconds))


if __name__ == '__main__':
    main()
