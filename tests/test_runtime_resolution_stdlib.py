"""Pure resolver validation only: no registry/pip/apt/network commands executed."""
import copy,importlib.util,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
SPEC=importlib.util.spec_from_file_location('runtime_candidate_resolver',Path(__file__).parent.parent/'scripts/resolve_runtime_candidate.py')
r=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(r)
class RuntimeResolutionChecks(unittest.TestCase):
    def report(self):
        return {'version':'1','install':[{'metadata':{'name':name,'version':'1.0'},'download_info':{'url':f'https://files.pythonhosted.org/packages/{name}-1.0-py3-none-any.whl',
            'archive_info':{'hashes':{'sha256':'a'*64}}}} for name in sorted(r.REQUIRED)]}
    def test_exact_official_wheel_report(self):self.assertEqual(len(r.validate_report(self.report())),len(r.REQUIRED))
    def test_nonofficial_url_or_sdist_or_yanked_denied(self):
        for kind in ('host','sdist','yanked','duplicate'):
            value=self.report()
            if kind=='host':value['install'][0]['download_info']['url']='https://example.test/evil.whl'
            elif kind=='sdist':value['install'][0]['download_info']['url']='https://files.pythonhosted.org/evil.tar.gz'
            elif kind=='yanked':value['install'][0]['is_yanked']=True
            else:value['install'].append(copy.deepcopy(value['install'][0]))
            with self.subTest(kind=kind),self.assertRaises(ValueError):r.validate_report(value)
    def test_requirements_disallow_urls_unpinned_and_options(self):
        self.assertEqual(r.validate_requirements('# candidate\nPillow==13.0.0\n'),['Pillow==13.0.0'])
        for text in ('pillow>=13','--extra-index-url https://example.test','evil @ https://example.test/x.whl','-r private.txt'):
            with self.assertRaises(ValueError):r.validate_requirements(text)
    def test_signed_official_debian_sources_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'sources.list.d').mkdir();source=root/'sources.list.d/debian.sources'
            text='Types: deb\nURIs: http://deb.debian.org/debian\nSuites: trixie trixie-updates\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n'
            source.write_text(text);self.assertTrue(r.validate_apt_sources(root))
            for bad in (text.replace('deb.debian.org','evil.test'),text.replace('Signed-By:','Trusted:'),text.replace('trixie trixie-updates','unstable')):
                source.write_text(bad)
                with self.assertRaises(ValueError):r.validate_apt_sources(root)
    def test_source_diagnostic_strips_uri_credentials_query_and_unknown_values(self):
        fields={'Types':'deb-src','URIs':'https://USERSECRET:PASSSECRET@deb.debian.org/debian?QUERYSECRET#FRAGMENTSECRET',
                'Suites':'trixie','Components':'main','Signed-By':'/usr/share/keyrings/debian-archive-keyring.gpg',
                'Unknown':'PRIVATEVALUE'}
        text=r.apt_source_diagnostic(fields);value=json.loads(text)
        self.assertEqual(value['uri_locations'],[{'host':'deb.debian.org','path':'/debian'}])
        self.assertEqual(value['Types'],'deb-src');self.assertIn('Unknown',value['field_names'])
        for secret in ('USERSECRET','PASSSECRET','QUERYSECRET','FRAGMENTSECRET','PRIVATEVALUE'):
            self.assertNotIn(secret,text)
        self.assertLess(len(r.apt_source_diagnostic({'URIs':'x'*100000,'Types':'a'*100000})),1024)

    def test_rejected_source_includes_safe_structure_without_widening_trust(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'sources.list.d').mkdir()
            source=root/'sources.list.d/debian.sources'
            source.write_text('# comment-only block\n\nTypes: deb\nURIs: http://deb.debian.org/debian\nSuites: trixie\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n')
            with self.assertRaisesRegex(ValueError,r'source_structure=.*field_names.*\[\]'):
                r.validate_apt_sources(root)

    def test_no_ci_opt_in_performs_no_subprocess_or_network(self):
        with patch.dict(os.environ,{'GITHUB_ACTIONS':'false'}),patch.object(r,'command',side_effect=AssertionError('command forbidden')):
            with self.assertRaises(ValueError):r.main(['--base-image','python@sha256:'+'a'*64,'--python-version','3.12.14','--input','none','--output','none'])
    def test_official_base_digest_regex(self):
        self.assertTrue(r.BASE_RE.fullmatch('python@sha256:'+'a'*64))
        self.assertTrue(r.BASE_RE.fullmatch('docker.io/library/python@sha256:'+'a'*64))
        for value in ('python:latest','evil/python@sha256:'+'a'*64,'python@sha256:bad'):
            self.assertFalse(r.BASE_RE.fullmatch(value))
if __name__=='__main__':unittest.main()
