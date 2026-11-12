"""Offline dependency-material verification. Never resolves/downloads/activates."""
import argparse,hashlib,importlib.metadata,json,os,platform,re,subprocess,sys,zipfile
from email.parser import BytesParser
from pathlib import Path

REQUIRED={'google-auth','google-cloud-firestore','google-cloud-storage','google-cloud-secret-manager','requests',
          'httpx','pillow','numpy','fastapi','uvicorn','python-multipart','cryptography'}
FONT_SHA='57f73e11f51999432bf7ab22ce55b6f945d5eca1bf824404cfa9ec2e3718c84e'

def need(value,message):
    if not value:raise ValueError(message)
def normalized(name):return re.sub(r'[-_.]+','-',name).lower()
def digest(raw):return hashlib.sha256(raw).hexdigest()
def validate_manifest(value):
    fields={'schema_version','status','base_image','python','platform','ffmpeg_version','packages','system_packages','wheels','debs'}
    need(isinstance(value,dict) and set(value)==fields and value['schema_version']==1
         and value['status'] in ('REVIEWED_DEPENDENCY_MATERIALS','RESOLVED_PUBLIC_CANDIDATE'),'Resolved exact dependency-only material lock required; template is not executable.')
    need(isinstance(value['base_image'],str) and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9./_-]*@sha256:[a-f0-9]{64}',value['base_image']),
         'Independently verified immutable base-image digest required.')
    need(value['platform']=='linux/amd64' and isinstance(value['python'],str) and re.fullmatch(r'3\.12\.\d+',value['python']),
         'Reviewed Linux amd64 CPython 3.12 patch release required.')
    need(value['ffmpeg_version']=='7.1.5-0+deb13u1','Historical native renderer requires its exact FFmpeg build.')
    packages=value['packages'];need(isinstance(packages,dict) and REQUIRED<=set(packages),'Complete hosted SDK/media package closure required.')
    for name,version in packages.items():
        need(name==normalized(name) and re.fullmatch(r'[a-z0-9][a-z0-9-]*',name) and isinstance(version,str)
             and re.fullmatch(r'[0-9][A-Za-z0-9.!+_-]*',version),'Exact normalized dependency versions required.')
    need(packages['pillow']=='13.0.0' and packages['numpy']=='2.5.4','Media candidate pins cannot silently change.')
    need(isinstance(value['system_packages'],dict) and 'ffmpeg' in value['system_packages'] and 'ca-certificates' in value['system_packages'],
         'Reviewed FFmpeg/system TLS dependency closure required.')
    for name,version in value['system_packages'].items():
        need(re.fullmatch(r'[a-z0-9][a-z0-9+.-]*(?::amd64)?',name) and isinstance(version,str) and re.fullmatch(r'[A-Za-z0-9.+:~_-]+',version),'Exact system package version required.')
    for group,extension in [('wheels','.whl'),('debs','.deb')]:
        records=value[group];need(isinstance(records,list) and bool(records),'Offline dependency archives required.')
        seen=set()
        for item in records:
            need(isinstance(item,dict) and set(item)=={'file','sha256','bytes'} and isinstance(item['file'],str)
                 and Path(item['file']).name==item['file'] and item['file'].endswith(extension) and item['file'] not in seen,
                 'Safe unique dependency filename required.')
            seen.add(item['file']);need(isinstance(item['sha256'],str) and re.fullmatch(r'[a-f0-9]{64}',item['sha256'])
                and type(item['bytes']) is int and 0<item['bytes']<=500_000_000,'Exact dependency archive hash/size required.')
    return value

def verify_materials(root,manifest):
    manifest=validate_manifest(manifest);root=Path(root).resolve(strict=True);found={};lines=[]
    for group in ('wheels','debs'):
        directory=root/group
        need(directory.is_dir() and not directory.is_symlink(),'Regular offline dependency directory required.')
        need({p.name for p in directory.iterdir()}=={r['file'] for r in manifest[group]},'Unlisted/missing dependency archive.')
        for row in manifest[group]:
            path=directory/row['file'];need(path.is_file() and not path.is_symlink() and path.stat().st_size==row['bytes'],'Dependency archive size or path changed.')
            raw=path.read_bytes();need(digest(raw)==row['sha256'],'Dependency archive hash changed.')
            if group=='wheels':
                with zipfile.ZipFile(path) as archive:
                    metadata=[i for i in archive.infolist() if i.filename.endswith('.dist-info/METADATA')]
                    need(len(metadata)==1 and metadata[0].file_size<=2_000_000,'Bounded wheel metadata required.')
                    parsed=BytesParser().parsebytes(archive.read(metadata[0]))
                name=normalized(parsed.get('Name',''));version=parsed.get('Version','')
                need(name not in found and manifest['packages'].get(name)==version,'Wheel differs from locked package/version or duplicates it.')
                found[name]=version;lines.append(f'{name}=={version} --hash=sha256:{row["sha256"]}')
    need(found==manifest['packages'],'Wheelhouse must contain the complete explicitly locked dependency closure.')
    return '\n'.join(sorted(lines))+'\n'

def verify_installed(manifest,*,font_path=None):
    manifest=validate_manifest(manifest)
    need(platform.system()=='Linux' and platform.machine() in ('x86_64','AMD64') and platform.python_version()==manifest['python'],
         'Installed interpreter/platform differs from reviewed dependency lock.')
    for name,version in manifest['packages'].items():
        try:actual=importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:raise ValueError('Locked runtime dependency missing; no installation attempted.') from None
        need(actual==version,'Installed dependency version differs from reviewed lock.')
    for name,version in manifest['system_packages'].items():
        result=subprocess.run(['dpkg-query','-W','-f=${Version}',name],capture_output=True,timeout=10,check=True)
        need(result.stdout.decode()==version,'Installed system library differs from reviewed lock.')
    for executable in ('ffmpeg','ffprobe'):
        result=subprocess.run([executable,'-version'],capture_output=True,timeout=15,check=True)
        need(result.stdout.decode().splitlines()[0].split()[2]==manifest['ffmpeg_version'],'Native media executable differs from reviewed lock.')
    subprocess.run([sys.executable,'-m','pip','check'],check=True,capture_output=True,timeout=60)
    if font_path is not None:
        path=Path(font_path);need(path.is_file() and not path.is_symlink() and digest(path.read_bytes())==FONT_SHA,'Bundled compositor font changed.')
    return True

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('mode',choices=['materials','installed'])
    parser.add_argument('--manifest',required=True,type=Path);parser.add_argument('--root',type=Path);parser.add_argument('--font',type=Path)
    parser.add_argument('--expected-base');parser.add_argument('--write-lock',type=Path);parser.add_argument('--write-sdk-lock',type=Path)
    args=parser.parse_args(argv);value=validate_manifest(json.loads(args.manifest.read_text()))
    if args.expected_base is not None:need(value['base_image']==args.expected_base,'Build base differs from reviewed lock.')
    if args.mode=='materials':
        need(args.root is not None,'Offline material root required.');lock=verify_materials(args.root,value)
        if args.write_lock is not None:
            with args.write_lock.open('x') as output:output.write(lock)
        if args.write_sdk_lock is not None:
            with args.write_sdk_lock.open('x') as output:
                json.dump({'python':'3.12','packages':value['packages']},output,sort_keys=True);output.write('\n')
    else:verify_installed(value,font_path=args.font)
    print(json.dumps({'dependency_materials_verified':True,'activated':False,'deployed':False}));return 0

if __name__=='__main__':raise SystemExit(main())
