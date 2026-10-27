#!/usr/bin/env python3
"""Install CI FFmpeg from bounded, signed Ubuntu HTTPS archives.

Run only on the disposable ubuntu-24.04 GitHub Actions runner, as root.
This does not edit the runner's system sources, use PPAs, skip media tests, or
fetch an unsigned/static binary. Downloads complete before dpkg is invoked.
Ubuntu source/package authenticity remains enforced by APT and its installed
Ubuntu archive keyring. Official archive and security hosts publish the same
signed Noble suites and provide a bounded fallback for a stalled mirror.
"""
from __future__ import annotations
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile

MIRRORS = ('https://archive.ubuntu.com/ubuntu', 'https://security.ubuntu.com/ubuntu')
KEYRING = '/usr/share/keyrings/ubuntu-archive-keyring.gpg'
UPDATE_SECONDS = 120
DOWNLOAD_SECONDS = 240
INSTALL_SECONDS = 120


def sources(mirror):
    if mirror not in MIRRORS:
        raise ValueError('Only the two fixed official Ubuntu HTTPS archives are permitted.')
    return ''.join(f'deb [arch=amd64 signed-by={KEYRING}] {mirror} {suite} main universe\n'
                   for suite in ('noble', 'noble-updates', 'noble-security'))


def apt_command(source, lists, archives, *arguments):
    # Isolate sources/lists/cache: no runner PPA, Azure mirror or insecure bypass.
    options = {
        'Dir::Etc::sourcelist': str(source), 'Dir::Etc::sourceparts': '-',
        'Dir::State::lists': str(lists), 'Dir::Cache::archives': str(archives),
        'APT::Architecture': 'amd64', 'Acquire::Languages': 'none',
        'Acquire::Retries': '1', 'Acquire::http::Timeout': '20',
        'Acquire::https::Timeout': '20',
        'Acquire::AllowInsecureRepositories': 'false',
        'Acquire::AllowDowngradeToInsecureRepositories': 'false',
        'APT::Get::AllowUnauthenticated': 'false',
        'APT::Update::Error-Mode': 'any',
        'Acquire::IndexTargets::deb::DEP-11::DefaultEnabled': 'false',
        'DPkg::Lock::Timeout': '15',
    }
    result = ['apt-get']
    for key, value in options.items():
        result.extend(['-o', f'{key}={value}'])
    return result + list(arguments)


def run_bounded(command, seconds):
    # GNU timeout manages the process group, including APT downloader children;
    # do not leave them holding locks when the next bounded mirror is attempted.
    subprocess.run(['timeout', '--signal=TERM', '--kill-after=10s', f'{seconds}s', *command],
                   check=True, env={**os.environ, 'DEBIAN_FRONTEND': 'noninteractive'})


def install(workspace, run=run_bounded):
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    workspace.chmod(0o755)  # Allow APT's unprivileged downloader to read sources.
    archives = workspace/'archives'
    (archives/'partial').mkdir(parents=True, exist_ok=True)
    last_failure = None
    for number, mirror in enumerate(MIRRORS, 1):
        root = workspace/f'attempt-{number}'
        (root/'lists'/'partial').mkdir(parents=True)
        source = root/'sources.list'
        source.write_text(sources(mirror), encoding='ascii')
        base = lambda *args: apt_command(source, root/'lists', archives, *args)
        print(f'Attempt {number}/{len(MIRRORS)}: signed Ubuntu archive {mirror}', flush=True)
        try:
            run(base('update'), UPDATE_SECONDS)
            run(base('--download-only', '--no-install-recommends', '-y', 'install', 'ffmpeg'), DOWNLOAD_SECONDS)
        except subprocess.CalledProcessError as exc:
            last_failure = exc
            print(f'Archive attempt failed (exit {exc.returncode}); no package installation was started.', flush=True)
            continue
        # No network during installation; a missing/corrupt package fails closed.
        # An installation error is NOT retried against another mirror.
        run(base('--no-download', '--no-install-recommends', '-y', 'install', 'ffmpeg'), INSTALL_SECONDS)
        run(['ffmpeg', '-version'], 10)
        run(['ffprobe', '-version'], 10)
        return
    raise RuntimeError('Both official Ubuntu archive attempts failed; mandatory media tests cannot run.') from last_failure


def main():
    release = platform.freedesktop_os_release()
    if (os.environ.get('GITHUB_ACTIONS') != 'true' or os.geteuid() != 0
            or release.get('ID') != 'ubuntu' or release.get('VERSION_ID') != '24.04'
            or platform.machine() != 'x86_64'):
        raise SystemExit('This installer is restricted to the root ubuntu-24.04 amd64 GitHub Actions job.')
    if not Path(KEYRING).is_file() or not all(shutil.which(x) for x in ('apt-get', 'timeout')):
        raise SystemExit('Ubuntu archive trust/runtime prerequisites are missing; refusing installation.')
    with tempfile.TemporaryDirectory(prefix='byron-ci-apt-') as temp:
        install(temp)


if __name__ == '__main__':
    main()
