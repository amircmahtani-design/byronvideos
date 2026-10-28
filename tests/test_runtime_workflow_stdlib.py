"""Static PR workflow safety contracts; no runner, network or publication."""
from pathlib import Path
import unittest

WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/resolve-hosted-runtime.yml'

def named_step(text, name):
    marker = '      - name: ' + name + '\n'
    if text.count(marker) != 1:
        raise AssertionError('Expected one exact named workflow step: ' + name)
    return text.split(marker, 1)[1].split('      - name:', 1)[0].split('\n#', 1)[0]


def artifact_paths(step):
    if step.count('          path: |\n') != 1:
        raise AssertionError('Expected one explicit artifact path block')
    return {line.strip() for line in step.split('          path: |\n', 1)[1].splitlines() if line.strip()}


class RuntimeWorkflowChecks(unittest.TestCase):
    def test_unprivileged_pr_trigger_and_explicit_patch_default(self):
        text = WORKFLOW.read_text()
        self.assertIn('  pull_request:\n', text)
        self.assertIn('types: [opened, synchronize, reopened, ready_for_review]', text)
        self.assertNotIn('pull_request_target', text)
        self.assertIn("PYTHON_PATCH: ${{ inputs.python_patch || '3.12.14' }}", text)
        self.assertIn('permissions:\n  contents: read\njobs:', text)
        self.assertIn('persist-credentials: false', text)
        for forbidden in ('secrets.', 'id-token:', 'contents: write', 'docker login', 'docker push', 'gcloud ', 'google-github-actions/auth'):
            self.assertNotIn(forbidden, text)

    def test_resolver_public_output_ownership_restored_even_on_failure(self):
        text = WORKFLOW.read_text()
        step = text.split('      - name: Resolve public PyPI wheels', 1)[1].split('      - name:', 1)[0]
        self.assertIn('test "$RUNTIME_OUTPUT" = "$RUNNER_TEMP/byron-runtime-output"', step)
        self.assertIn('test ! -L "$RUNTIME_OUTPUT"', step)
        self.assertIn('runner_owner="$(id -u):$(id -g)"', step)
        restore = "trap 'sudo chown -hR -- \"$runner_owner\" \"$RUNTIME_OUTPUT\"' EXIT"
        self.assertIn(restore, step)
        self.assertLess(step.index(restore), step.index('sudo chown -- 0:0'))
        self.assertLess(step.index('sudo chown -- 0:0'), step.index('docker run'))
        self.assertIn('--cap-drop=ALL --security-opt=no-new-privileges', step)
        self.assertNotIn('--privileged', step)
        self.assertNotIn('chmod 777', step)
        resolver = (WORKFLOW.parents[2] / 'scripts/resolve_runtime_candidate.py').read_text()
        self.assertEqual(resolver.count("'APT::Sandbox::User=root'"), 2)

    def test_candidate_branch_checks_only_run_on_pr_not_duplicate_push(self):
        text=(WORKFLOW.parent/"checks.yml").read_text()
        self.assertIn("  push:\n    branches: [main]\n  pull_request:",text)
        self.assertIn("python -m pytest -q",text)

    def test_legacy_test_dependencies_include_signed_runtime_crypto(self):
        requirements = (WORKFLOW.parents[2] / 'requirements-test.txt').read_text().splitlines()
        self.assertIn('cryptography==50.0.2', requirements)

    def test_offline_tests_and_explicit_public_artifact_allowlist(self):
        text = WORKFLOW.read_text()
        self.assertIn('docker build --network=none --pull=false', text)
        self.assertIn('docker run --rm --network=none --read-only', text)
        self.assertIn('python3 -m pytest -q -p no:cacheprovider', text)
        self.assertIn('BYRON_REQUIRE_MEDIA_TESTS=1', text)
        evidence = named_step(text, 'Upload only public dependency identities and review evidence')
        names = {'base-resolution.json','materials.json','requirements.lock','sdk-lock.json',
                                'resolution-status.json','sbom.cdx.json','pypi-artifacts.json','apt-policy.txt',
                                'candidate-pins.txt','candidate-image-id.json'}
        self.assertEqual(artifact_paths(evidence), {'${{ runner.temp }}/byron-runtime-output/' + name for name in names})
        self.assertNotIn('playwright install', text)
        self.assertNotIn('npm install', text)

    def test_image_export_has_separate_exact_allowlist_and_success_scope(self):
        text = WORKFLOW.read_text()
        export_name = 'Export the tested public image for isolated operator transfer'
        upload_name = 'Upload public tested image with one-day retention'
        export, upload = named_step(text, export_name), named_step(text, upload_name)
        gate = "if: github.event_name == 'pull_request' && github.head_ref == 'candidate/private-studio-v9-20261027' && github.event.pull_request.head.repo.full_name == github.repository"
        for step in (export, upload):
            self.assertIn(gate, step)
            self.assertNotIn('always()', step)
            self.assertNotIn('continue-on-error', step)
        self.assertNotIn('continue-on-error', text)
        self.assertLess(text.index('      - name: Full pinned tests with no network, credentials or provider access'), text.index('      - name: ' + export_name))
        self.assertLess(text.index('      - name: ' + export_name), text.index('      - name: ' + upload_name))
        self.assertEqual(artifact_paths(upload), {'${{ runner.temp }}/byron-image-transfer/image.tar.gz',
                                                '${{ runner.temp }}/byron-image-transfer/transfer.json'})
        self.assertIn('retention-days: 1\n', upload)
        self.assertIn('if-no-files-found: error', upload)
        self.assertIn("assert 0<info['Size']<=5*1024**3", export)
        self.assertIn('assert archive.stat().st_size<=1024**3', export)
        self.assertIn("['docker','save','byron-hosted-candidate']", export)
        for binding in ('head_commit=head', "image_id=info['Id']", 'archive_sha256=sha.hexdigest()', 'application_sources=sources'):
            self.assertIn(binding, export)

    def test_named_artifact_parser_does_not_depend_on_first_path_block(self):
        text = WORKFLOW.read_text()
        evidence = named_step(text, 'Upload only public dependency identities and review evidence')
        prefixed = '      - name: Unrelated artifact\n          path: |\n            private-config.json\n' + text
        self.assertEqual(named_step(prefixed, 'Upload only public dependency identities and review evidence'), evidence)
        with self.assertRaises(AssertionError):
            named_step(text + text, 'Upload only public dependency identities and review evidence')

if __name__ == '__main__': unittest.main()
