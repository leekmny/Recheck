"""Guarded Recheck PR/release CLI. Credentials stay in memory; writes require --execute.

API contracts: https://docs.github.com/en/rest/pulls/pulls and
https://docs.github.com/en/rest/releases/assets (2026-03-10).
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REPOSITORY='leekmny/Recheck'
BRANCH='feature/recheck-distribution-validation'
TAG='v0.1.3'
API_VERSION='2026-03-10'
ROOT=Path(__file__).resolve().parents[1]
CHUNK=1024*1024

class SafeError(Exception): pass

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        raise SafeError('Unexpected API redirect; authentication was not forwarded')

def checked_ref(value):
    if (not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,199}',value)
        or '..' in value or '//' in value or value.endswith(('/', '.', '.lock'))):
        raise SafeError('A valid branch or tag name is required')
    return value

def checked_sha(value):
    if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{40}',value):
        raise SafeError('A full immutable Git commit SHA is required')
    return value

def credential():
    result=subprocess.run(['git','credential','fill'],input='protocol=https\nhost=github.com\n\n',
        cwd=ROOT,env={**os.environ,'GIT_TERMINAL_PROMPT':'0','GCM_INTERACTIVE':'never'},
        capture_output=True,text=True,encoding='utf-8',timeout=30)
    if result.returncode:
        raise SafeError('Git Credential Manager did not provide GitHub authentication')
    fields=dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
    token=fields.get('password')
    if not token: raise SafeError('No GitHub credential was available in memory')
    return token

def safe_run(run):
    return {key:run.get(key) for key in ('id','name','event','status','conclusion','html_url','head_sha','head_branch','run_attempt')}

def safe_release(release):
    return {key:release.get(key) for key in ('id','tag_name','target_commitish','html_url','draft','prerelease')}

class GitHub:
    def __init__(self,*,execute=False,token=None,branch=BRANCH,tag=TAG):
        self.branch,self.tag=checked_ref(branch),checked_ref(tag)
        self._token=token if token is not None else credential()
        self.execute=execute
        self.opener=urllib.request.build_opener(NoRedirect())
    def __repr__(self): return '<GitHub repository=leekmny/Recheck authentication=redacted>'
    def headers(self):
        return {'Authorization':'Bearer '+self._token,'Accept':'application/vnd.github+json',
                'X-GitHub-Api-Version':API_VERSION,'User-Agent':'Recheck-release-QA'}
    def request(self,method,path,data=None,*,missing_ok=False):
        if method!='GET' and not self.execute: raise SafeError('Writes are disabled; explicit --execute authorization is required')
        prefix='/repos/'+REPOSITORY
        if path!=prefix and not path.startswith(prefix+'/') and not path.startswith(prefix+'?'):
            raise SafeError('Request is outside the selected repository')
        if any(part in ('.','..') for part in urllib.parse.unquote(path.split('?',1)[0]).split('/')):
            raise SafeError('Request is outside the selected repository')
        if method!='GET':
            repository=self.assert_repository()
            if (repository['visibility']=='public' and method=='PUT'
                and re.fullmatch(re.escape(prefix)+r'/pulls/[1-9][0-9]*/merge',
                                 urllib.parse.unquote(path.split('?',1)[0]))):
                raise SafeError('Server-side merge is disabled for email privacy; use a reviewed local merge with noreply author and committer')
        return self._transport(method,path,data,missing_ok=missing_ok)
    def assert_repository(self):
        repository=self.request('GET',self.endpoint(''))
        if (not isinstance(repository,dict) or repository.get('full_name')!=REPOSITORY
            or type(repository.get('private')) is not bool
            or repository.get('visibility') not in ('private','public')
            or repository['private']!=(repository['visibility']=='private')):
            raise SafeError('Mutations require the selected repository with verified public/private visibility')
        return repository
    def _transport(self,method,path,data=None,*,missing_ok=False):
        body=None if data is None else json.dumps(data,ensure_ascii=False).encode('utf-8')
        headers=self.headers()
        if body is not None: headers['Content-Type']='application/json'
        request=urllib.request.Request('https://api.github.com'+path,data=body,headers=headers,method=method)
        try:
            with self.opener.open(request,timeout=60) as response:
                payload=response.read(4*1024*1024+1)
                if len(payload)>4*1024*1024: raise SafeError('API JSON exceeded the response size limit')
                return json.loads(payload) if payload else None
        except urllib.error.HTTPError as error:
            code=error.code; error.close()
            if code==404 and missing_ok:return None
            raise SafeError(f'GitHub API returned HTTP {code} for {method} {path.split("?")[0]}') from None
        except (urllib.error.URLError,TimeoutError):
            raise SafeError('GitHub API network request failed') from None
    def endpoint(self,suffix):return '/repos/'+REPOSITORY+suffix
    def branch_sha(self,missing_ok=False,branch=None):
        branch=checked_ref(branch or self.branch)
        record=self.request('GET',self.endpoint('/branches/'+urllib.parse.quote(branch,safe='')),missing_ok=missing_ok)
        return record['commit']['sha'] if record else None
    def assert_head(self,sha,branch=None):
        checked_sha(sha)
        if self.branch_sha(branch=branch)!=sha:raise SafeError('Remote selected branch does not match the reviewed commit')
    def release_for_tag(self):
        found=self.request('GET',self.endpoint('/releases/tags/'+urllib.parse.quote(self.tag,safe='')),missing_ok=True)
        if found is not None:return found
        # The by-tag endpoint can omit drafts; authenticated release inventory
        # prevents accidentally creating a second draft for the same version.
        for page in range(1,21):
            releases=self.request('GET',self.endpoint(f'/releases?per_page=100&page={page}'))
            for release in releases:
                if release.get('tag_name')==self.tag:return release
            if len(releases)<100:return None
        raise SafeError('Release inventory exceeded its bounded absence-check range')
    def preflight(self):
        repository=self.request('GET',self.endpoint(''))
        main=self.request('GET',self.endpoint('/branches/main'))
        tag=self.request('GET',self.endpoint('/git/ref/tags/'+urllib.parse.quote(self.tag,safe='')),missing_ok=True)
        release=self.release_for_tag()
        return {'checked_at':datetime.now(timezone.utc).isoformat(),'repository':repository['full_name'],
          'private':repository['private'],'default_branch':repository['default_branch'],
          'permissions':repository.get('permissions',{}),'main_sha':main['commit']['sha'],
          'main_protected':main.get('protected'),'branch':self.branch,'branch_sha':self.branch_sha(missing_ok=True),
          'tag':self.tag,'tag_exists':tag is not None,'release_exists':release is not None,
          'release':safe_release(release) if release else None}
    def ci(self,sha,event='push',branch=None):
        branch=branch or self.branch
        self.assert_head(sha,branch=branch)
        query={'branch':branch,'head_sha':sha,'per_page':100}
        if event!='any':query['event']=event
        result=self.request('GET',self.endpoint('/actions/runs?'+urllib.parse.urlencode(query)))
        runs=[]; workflows=set()
        if result.get('total_count',0)>100:raise SafeError('CI inventory exceeds bounded verification range')
        for run in sorted(result.get('workflow_runs',[]),key=lambda row:row.get('id',0),reverse=True):
            if run.get('head_sha')!=sha or run.get('head_branch')!=branch or (event!='any' and run.get('event')!=event):continue
            key=(run.get('workflow_id'),run.get('event'))
            if key in workflows:continue
            workflows.add(key); runs.append(safe_run(run))
        complete=any(run['name']=='Unit tests' for run in runs) and all(run['status']=='completed' for run in runs)
        return {'sha':sha,'event':event,'complete':complete,
                'success':complete and all(run['conclusion']=='success' for run in runs),'runs':runs}
    def draft_pr(self,sha,title,body):
        self.assert_head(sha)
        query=urllib.parse.urlencode({'state':'open','head':REPOSITORY.split('/')[0]+':'+self.branch,'base':'main'})
        existing=self.request('GET',self.endpoint('/pulls?'+query))
        if existing:raise SafeError('An open pull request already exists for this branch; no duplicate was created')
        result=self.request('POST',self.endpoint('/pulls'),{'title':title,'body':body,'head':self.branch,'base':'main','draft':True})
        return {key:result.get(key) for key in ('number','html_url','draft','state')}
    def _green_ci(self,sha):
        status=self.ci(sha)
        if not status['success']:raise SafeError('Exact-head Unit tests CI must be completed and green')
        return status
    def _pull(self,number,sha):
        if type(number) is not int or number<=0:raise SafeError('A positive PR number is required')
        self.assert_head(sha)
        pull=self.request('GET',self.endpoint(f'/pulls/{number}'))
        head,base=pull.get('head',{}),pull.get('base',{})
        if (head.get('sha')!=sha or head.get('ref')!=self.branch
            or (head.get('repo') or {}).get('full_name')!=REPOSITORY
            or base.get('ref')!='main' or (base.get('repo') or {}).get('full_name')!=REPOSITORY
            or pull.get('state')!='open'):
            raise SafeError('Pull request does not match the reviewed commit and selected branch')
        return pull
    def ready_pr(self,number,sha):
        if not self.execute:raise SafeError('Writes are disabled; explicit --execute authorization is required')
        pull=self._pull(number,sha)
        self._green_ci(sha)
        if not pull.get('draft'):raise SafeError('Pull request is already ready; no change was made')
        node=pull.get('node_id')
        if not isinstance(node,str) or not re.fullmatch('[A-Za-z0-9_=-]{1,200}',node):raise SafeError('Invalid selected PR node identity')
        self.assert_repository()
        self.assert_head(sha)
        result=self._transport('POST','/graphql',{
            'query':'mutation($id:ID!){markPullRequestReadyForReview(input:{pullRequestId:$id}){pullRequest{number isDraft}}}',
            'variables':{'id':node}})
        if result.get('errors'):raise SafeError('GitHub declined PR readiness; response details suppressed')
        ready=result.get('data',{}).get('markPullRequestReadyForReview',{}).get('pullRequest',{})
        if ready.get('number')!=number or ready.get('isDraft') is not False:raise SafeError('PR readiness did not verify')
        return dict(number=number,draft=False,head_sha=sha)
    def merge_pr(self,number,sha):
        pull=self._pull(number,sha)
        self._green_ci(sha)
        if pull.get('draft') or pull.get('mergeable') is not True:raise SafeError('PR is draft or mergeability is not confirmed')
        result=self.request('PUT',self.endpoint(f'/pulls/{number}/merge'),{'sha':sha,'merge_method':'merge'})
        if result.get('merged') is not True:raise SafeError('GitHub did not merge the selected PR')
        return dict(number=number,merged=True,sha=checked_sha(result.get('sha')))
    def create_release(self,sha,title,body):
        self.assert_head(sha)
        if self.request('GET',self.endpoint('/git/ref/tags/'+urllib.parse.quote(self.tag,safe='')),missing_ok=True) is not None:raise SafeError('Release tag already exists; it was not changed')
        if self.release_for_tag() is not None:raise SafeError('Release already exists; it was not changed')
        self._green_ci(sha)
        result=self.request('POST',self.endpoint('/releases'),{'tag_name':self.tag,'target_commitish':sha,'name':title,'body':body,'draft':True,'prerelease':True,'generate_release_notes':False})
        return safe_release(result)
    def _local_zip(self,path,expected_sha256,expected_size):
        if not re.fullmatch('[0-9a-f]{64}',str(expected_sha256)) or type(expected_size) is not int or expected_size<=0:
            raise SafeError('Reviewed local ZIP SHA256 and positive size are required')
        path=Path(path)
        if path.is_symlink():raise SafeError('Release ZIP must not be a symbolic link')
        try:path=path.resolve(strict=True)
        except OSError:raise SafeError('Reviewed local ZIP is missing') from None
        if path.suffix.lower()!='.zip' or not path.is_file():raise SafeError('A reviewed regular local ZIP is required')
        initial=path.stat()
        digest=hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda:stream.read(CHUNK),b''):digest.update(chunk)
        if initial.st_size!=expected_size or digest.hexdigest()!=expected_sha256:
            raise SafeError('Reviewed local ZIP hash or size does not match')
        return path,initial
    def _selected_release(self,release_id,sha):
        if type(release_id) is not int or release_id<=0:raise SafeError('A positive release ID is required')
        self.assert_head(sha)
        release=self.request('GET',self.endpoint(f'/releases/{release_id}'))
        if (release.get('id')!=release_id or release.get('tag_name')!=self.tag
            or release.get('target_commitish')!=sha or not release.get('draft') or not release.get('prerelease')):
            raise SafeError('Operation requires the exact selected draft prerelease and target SHA')
        return release
    def upload_asset(self,release_id,path,*,sha,expected_sha256,expected_size,connection_factory=http.client.HTTPSConnection,progress=True):
        if not self.execute:raise SafeError('Writes are disabled; explicit --execute authorization is required')
        path,initial=self._local_zip(path,expected_sha256,expected_size)
        release=self._selected_release(release_id,sha)
        if any(asset.get('name')==path.name for asset in release.get('assets',[])):raise SafeError('An asset with this filename already exists; it was not replaced')
        url=urllib.parse.urlsplit(release['upload_url'].split('{',1)[0])
        if (url.scheme!='https' or url.netloc!='uploads.github.com'
            or url.path!=self.endpoint(f'/releases/{release_id}/assets')):raise SafeError('Unexpected release upload destination')
        self.assert_repository()
        target=url.path+'?'+urllib.parse.urlencode({'name':path.name})
        headers=self.headers(); headers['Content-Type']='application/zip'; headers['Content-Length']=str(initial.st_size)
        connection=connection_factory('uploads.github.com',timeout=120)
        digest=hashlib.sha256(); sent=0; reported=0
        try:
            connection.putrequest('POST',target)
            for key,value in headers.items():connection.putheader(key,value)
            connection.endheaders()
            with path.open('rb') as stream:
                for chunk in iter(lambda:stream.read(CHUNK),b''):
                    connection.send(chunk); digest.update(chunk); sent+=len(chunk)
                    if progress and sent-reported>=64*CHUNK:
                        print(json.dumps({'operation':'upload_progress','uploaded_bytes':sent,'total_bytes':initial.st_size}),flush=True)
                        reported=sent
            response=connection.getresponse(); payload=response.read(1024*1024+1)
            if response.status!=201:raise SafeError(f'Asset upload returned HTTP {response.status}; no response body was logged')
            if len(payload)>1024*1024:raise SafeError('Asset upload response exceeded the size limit')
            asset=json.loads(payload)
        finally:connection.close()
        final=path.stat(); sha=digest.hexdigest()
        if sent!=initial.st_size or sha!=expected_sha256 or (initial.st_size,initial.st_mtime_ns)!=(final.st_size,final.st_mtime_ns):raise SafeError('Release ZIP changed during upload')
        if asset.get('name')!=path.name or asset.get('size')!=sent or asset.get('state')!='uploaded':raise SafeError('Uploaded asset name/size/state did not verify')
        if asset.get('digest')!='sha256:'+sha:raise SafeError('Uploaded asset digest differs or is missing from the server response')
        return {**{key:asset.get(key) for key in ('id','name','size','state','digest','browser_download_url')},'local_sha256':sha}
    def publish_release(self,release_id,*,sha,asset_id,path,expected_sha256,expected_size):
        if not self.execute:raise SafeError('Writes are disabled; explicit --execute authorization is required')
        path,_=self._local_zip(path,expected_sha256,expected_size)
        release=self._selected_release(release_id,sha)
        self._green_ci(sha)
        asset=next((item for item in release.get('assets',[]) if item.get('id')==asset_id),None)
        if (not asset or asset.get('name')!=path.name or asset.get('state')!='uploaded'
            or asset.get('size')!=expected_size or asset.get('digest')!='sha256:'+expected_sha256):
            raise SafeError('The exact verified asset ID/name/size/digest is required before publishing')
        tag=self.request('GET',self.endpoint('/git/ref/tags/'+urllib.parse.quote(self.tag,safe='')),missing_ok=True)
        if tag is not None:
            target=tag.get('object',{})
            for _ in range(5):
                if target.get('type')!='tag':break
                oid=checked_sha(target.get('sha'))
                target=self.request('GET',self.endpoint('/git/tags/'+oid)).get('object',{})
            if target.get('type')!='commit' or target.get('sha')!=sha:
                raise SafeError('Release tag points to a different or unverified commit; it was not changed')
        return safe_release(self.request('PATCH',self.endpoint(f'/releases/{release_id}'),{'draft':False,'prerelease':True}))

def body_file(path):
    result=Path(path).read_text(encoding='utf-8-sig')
    if not result.strip() or len(result)>32768:raise SafeError('A nonempty reviewed Markdown body under 32 KiB is required')
    return result

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute',action='store_true',help='Enable only the explicitly authorized GitHub mutation command')
    parser.add_argument('--output-json',type=Path)
    parser.add_argument('--branch',default=BRANCH,help='Reviewed remote head branch (main for post-merge release)')
    parser.add_argument('--tag',default=TAG,help='New prerelease tag; existing tags are never changed')
    commands=parser.add_subparsers(dest='operation',required=True)
    commands.add_parser('preflight')
    for name in ('ci-status','watch-ci'):
        sub=commands.add_parser(name); sub.add_argument('--sha',required=True); sub.add_argument('--event',default='push',choices=('push','pull_request','workflow_dispatch','any'))
        if name=='watch-ci':sub.add_argument('--timeout-seconds',type=int,default=1800);sub.add_argument('--poll-seconds',type=int,default=20)
    for name in ('draft-pr','create-release'):
        sub=commands.add_parser(name);sub.add_argument('--sha',required=True);sub.add_argument('--title',required=True);sub.add_argument('--body-file',required=True,type=Path)
    for name in ('ready-pr','merge-pr'):
        sub=commands.add_parser(name);sub.add_argument('--pr-number',type=int,required=True);sub.add_argument('--sha',required=True)
    for name in ('upload-asset','publish-release'):
        sub=commands.add_parser(name)
        sub.add_argument('--release-id',type=int,required=True)
        sub.add_argument('--sha',required=True)
        sub.add_argument('--file',type=Path,required=True)
        sub.add_argument('--sha256',required=True)
        sub.add_argument('--size',type=int,required=True)
        if name=='publish-release':sub.add_argument('--asset-id',type=int,required=True)
    sub=commands.add_parser('release-status');sub.add_argument('--release-id',type=int,required=True)
    args=parser.parse_args(argv)
    client=GitHub(execute=args.execute,branch=args.branch,tag=args.tag)
    if args.operation=='preflight':result=client.preflight()
    elif args.operation=='ci-status':result=client.ci(args.sha,args.event,args.branch)
    elif args.operation=='watch-ci':
        if not 5<=args.poll_seconds<=60 or not 1<=args.timeout_seconds<=7200:raise SafeError('CI polling limits are invalid')
        deadline=time.monotonic()+args.timeout_seconds; last=None
        while True:
            result=client.ci(args.sha,args.event,args.branch)
            if result!=last:print(json.dumps({'operation':'ci_update',**result}),flush=True);last=result
            if result['complete'] or time.monotonic()>=deadline:break
            time.sleep(args.poll_seconds)
    elif args.operation=='draft-pr':result=client.draft_pr(args.sha,args.title,body_file(args.body_file))
    elif args.operation=='ready-pr':result=client.ready_pr(args.pr_number,args.sha)
    elif args.operation=='merge-pr':result=client.merge_pr(args.pr_number,args.sha)
    elif args.operation=='create-release':result=client.create_release(args.sha,args.title,body_file(args.body_file))
    elif args.operation=='upload-asset':result=client.upload_asset(args.release_id,args.file,sha=args.sha,expected_sha256=args.sha256,expected_size=args.size)
    elif args.operation=='publish-release':result=client.publish_release(args.release_id,sha=args.sha,asset_id=args.asset_id,path=args.file,expected_sha256=args.sha256,expected_size=args.size)
    elif args.operation=='release-status':
        if args.release_id<=0:raise SafeError('A positive release ID is required')
        release=client.request('GET',client.endpoint(f'/releases/{args.release_id}'))
        result={**safe_release(release),'assets':[{key:item.get(key) for key in ('id','name','size','state','digest','browser_download_url')} for item in release.get('assets',[])]}
    if args.output_json:
        args.output_json.parent.mkdir(parents=True,exist_ok=True)
        args.output_json.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,indent=2),flush=True)
    if args.operation in ('ci-status','watch-ci') and result['complete'] and not result['success']:return 2
    if args.operation=='watch-ci' and not result['complete']:return 3
    return 0

if __name__=='__main__':
    try:raise SystemExit(main())
    except SafeError as error:
        print(json.dumps({'ok':False,'error':str(error)}));raise SystemExit(1)
    except Exception as error:
        print(json.dumps({'ok':False,'error':'Unexpected '+type(error).__name__+'; sensitive details suppressed'}));raise SystemExit(1)
