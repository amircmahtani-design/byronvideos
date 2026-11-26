#!/usr/bin/env python3
"""MANUAL CI dependency resolution only; never run in an application container.

Downloads public dependency materials, writes candidate locks/SBOM, installs
nothing. Requires explicit CI opt-in. No provider/project/config inputs accepted.
"""
import argparse,hashlib,json,os,platform,re,shutil,subprocess,sys,urllib.request,urllib.parse,zipfile
from email.parser import BytesParser
from pathlib import Path
try:from runtime_packaging import REQUIRED,normalized,verify_materials
except ImportError:from byron_video.runtime_packaging import REQUIRED,normalized,verify_materials

BASE_RE=re.compile(r'(?:docker\.io/)?(?:library/)?python@sha256:[0-9a-f]{64}\Z')
FFMPEG='7:7.1.5-0+deb13u1'
def need(ok,message):
    if not ok:raise ValueError(message)
def sha(raw):return hashlib.sha256(raw).hexdigest()
def dependency_failure_text(raw):
    """Bounded public tool output only; never dump arguments or environment."""
    text=(raw.decode('utf-8','replace') if isinstance(raw,bytes) else str(raw or ''))[:16384]
    def scrub(match):
        try:
            uri=urllib.parse.urlsplit(match.group(0))
            host=uri.hostname or ''
            return uri.scheme+'://'+host+uri.path
        except ValueError:return '[redacted-url]'
    text=re.sub(r'[A-Za-z][A-Za-z0-9+.-]*://[^\s<>"\']+',scrub,text)
    return ''.join(c if c in '\n\t' or (c.isprintable() and c!='\x7f') else '?' for c in text)

def command(args,**kwargs):
    try:
        return subprocess.run(args,check=True,capture_output=True,timeout=1200,**kwargs).stdout.decode()
    except subprocess.CalledProcessError as error:
        # No argv, environment, raw exception or private paths are serialized.
        diagnostic={'exit_code':error.returncode,
                    'stdout':dependency_failure_text(error.stdout),
                    'stderr':dependency_failure_text(error.stderr)}
        raise RuntimeError('Public dependency command failed: '+json.dumps(diagnostic,sort_keys=True)) from None

