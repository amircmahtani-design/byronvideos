"""Pure public packaging allowlist tests; no publication or private-file reads."""
import importlib.util,tempfile,unittest
from pathlib import Path
SPEC=importlib.util.spec_from_file_location('public_candidate',Path(__file__).parent.parent/'scripts/prepare_public_candidate.py')
p=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(p)
class PublicCandidateChecks(unittest.TestCase):
    def test_private_recovery_and_handoff_evidence_never_public(self):
        for name in ('validated/byronvideos-recovery.zip','RECOVERY-MANIFEST.json','RECOVERY-VALIDATION.md','VERIFICATION.md',
                     'hosted/OFFLINE-HANDOFF.md','hosted/DEPLOYMENT-CHECKLIST.md','.env','.private/config.json','uploads/original.jpg',
                     'style-guide/approved/real-original.png','style-guide/fonts/other.ttf'):
            with self.subTest(name=name):self.assertFalse(p.allowed(name))
    def test_runtime_tests_templates_and_known_synthetic_baselines_allowed(self):
        for name in ('byron_video/worker_bootstrap.py','tests/test_engine.py','scripts/resolve_runtime_candidate.py',
                     'hosted/deployment.private.template.json','.github/workflows/resolve-hosted-runtime.yml',
                     'style-guide/fonts/DejaVuSans.ttf','style-guide/references/synthetic-baselines/portrait.png'):
            with self.subTest(name=name):self.assertTrue(p.allowed(name))
    def test_existing_public_web_assets_and_tests_preserved(self):
        for name in ('web/sitemap.xml','web/ui.test.cjs','web/README.md'):
            self.assertTrue(p.allowed(name))
        self.assertFalse(p.allowed('web/private-handoff.md'))
    def test_inventory_omits_private_names_and_content(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'README.md').write_text('public fixture');(root/'private-user-filename.txt').write_text('private fixture')
            value=p.inventory(root)
            self.assertEqual([r['path'] for r in value['files']],['README.md']);self.assertEqual(value['omitted_file_count'],1)
            self.assertNotIn('private-user-filename',str(value));self.assertNotIn('private fixture',str(value))
if __name__=='__main__':unittest.main()
