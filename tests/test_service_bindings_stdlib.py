"""Packaged service construction with fake SDK/identity; no metadata or secrets read."""
import copy,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from byron_video import service_bindings as packaged
from byron_video import service_bootstrap as bootstrap
from byron_video.deployment_plan import validate_config
from test_deployment_plan_stdlib import config
from test_hosted_uploads_stdlib import plan
from test_runtime_assembly_stdlib import signed


def service_config():
    value=config();value['firestore']['database']='byron-studio'
    value['runtime'].update(service_service_account='synthetic-service@synthetic-project.iam.gserviceaccount.com',csrf_secret_version='7')
    return value

class ServiceBindingsChecks(unittest.TestCase):
    def setUp(self):
        self.config,self.plan=service_config(),plan();self.envelope,self.public,_=signed(self.config,self.plan)
        self.calls=[];self.secret=b'synthetic-only-csrf-secret-at-least-32-bytes'
        self.name=self.config['secret_resources']['csrf']+'/versions/7'
        self.response=SimpleNamespace(name=self.name,payload=SimpleNamespace(data=self.secret,data_crc32c=packaged.crc32c(self.secret)))
        def read(**kwargs):self.calls.append(('secret',kwargs));return self.response
        self.clients=(SimpleNamespace(_database_string='projects/synthetic-project/databases/byron-studio'),SimpleNamespace(project='synthetic-project'),SimpleNamespace(access_secret_version=read))
        def verify():self.calls.append(('identity',None));return True
        self.bundle=SimpleNamespace(credentials=SimpleNamespace(service_account_email=self.config['runtime']['service_service_account']),token_reader=lambda:'synthetic-access-token',verify_identity=verify)
    def args(self,**changes):return dict(dict(config=self.config,processing_plan=self.plan,sdk_lock_bytes=b'fixture lock',acceptance=self.envelope,reviewer_public_key=self.public,enabled=True),**changes)
    def build(self,**changes):
        with patch.object(bootstrap,'validate_service_runtime',side_effect=lambda *a:self.calls.append(('lock',None))),patch.object(packaged,'create_attached_credentials',return_value=self.bundle),patch.object(packaged,'_sdk_clients',side_effect=lambda *a:(self.calls.append(('sdk',None)) or self.clients)):
            return packaged.build_service_bindings(**self.args(**changes))
    def test_crc32c_known_castagnoli_vector(self):
        self.assertEqual(packaged.crc32c(b'123456789'),0xe3069283)
    def test_disabled_default_never_discovers_credentials_or_reads_secrets(self):
        with patch.object(packaged,'create_attached_credentials') as credentials,patch.object(packaged,'_sdk_clients') as sdk,self.assertRaises(ValueError):
            packaged.build_service_bindings(**self.args(enabled=False))
        credentials.assert_not_called();sdk.assert_not_called();self.assertEqual(self.calls,[])
    def test_lock_or_signed_acceptance_fails_before_identity(self):
        with patch.object(bootstrap,'validate_service_runtime',side_effect=ValueError('lock')),patch.object(packaged,'create_attached_credentials') as identity,self.assertRaises(ValueError):
            packaged.build_service_bindings(**self.args())
        identity.assert_not_called()
        with self.assertRaises(ValueError):self.build(acceptance={})
        self.assertFalse(any(name=='identity' for name,_ in self.calls))
    def test_verified_identity_precedes_sdk_and_pinned_integrity_checked_csrf_read(self):
        result=self.build()
        self.assertEqual([x[0] for x in self.calls],['lock','identity','sdk','secret'])
        self.assertEqual(self.calls[-1][1],dict(request={'name':self.name},retry=None,timeout=20))
        self.assertEqual(result.csrf_secret,self.secret);self.assertNotIn(self.secret.decode(),repr(result))
    def test_mismatched_attached_identity_prevents_sdk_and_secret_access(self):
        self.bundle.credentials.service_account_email=self.config['runtime']['worker_service_account']
        with self.assertRaises(ValueError):self.build()
        self.assertFalse(any(name in {'sdk','secret'} for name,_ in self.calls))
    def test_default_database_or_wrong_sdk_database_prevents_secret_access(self):
        value=copy.deepcopy(self.config);value['firestore']['database']='(default)'
        with self.assertRaises(ValueError):self.build(config=value)
        self.clients[0]._database_string='projects/synthetic-project/databases/(default)'
        with self.assertRaises(ValueError):self.build()
        self.assertFalse(any(name=='secret' for name,_ in self.calls))
    def test_secret_name_integrity_and_length_are_mandatory(self):
        for response in [SimpleNamespace(name=self.name+'/extra',payload=self.response.payload),
                SimpleNamespace(name=self.name,payload=SimpleNamespace(data=self.secret,data_crc32c=0)),
                SimpleNamespace(name=self.name,payload=SimpleNamespace(data=b'short',data_crc32c=packaged.crc32c(b'short')))]:
            self.response=response
            with self.assertRaises(ValueError):self.build()
    def test_named_database_and_distinct_service_identity_schema(self):
        self.assertEqual(validate_config(self.config)['firestore']['database'],'byron-studio')
        for updates in [dict(service_service_account=self.config['runtime']['worker_service_account']),dict(csrf_secret_version='latest'),dict(csrf_secret_version=7)]:
            value=copy.deepcopy(self.config);value['runtime'].update(updates)
            with self.assertRaises(ValueError):validate_config(value)
        with self.assertRaises(ValueError):self.build(config=config())
    def test_sdk_clients_use_explicit_credentials_project_database_and_fixed_endpoints(self):
        import sys
        calls=[]
        def constructor(kind):return lambda **kwargs:(calls.append((kind,kwargs)) or SimpleNamespace())
        google=SimpleNamespace(cloud=SimpleNamespace())
        cloud=SimpleNamespace(firestore=SimpleNamespace(Client=constructor('firestore')),storage=SimpleNamespace(Client=constructor('storage')),secretmanager=SimpleNamespace(SecretManagerServiceClient=constructor('secret')))
        with patch.dict(sys.modules,{'google':google,'google.cloud':cloud}):packaged._sdk_clients(self.config,self.bundle.credentials)
        self.assertEqual(calls[0][1]['database'],'byron-studio')
        for _,kwargs in calls:self.assertIs(kwargs['credentials'],self.bundle.credentials)
        self.assertEqual(calls[2][1]['client_options']['api_endpoint'],'secretmanager.googleapis.com')
    def test_cli_declarative_serving_uses_packaged_factory_not_private_python(self):
        with tempfile.TemporaryDirectory() as root:
            def private(name,data):
                p=Path(root)/name;p.write_bytes(data);p.chmod(0o600);return str(p)
            args=['--config',private('config',json.dumps(self.config).encode()),'--processing-plan',private('plan',json.dumps(self.plan).encode()),
                '--sdk-lock',private('lock',b'{}'),'--acceptance',private('acceptance',json.dumps(self.envelope).encode()),
                '--reviewer-public-key',private('key',self.public),
                '--production-policy',private('policy',b'{}'),'--policy-public-key',private('policy-key',self.public),
                '--billing-contract',private('billing',b'{}'),'--billing-public-key',private('billing-key',self.public),
                '--trusted-proxy','127.0.0.1','--serve']
            # This is CLI argument-routing only; real signed admission is covered by
            # production_admission and packaged fresh-flow integration tests.
            with patch('byron_video.production_admission.ProductionAdmission') as admission,patch.object(bootstrap,'validate_service_runtime'),patch.object(packaged,'build_service_bindings',return_value=object()) as factory,patch.object(bootstrap,'load_bindings') as legacy,patch.object(bootstrap,'build_service',return_value=SimpleNamespace(app=object())),patch('uvicorn.run') as run:
                self.assertEqual(bootstrap.main(args),0);admission.assert_called_once();factory.assert_called_once();legacy.assert_not_called();run.assert_called_once()
            with patch.object(bootstrap,'validate_service_runtime'),patch.object(packaged,'build_service_bindings') as factory,self.assertRaises(SystemExit):
                bootstrap.main(args+['--bindings','/not/executed.py','--bindings-sha256','0'*64])
            factory.assert_not_called()
