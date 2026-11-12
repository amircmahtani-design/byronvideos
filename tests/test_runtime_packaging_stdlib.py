"""Local fake wheel metadata/archive locks; never installs or resolves anything."""
import copy,hashlib,json,tempfile,unittest,zipfile
from pathlib import Path
from byron_video import runtime_packaging as p
class RuntimePackagingChecks(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        (self.root/'wheels').mkdir();(self.root/'debs').mkdir()
        packages={n:'1.0' for n in p.REQUIRED};packages.update(pillow='13.0.0',numpy='2.5.4')
        self.manifest=dict(schema_version=1,status='REVIEWED_DEPENDENCY_MATERIALS',base_image='python@sha256:'+'a'*64,
            python='3.12.14',platform='linux/amd64',ffmpeg_version='7.1.5-0+deb13u1',packages=packages,
            system_packages={'ffmpeg':'7:7.1.5-0+deb13u1','ca-certificates':'1'},wheels=[],debs=[])
        for name,version in packages.items():
            path=self.root/'wheels'/f'{name.replace("-","_")}-{version}-py3-none-any.whl'
            with zipfile.ZipFile(path,'w') as z:z.writestr(f'{name}-{version}.dist-info/METADATA',f'Name: {name}\nVersion: {version}\n')
            self.manifest['wheels'].append(self.row(path))
        path=self.root/'debs'/'fixture.deb';path.write_bytes(b'not a real package; hash checking only');self.manifest['debs']=[self.row(path)]
    def tearDown(self):self.temp.cleanup()
    def row(self,path):return dict(file=path.name,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),bytes=path.stat().st_size)
    def test_exact_complete_materials_produce_hash_required_lock(self):
        lock=p.verify_materials(self.root,self.manifest)
        self.assertEqual(len(lock.splitlines()),len(p.REQUIRED));self.assertIn('pillow==13.0.0 --hash=sha256:',lock)
    def test_sdk_lock_bytes_match_resolution_artifact(self):
        path=self.root/'materials.json';path.write_text(json.dumps(self.manifest))
        target=self.root/'sdk-lock.json'
        p.main(['materials','--manifest',str(path),'--root',str(self.root),'--write-sdk-lock',str(target)])
        self.assertEqual(target.read_text(),json.dumps({'python':'3.12','packages':self.manifest['packages']},sort_keys=True)+'\n')
    def test_unresolved_template_never_builds(self):
        value=json.loads((Path(__file__).parent.parent/'hosted/runtime-materials.template.json').read_text())
        with self.assertRaises(ValueError):p.validate_manifest(value)
    def test_changed_archive_or_extra_file_fails(self):
        (self.root/'debs'/'fixture.deb').write_bytes(b'changed')
        with self.assertRaises(ValueError):p.verify_materials(self.root,self.manifest)
    def test_missing_transitive_wheel_fails(self):
        self.manifest['packages']['extra-required']='1.0'
        with self.assertRaises(ValueError):p.verify_materials(self.root,self.manifest)
    def test_path_traversal_symlink_and_mutable_base_fail(self):
        for change in ('path','symlink','base'):
            value=copy.deepcopy(self.manifest)
            if change=='path':value['wheels'][0]['file']='../evil.whl'
            elif change=='base':value['base_image']='python:latest'
            else:
                path=self.root/'debs'/'fixture.deb';path.unlink();path.symlink_to('/dev/null')
            with self.subTest(change=change),self.assertRaises(ValueError):p.verify_materials(self.root,value)
    def test_offline_deb_install_uses_verified_absolute_archive_cache(self):
        root=Path(__file__).resolve().parents[1]
        for name in ('Dockerfile.dependencies','Dockerfile.hosted-candidate'):
            text=(root/name).read_text()
            line=next(line for line in text.splitlines() if line.startswith('RUN ') and 'apt-get ' in line)
            self.assertIn('RUN --network=none apt-get',line)
            self.assertIn('-o Dir::Cache::archives=/opt/byron-runtime/debs',line)
            self.assertIn('--no-download --no-remove --no-install-recommends',line)
            self.assertTrue(line.endswith('install /opt/byron-runtime/debs/*.deb'))
            self.assertLess(text.index('runtime_packaging.py materials'),text.index(line))
            self.assertNotIn('--allow-unauthenticated',text)
            self.assertNotIn('apt-get update',text)

    def test_wrong_media_runtime_cannot_silently_repin(self):
        self.manifest['packages']['pillow']='12.3.0'
        with self.assertRaises(ValueError):p.validate_manifest(self.manifest)
if __name__=='__main__':unittest.main()
