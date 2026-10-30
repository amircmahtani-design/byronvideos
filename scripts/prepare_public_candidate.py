#!/usr/bin/env python3
"""Explicit public source allowlist + SHA inventory; never publishes anything.

Default is inventory only. Optional --copy-to creates a NEW sanitized directory.
Excluded recovery/private filenames are not emitted into the public inventory.
This mechanical allowlist does not replace a human privacy/security review.
"""
import argparse,hashlib,json,shutil
from pathlib import Path
TOP={'.dockerignore','.gitattributes','.gitignore','README.md','RELEASE.md','THIRD_PARTY_NOTICES.md','netlify.toml',
     'Dockerfile.worker','Dockerfile.service','Dockerfile.dependencies','Dockerfile.hosted-candidate',
     'requirements.txt','requirements-dev.txt','requirements-test.txt','requirements-hosted.in'}
HOSTED={'cloud-run.target.draft.json','deployment.private.template.json','narration-profile.draft.json','runtime-materials.template.json'}
SCRIPTS={'install_ci_ffmpeg.py','make_demo.py','resolve_runtime_candidate.py','prepare_public_candidate.py'}
WORKFLOWS={'checks.yml','hosted-runtime-candidate.yml','resolve-hosted-runtime.yml'}
BASELINE_IMAGES={'style-guide/references/synthetic-baselines/landscape.png','style-guide/references/synthetic-baselines/portrait.png'}

def allowed(relative):
    p=Path(relative);parts=p.parts
    if any(x in ('.git','.private','__pycache__','.pytest_cache','validated','outputs','uploads') for x in parts):return False
    if len(parts)==1:return p.name in TOP
    if len(parts)==2 and parts[0] in ('byron_video','tests'):return p.suffix=='.py'
    if len(parts)==2 and parts[0]=='scripts':return p.name in SCRIPTS
    if len(parts)==3 and parts[:2]==('.github','workflows'):return p.name in WORKFLOWS
    if len(parts)==2 and parts[0]=='hosted':return p.name in HOSTED
    if len(parts)==2 and parts[0]=='web':return p.suffix in ('.mjs','.html','.css','.txt') or p.name in ('sitemap.xml','ui.test.cjs','README.md')
    if parts[0]=='style-guide':
        if p.suffix=='.png':return p.as_posix() in BASELINE_IMAGES
        if p.suffix=='.ttf':return p.as_posix() in ('style-guide/fonts/DejaVuSans.ttf','style-guide/fonts/Italianno-Regular.ttf')
        return p.suffix in ('.json','.md','.txt') or p.name=='.gitkeep'
    return False

def inventory(root):
    root=Path(root).resolve(strict=True);rows=[];omitted=0
    for path in sorted(root.rglob('*')):
        if not path.is_file():continue
        relative=path.relative_to(root)
        if not allowed(relative):omitted+=1;continue
        if path.is_symlink() or any(p.is_symlink() for p in path.parents if p!=root):raise ValueError('Public candidate cannot contain symlinks.')
        raw=path.read_bytes();rows.append(dict(path=relative.as_posix(),bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest()))
    return dict(schema_version=1,status='PUBLIC_SOURCE_CANDIDATE_REQUIRES_REVIEW',files=rows,omitted_file_count=omitted,
                private_configuration_included=False,publication_performed=False)

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,default=Path(__file__).resolve().parent.parent)
    p.add_argument('--inventory',required=True,type=Path);p.add_argument('--copy-to',type=Path);a=p.parse_args(argv)
    data=inventory(a.root)
    if a.copy_to:
        if a.copy_to.exists() or a.copy_to.resolve().is_relative_to(a.root.resolve()):raise ValueError('New public candidate destination outside source required.')
        a.copy_to.mkdir(parents=True,mode=0o755)
        for row in data['files']:
            target=a.copy_to/row['path'];target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(a.root/row['path'],target)
            if hashlib.sha256(target.read_bytes()).hexdigest()!=row['sha256']:raise ValueError('Source changed during public candidate copy.')
    with a.inventory.open('x') as output:json.dump(data,output,indent=2,sort_keys=True);output.write('\n')
    print(json.dumps({'allowlisted_files':len(data['files']),'omitted_files':data['omitted_file_count'],'published':False}));return 0
if __name__=='__main__':raise SystemExit(main())
