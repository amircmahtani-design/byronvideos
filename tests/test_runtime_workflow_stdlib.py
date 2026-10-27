"""Static PR workflow safety contracts; no runner, network or publication."""
from pathlib import Path
import unittest

WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/resolve-hosted-runtime.yml'

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

    def test_offline_tests_and_explicit_public_artifact_allowlist(self):
        text = WORKFLOW.read_text()
        self.assertIn('docker build --network=none --pull=false', text)
        self.assertIn('docker run --rm --network=none --read-only', text)
        self.assertIn('python3 -m pytest -q -p no:cacheprovider', text)
        self.assertIn('BYRON_REQUIRE_MEDIA_TESTS=1', text)
        paths = text.split('          path: |\n', 1)[1].split('\n#', 1)[0].splitlines()
        names = {line.strip().removeprefix('${{ runner.temp }}/byron-runtime-output/') for line in paths if line.strip()}
        self.assertEqual(names, {'base-resolution.json','materials.json','requirements.lock','sdk-lock.json',
                                'resolution-status.json','sbom.cdx.json','pypi-artifacts.json','apt-policy.txt',
                                'candidate-pins.txt','candidate-image-id.json'})
        self.assertNotIn('playwright install', text)
        self.assertNotIn('npm install', text)

if __name__ == '__main__': unittest.main()
