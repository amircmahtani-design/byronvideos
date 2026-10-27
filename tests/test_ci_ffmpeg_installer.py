"""Offline installer control-flow tests; no sudo, package or network operations."""
import importlib.util
from pathlib import Path
import subprocess
import pytest

SPEC = importlib.util.spec_from_file_location('ci_ffmpeg', Path(__file__).resolve().parents[1]/'scripts/install_ci_ffmpeg.py')
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def test_only_signed_official_https_noble_sources():
    for mirror in installer.MIRRORS:
        source = installer.sources(mirror)
        assert source.count('signed-by=/usr/share/keyrings/ubuntu-archive-keyring.gpg') == 3
        assert source.count(mirror) == 3
        assert 'noble-updates' in source and 'noble-security' in source
        assert 'azure' not in source and 'http:' not in source and 'trusted=yes' not in source
    with pytest.raises(ValueError): installer.sources('https://example.test/untrusted')


def test_source_isolation_authentication_and_bounded_retries(tmp_path):
    cmd = installer.apt_command(tmp_path/'sources', tmp_path/'lists', tmp_path/'archives', 'update')
    assert 'Dir::Etc::sourceparts=-' in cmd
    assert 'APT::Get::AllowUnauthenticated=false' in cmd
    assert 'Acquire::AllowInsecureRepositories=false' in cmd
    assert 'Acquire::AllowDowngradeToInsecureRepositories=false' in cmd
    assert 'APT::Update::Error-Mode=any' in cmd
    assert 'Acquire::Retries=1' in cmd
    assert 'Acquire::https::Timeout=20' in cmd
    assert '--allow-unauthenticated' not in cmd


def test_download_success_precedes_offline_install_and_verifies_tools(tmp_path):
    calls=[]
    installer.install(tmp_path, lambda cmd, timeout: calls.append((cmd,timeout)))
    assert len(calls)==5
    assert calls[0][0][-1]=='update' and calls[0][1]==120
    assert '--download-only' in calls[1][0] and calls[1][1]==240
    assert '--no-download' in calls[2][0] and calls[2][1]==120
    assert calls[3:]==[(['ffmpeg','-version'],10),(['ffprobe','-version'],10)]
    assert not (tmp_path/'attempt-2').exists()


@pytest.mark.parametrize('failed_stage', ['update','download'])
def test_bounded_official_fallback_before_any_install(tmp_path,failed_stage):
    calls=[]
    def run(cmd,timeout):
        calls.append((cmd,timeout))
        first=any('attempt-1/sources.list' in arg for arg in cmd)
        failure=(cmd[-1]=='update' if failed_stage=='update' else '--download-only' in cmd)
        if first and failure: raise subprocess.CalledProcessError(124,cmd)
    installer.install(tmp_path,run)
    assert (tmp_path/'attempt-2/sources.list').read_text()==installer.sources(installer.MIRRORS[1])
    installs=[cmd for cmd,_ in calls if '--no-download' in cmd]
    assert len(installs)==1 and any('attempt-2/sources.list' in a for a in installs[0])
    assert len(calls)<=7


def test_all_download_failures_stop_without_install_or_success(tmp_path):
    calls=[]
    def run(cmd,timeout):
        calls.append(cmd)
        if '--download-only' in cmd: raise subprocess.CalledProcessError(100,cmd)
    with pytest.raises(RuntimeError,match='mandatory media tests'):
        installer.install(tmp_path,run)
    assert len(calls)==4
    assert not any('--no-download' in cmd or cmd[0]=='ffmpeg' for cmd in calls)


def test_package_install_failure_is_not_retried(tmp_path):
    calls=[]
    def run(cmd,timeout):
        calls.append(cmd)
        if '--no-download' in cmd: raise subprocess.CalledProcessError(100,cmd)
    with pytest.raises(subprocess.CalledProcessError): installer.install(tmp_path,run)
    assert len(calls)==3 and not (tmp_path/'attempt-2').exists()


def test_gnu_timeout_bounds_process_group_and_no_shell(monkeypatch):
    calls=[]
    monkeypatch.setattr(installer.subprocess,'run',lambda *args,**kwargs:calls.append((args,kwargs)))
    installer.run_bounded(['apt-get','update'],120)
    args,kwargs=calls[0]
    assert args[0]==['timeout','--signal=TERM','--kill-after=10s','120s','apt-get','update']
    assert kwargs['check'] is True and kwargs.get('shell',False) is False
    assert kwargs['env']['DEBIAN_FRONTEND']=='noninteractive'


def test_main_rejects_non_ci_before_install(monkeypatch):
    monkeypatch.delenv('GITHUB_ACTIONS',raising=False)
    with pytest.raises(SystemExit,match='restricted'): installer.main()


def test_workflow_keeps_mandatory_media_and_existing_timeout():
    workflow=(Path(__file__).resolve().parents[1]/'.github/workflows/checks.yml').read_text()
    assert 'BYRON_REQUIRE_MEDIA_TESTS: "1"' in workflow
    assert 'timeout-minutes: 20' in workflow
    assert 'sudo --preserve-env=GITHUB_ACTIONS python3 scripts/install_ci_ffmpeg.py' in workflow
    assert 'continue-on-error' not in workflow
    assert 'python -m pytest -q' in workflow