def validate_requirements(text):
    lines=[x.strip() for x in text.splitlines() if x.strip() and not x.strip().startswith('#')]
    need(lines and all(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*==[0-9][A-Za-z0-9.!+_-]*',x) for x in lines),
         'Candidate inputs allow exact public package pins only, no URLs/options.')
    return lines

def validate_report(report):
    need(isinstance(report,dict) and report.get('version')=='1' and isinstance(report.get('install'),list),'Bounded pip resolution report required.')
    records=[];seen=set()
    for row in report['install']:
        metadata=row['metadata'];name=normalized(metadata['name']);version=metadata['version'];info=row['download_info'];url=info['url'];parsed=urllib.parse.urlsplit(url)
        need(parsed.scheme=='https' and parsed.hostname=='files.pythonhosted.org' and parsed.port is None
             and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment,
             'Only exact official PyPI wheel URLs accepted.')
        filename=urllib.parse.unquote(parsed.path.rsplit('/',1)[-1]);digest=info['archive_info']['hashes']['sha256']
        need(Path(filename).name==filename and filename.endswith('.whl') and name not in seen and re.fullmatch(r'[a-f0-9]{64}',digest)
             and re.fullmatch(r'[a-z0-9][a-z0-9-]*',name) and re.fullmatch(r'[0-9][A-Za-z0-9.!+_-]*',version),'Unique exact wheel identity/hash required.')
        need(not row.get('is_yanked',False),'Yanked dependencies require separate review, not automatic candidate selection.')
        seen.add(name);records.append(dict(name=name,version=version,url=url,file=filename,sha256=digest))
    need(REQUIRED<=seen,'Resolution omitted required SDK/runtime packages.')
    return records

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise ValueError('Dependency download redirect is not authorized.')
def download_wheel(row,directory):
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    req=urllib.request.Request(row['url'],headers={'User-Agent':'Byron-dependency-candidate/1','Accept-Encoding':'identity'})
    path=directory/row['file'];digest=hashlib.sha256();size=0
    with opener.open(req,timeout=60) as response,path.open('xb') as output:
        need(response.status==200 and response.headers.get('Content-Encoding','identity')=='identity','Exact dependency response required.')
        while True:
            data=response.read(1024*1024)
            if not data:break
            size+=len(data);need(size<=500_000_000,'Dependency archive exceeds bound.');digest.update(data);output.write(data)
    need(digest.hexdigest()==row['sha256'],'Downloaded dependency differs from official resolution hash.')
    return dict(file=row['file'],sha256=row['sha256'],bytes=size)

def apt_source_diagnostic(fields):
    """Bounded source structure only: never raw text, credentials or environment."""
    def safe(value, limit=256):
        return ''.join(c if c.isascii() and (c.isalnum() or c in '/._+ -') else '?' for c in str(value)[:limit])
    result={'field_names':[safe(k,64) for k in sorted(fields)[:16]]}
    for key in ('Types','Suites','Components','Signed-By'):
        if key in fields:result[key]=safe(fields[key])
    result['uri_locations']=[]
    for uri in fields.get('URIs','').split()[:8]:
        try:
            parsed=urllib.parse.urlsplit(uri)
            result['uri_locations'].append({'host':safe(parsed.hostname or '',128),'path':safe(parsed.path,256)})
        except ValueError:result['uri_locations'].append({'invalid_uri':True})
    return json.dumps(result,sort_keys=True,separators=(',',':'))

def validate_apt_sources(root=Path('/etc/apt')):
    need(not (root/'sources.list').exists() or not (root/'sources.list').read_text().strip(),'Unexpected legacy apt sources.')
    files=list((root/'sources.list.d').iterdir());need(files and all(p.suffix=='.sources' and not p.is_symlink() for p in files),'Only explicit Debian deb822 sources accepted.')
    for path in files:
        text=path.read_text();need(len(text)<=16384,'APT source configuration exceeds bound.')
        blocks=[b for b in text.split('\n\n') if b.strip()]
        for block in blocks:
            fields={}
            for line in block.splitlines():
                if line.strip() and not line.lstrip().startswith('#'):
                    need(':' in line and not line.startswith((' ','\t')),'Unexpected apt source syntax.')
                    key,value=line.split(':',1);need(key not in fields,'Duplicate apt source field.');fields[key]=value.strip()
            need(set(fields)<= {'Types','URIs','Suites','Components','Signed-By'} and fields.get('Types')=='deb'
                 and fields.get('Signed-By') in ('/usr/share/keyrings/debian-archive-keyring.gpg',
                                                '/usr/share/keyrings/debian-archive-keyring.pgp')
                 and set(fields.get('Suites','').split())<= {'trixie','trixie-updates','trixie-security'}
                 and fields.get('Components')=='main','Only existing signed Debian trixie repositories accepted; source_structure='+apt_source_diagnostic(fields))
            for uri in fields['URIs'].split():
                parsed=urllib.parse.urlsplit(uri)
                need(parsed.scheme in ('http','https') and parsed.hostname=='deb.debian.org' and parsed.port is None
                    and parsed.path in ('/debian','/debian-security') and not parsed.query and not parsed.fragment
                    and not parsed.username and not parsed.password,'APT repository is not official Debian.')
    return True

def isolated_apt_config(out, debs):
    """Root-owned disposable indexes/cache; preserve installed status and trust."""
    out=Path(out).resolve();debs=Path(debs).resolve()
    need(debs==out/'debs' and re.fullmatch(r'/[A-Za-z0-9/_.-]+',str(out)),
         'APT workspace must be the explicit safe output directory.')
    workspace=out/'apt-workspace';workspace.mkdir(mode=0o755)
    for directory in (workspace/'lists',workspace/'lists/partial',workspace/'lists/auxfiles',
                      workspace/'cache',debs/'partial'):
        directory.mkdir(mode=0o755)
    config=workspace/'isolated.conf'
    config.write_text(
        '#clear APT::Update::Post-Invoke;\n'
        'Dir::State::status "/var/lib/dpkg/status";\n'
        f'Dir::State::lists "{workspace}/lists";\n'
        f'Dir::Cache "{workspace}/cache";\n'
        f'Dir::Cache::archives "{debs}";\n'
        'Dir::Cache::pkgcache "";\nDir::Cache::srcpkgcache "";\n')
    return config

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--resolve-public-dependencies',action='store_true')
    p.add_argument('--base-image',required=True);p.add_argument('--python-version',required=True);p.add_argument('--input',required=True,type=Path);p.add_argument('--output',required=True,type=Path)
    a=p.parse_args(argv)
    need(a.resolve_public_dependencies and os.environ.get('GITHUB_ACTIONS')=='true','Only explicit manual CI dependency-resolution stage is allowed.')
    need(BASE_RE.fullmatch(a.base_image) and re.fullmatch(r'3\.12\.\d+',a.python_version) and platform.python_version()==a.python_version
         and platform.machine()=='x86_64','Exact official base/interpreter/platform binding required.')
    need(not a.output.exists(),'Fresh dependency output directory required.');a.output.mkdir(mode=0o755);out=a.output.resolve()
    wheels=out/'wheels';debs=out/'debs';wheels.mkdir();debs.mkdir()
    validate_apt_sources();pins=validate_requirements(a.input.read_text());pins+=['pytest==9.1.1']
    inputs=out/'candidate-pins.txt';inputs.write_text('\n'.join(pins)+'\n')
    env={'PATH':os.environ['PATH'],'HOME':'/tmp/byron-pip-candidate','LANG':'C.UTF-8','PIP_CONFIG_FILE':'/dev/null'}
    report_path=out/'pip-report.json'
    command([sys.executable,'-m','pip','--isolated','--disable-pip-version-check','--no-input','--retries','0','--timeout','30','install','--dry-run','--ignore-installed','--no-cache-dir',
        '--only-binary=:all:','--index-url','https://pypi.org/simple','--report',str(report_path),'-r',str(inputs)],env=env)
    need(report_path.stat().st_size<=10_000_000,'Resolution report exceeds bound.')
    rows=validate_report(json.loads(report_path.read_text()));records=[download_wheel(row,wheels) for row in rows]
    # apt authenticates repository indexes/archive hashes with the existing Debian
    # archive keyring. Never allow insecure repos, unauthenticated packages/removals.
    apt_config=isolated_apt_config(out,debs)
    command(['apt-get','-c',str(apt_config),'-o','APT::Sandbox::User=root','-o','Acquire::Retries=0','update'])
    policy=command(['apt-cache','-c',str(apt_config),'policy','ffmpeg','ca-certificates']);(out/'apt-policy.txt').write_text(policy)
    command(['apt-get','-c',str(apt_config),'-o','APT::Sandbox::User=root','-o','Acquire::Retries=0','-o','APT::Get::AllowUnauthenticated=false','-o','Acquire::AllowInsecureRepositories=false',
        '-o','Dir::Cache::archives='+str(debs),'--download-only','--reinstall','--no-remove','--no-install-recommends','-y','install','ffmpeg='+FFMPEG,'ca-certificates'])
    shutil.rmtree(apt_config.parent)
    versions={}
    for line in command(['dpkg-query','-W','-f=${Package}\t${Version}\n']).splitlines():
        name,version=line.split('\t');versions[name]=version
    archives=[]
    for path in sorted(debs.glob('*.deb')):
        name=command(['dpkg-deb','-f',str(path),'Package']).strip();version=command(['dpkg-deb','-f',str(path),'Version']).strip()
        versions[name]=version;archives.append(dict(file=path.name,sha256=sha(path.read_bytes()),bytes=path.stat().st_size))
    for path in list(debs.iterdir()):
        if path.name=='lock':path.unlink()
        elif path.name=='partial':path.rmdir()
    need(versions.get('ffmpeg')==FFMPEG,'Exact historical FFmpeg package was not resolved; no version fallback.')
    manifest=dict(schema_version=1,status='RESOLVED_PUBLIC_CANDIDATE',base_image=a.base_image,python=a.python_version,
        platform='linux/amd64',ffmpeg_version='7.1.5-0+deb13u1',packages={r['name']:r['version'] for r in rows},
        system_packages=versions,wheels=records,debs=archives)
    lock=verify_materials(out,manifest)
    (out/'materials.json').write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n')
    (out/'requirements.lock').write_text(lock)
    (out/'sdk-lock.json').write_text(json.dumps({'python':'3.12','packages':manifest['packages']},sort_keys=True)+'\n')
    (out/'resolution-status.json').write_text(json.dumps(dict(status='CANDIDATE_FOR_REVIEW_NOT_ACTIVATED',automated_origin_validation=True,human_reviewed=False,base_image=a.base_image,python=a.python_version,provider_calls=0))+'\n')
    components=[dict(type='library',name=r['name'],version=r['version'],purl=f'pkg:pypi/{r["name"]}@{r["version"]}',hashes=[{'alg':'SHA-256','content':r['sha256']}]) for r in rows]
    components += [dict(type='library',name=n,version=v,purl=f'pkg:deb/debian/{n}@{urllib.parse.quote(v,safe="")}') for n,v in sorted(versions.items())]
    (out/'sbom.cdx.json').write_text(json.dumps({'bomFormat':'CycloneDX','specVersion':'1.5','version':1,'components':components},indent=2)+'\n')
    # Never upload raw resolver environment/report; export only allowlisted public
    # artifact identities and exact hashes. Actual archives stay in ephemeral CI.
    (out/'pypi-artifacts.json').write_text(json.dumps(rows,indent=2)+'\n');report_path.unlink()
    print(json.dumps({'candidate_materials_resolved':True,'human_reviewed':False,'activated':False,'deployed':False}));return 0
if __name__=='__main__':raise SystemExit(main())
