# SPDX-License-Identifier: MIT
"""Paths and helpers shared by the platform build scripts.

Every generated file lives under platform/build/: downloads, prepared source
trees, out-of-tree build directories and the final images. Deleting that one
directory returns the checkout to its pristine state.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PLATFORM = ROOT / 'platform'
BUILD = PLATFORM / 'build'
DOWNLOADS = BUILD / 'downloads'
SRC = BUILD / 'src'
IMAGES = BUILD / 'images'
LOGS = BUILD / 'logs'

BUILDROOT_OUT = BUILD / 'buildroot'
HOST_BIN = BUILDROOT_OUT / 'host/bin'
CROSS_COMPILE = os.environ.get('W1700K_CROSS_COMPILE',
                               str(HOST_BIN / 'aarch64-buildroot-linux-musl-'))
LINUX_OUT = BUILD / 'linux'
KERNEL_IMAGE = LINUX_OUT / 'arch/arm64/boot/Image'
DTB = LINUX_OUT / 'sdr/an7581-w1700k-sdr.dtb'
ROOTFS_IMAGE = BUILDROOT_OUT / 'images/rootfs.squashfs'
MKIMAGE = BUILD / 'mkimage/tools/mkimage'

# Fixed build identity so that rebuilding unchanged inputs gives the same bytes.
REPRODUCIBLE_ENV = {'SOURCE_DATE_EPOCH': '0', 'TZ': 'UTC', 'LC_ALL': 'C',
                    'KBUILD_BUILD_USER': 'w1700k', 'KBUILD_BUILD_HOST': 'build',
                    'KBUILD_BUILD_VERSION': '1',
                    'KBUILD_BUILD_TIMESTAMP': '1970-01-01 00:00:00 UTC'}


class BuildError(Exception):
    """A build step failed; the message says which and why."""


def require(condition, message):
    if not condition:
        raise BuildError(message)


def sources():
    """The pinned upstream inputs, from platform/sources.json."""
    return json.loads((PLATFORM / 'sources.json').read_text())


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def build_env(extra_path=()):
    """Environment for cross builds: Buildroot's host tools first on PATH.

    The host tools include bison and flex, which the kernel and U-Boot need and
    which many hosts do not install.
    """
    env = os.environ.copy()
    env.update(REPRODUCIBLE_ENV)
    path = [str(p) for p in extra_path] + [str(HOST_BIN), env.get('PATH', '')]
    env['PATH'] = os.pathsep.join(path)
    return env


def jobs():
    return str(int(os.environ.get('JOBS', os.cpu_count() or 4)))


def run(argv, *, cwd=None, env=None, log=None, input=None):
    """Run a command; with `log`, capture its output there and show the tail on failure."""
    argv = [str(a) for a in argv]
    if log is None:
        result = subprocess.run(argv, cwd=cwd, env=env, input=input)
    else:
        log = Path(log)
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open('ab') as stream:
            stream.write(('$ ' + ' '.join(argv) + '\n').encode())
            stream.flush()
            result = subprocess.run(argv, cwd=cwd, env=env, input=input,
                                    stdout=stream, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        tail = ''
        if log is not None:
            lines = Path(log).read_text(errors='replace').splitlines()[-25:]
            tail = '\n' + '\n'.join('    ' + line for line in lines)
        raise BuildError(f'command failed (exit {result.returncode}): '
                         f'{" ".join(argv[:4])}...{tail}'
                         + (f'\n  full log: {log}' if log else ''))
    return result


def output(argv, **options):
    return subprocess.run([str(a) for a in argv], check=True, capture_output=True,
                          text=True, **options).stdout


def kconfig(path):
    """A Kconfig file as {CONFIG_NAME: value}; `# X is not set` becomes 'n'."""
    values = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line.startswith('# ') and line.endswith(' is not set'):
            values[line[2:-len(' is not set')]] = 'n'
        elif '=' in line and not line.startswith('#'):
            key, value = line.split('=', 1)
            values[key] = value
    return values


def write_if_changed(path, data):
    """Write bytes or text only when they differ, so make-style timestamps stay put."""
    path = Path(path)
    data = data.encode() if isinstance(data, str) else data
    if path.is_file() and path.read_bytes() == data:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_bytes(data)
    temporary.replace(path)
    return True


def copy_if_changed(source, dest):
    return write_if_changed(dest, Path(source).read_bytes())


def apply_patches(src, patches, log):
    """Apply each patch in order with zero fuzz to a freshly extracted tree.

    Trees are never re-patched in place: `prepared()` discards a tree whose
    patch set changed, and the caller extracts a fresh one.
    """
    for patch in patches:
        run(['patch', '-p1', '--batch', '--forward', '--fuzz=0',
             '--no-backup-if-mismatch', '-i', patch], cwd=src, log=log)


def patch_stamp(patches, extra=()):
    """A digest over the patch set (and any other preparation inputs)."""
    digest = hashlib.sha256()
    for path in [*patches, *extra]:
        digest.update(Path(path).name.encode() + b'\0')
        digest.update(hashlib.sha256(Path(path).read_bytes()).digest())
    return digest.hexdigest()


def prepared(src, stamp):
    """True when `src` was prepared from exactly this stamp; removes it otherwise."""
    marker = Path(str(src) + '.prepared')
    if src.is_dir() and marker.is_file() and marker.read_text().strip() == stamp:
        return True
    if src.exists():
        shutil.rmtree(src)
    marker.unlink(missing_ok=True)
    return False


def mark_prepared(src, stamp):
    Path(str(src) + '.prepared').write_text(stamp + '\n')


def main_guard(function):
    """Run a script entry point, turning BuildError into a one-line exit."""
    try:
        sys.exit(function())
    except BuildError as error:
        sys.exit(f'{Path(sys.argv[0]).name}: {error}')
