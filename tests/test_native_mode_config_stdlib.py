"""Explicit native API selection is configuration-only, never fallback/activation."""
import copy,unittest
from byron_video.deployment_plan import validate_config
from test_deployment_plan_stdlib import config

class NativeModeConfigChecks(unittest.TestCase):
    def test_legacy_vertex_config_preserves_exact_bytes_and_no_default_insertion(self):
        value=config();self.assertEqual(validate_config(value),value)
        self.assertNotIn('native_api_mode',validate_config(value))
        value['native_api_mode']='vertex_eu';self.assertEqual(validate_config(value),value)
    def test_developer_requires_explicit_mode_and_existing_same_project_resource(self):
        value=config();value['native_api_mode']='gemini_developer'
        with self.assertRaises(ValueError):validate_config(value)
        value['secret_resources']['gemini']='projects/synthetic-project/secrets/synthetic_gemini'
        self.assertEqual(validate_config(value),value)
        for reference in ('literal-private-api-key','projects/other-project/secrets/gemini'):
            bad=copy.deepcopy(value);bad['secret_resources']['gemini']=reference
            with self.assertRaises(ValueError):validate_config(bad)
    def test_adding_key_never_selects_developer_or_allows_auto_fallback(self):
        value=config();value['secret_resources']['gemini']='projects/synthetic-project/secrets/synthetic_gemini'
        checked=validate_config(value);self.assertNotIn('native_api_mode',checked)
        for mode in ('auto','fallback','gemini','',None):
            value['native_api_mode']=mode
            with self.assertRaises(ValueError):validate_config(value)
if __name__=='__main__':unittest.main()
