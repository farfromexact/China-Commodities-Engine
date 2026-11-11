"""Publish only data from an isolated local checkout; retain failed pushes."""
import hashlib
from pathlib import Path
import shutil
import os
import stat
import subprocess

from .storage import read_json, write_json_atomic


def git(root, *args):
    r=subprocess.run(['git','-C',str(root),*args],capture_output=True,text=True,timeout=120)
    if r.returncode:
        # Avoid echoing remote URLs (which could contain credentials).
        raise RuntimeError(f'git {args[0]} failed (exit {r.returncode}); local checkout retained')
    return r.stdout.rstrip('\r\n')


def digest(root):
    result=hashlib.sha256()
    for path in sorted(Path(root).rglob('*')):
        if path.is_file():
            result.update(path.relative_to(root).as_posix().encode())
            result.update(hashlib.sha256(path.read_bytes()).digest())
    return result.hexdigest()


def remove_checkout(path):
    def writable(function, filename, error):
        os.chmod(filename,stat.S_IRWXU)
        function(filename)
    if Path(path).exists():
        shutil.rmtree(path,onexc=writable)


class Publisher:
    def __init__(self, root, cache, branch='main'):
        self.root,self.cache,self.branch=Path(root).resolve(),Path(cache).resolve(),branch
        self.outbox=self.cache/'outbox'
        self.journal=self.cache/'outbox.json'

    def clean(self):
        if git(self.root,'branch','--show-current')!=self.branch:
            raise RuntimeError('local checkout is not on configured publication branch')
        if git(self.root,'status','--porcelain'):
            raise RuntimeError('local checkout has uncommitted changes; nothing auto-committed')

    def sync(self):
        self.clean()
        git(self.root,'pull','--ff-only','origin',self.branch)

    def prepare(self, staged_data, base):
        if self.journal.exists() or self.outbox.exists():
            raise RuntimeError('publication outbox already exists; recover it first')
        self.clean()
        if git(self.root,'rev-parse','HEAD')!=base:
            raise RuntimeError('local HEAD changed during collection')
        self.cache.mkdir(parents=True,exist_ok=True)
        info=dict(root=str(self.root),base=base,phase='creating',data_digest=digest(staged_data))
        write_json_atomic(self.journal,info)
        git(self.root,'clone','--shared','--no-hardlinks',str(self.root),str(self.outbox))
        remote=git(self.root,'remote','get-url','origin')
        git(self.outbox,'remote','set-url','origin',remote)
        # Copy configured author to the isolated clone; never use a guessed identity.
        for key in ('user.name','user.email'):
            git(self.outbox,'config',key,git(self.root,'config',key))
        shutil.rmtree(self.outbox/'data')
        shutil.copytree(staged_data,self.outbox/'data')
        info['phase']='prepared'
        write_json_atomic(self.journal,info)

    def recover(self):
        if not self.journal.exists():
            if self.outbox.exists():
                raise RuntimeError('unowned outbox exists; manual inspection required')
            return False
        info=read_json(self.journal)
        if info['root']!=str(self.root):
            raise RuntimeError('outbox belongs to a different repository')
        if info['phase']=='pushed':
            # Cleanup may have been interrupted after deleting the clone. Verify
            # the published commit through the main checkout before finishing.
            self.sync()
            ancestor=subprocess.run(['git','-C',str(self.root),'merge-base','--is-ancestor',
                                     info['commit'],'HEAD'],capture_output=True)
            if ancestor.returncode:
                raise RuntimeError('published commit is not in synchronized history')
            remove_checkout(self.outbox)
            self.journal.unlink()
            return True
        if info['phase']=='creating':
            # No commit/push is allowed until the complete data tree is prepared.
            if (self.outbox/'.git').exists() and git(self.outbox,'rev-parse','HEAD')!=info['base']:
                raise RuntimeError('unexpected commit in incomplete outbox')
            remove_checkout(self.outbox)
            self.journal.unlink()
            return False
        head=git(self.outbox,'rev-parse','HEAD')
        if head==info['base']:
            if digest(self.outbox/'data')!=info['data_digest']:
                raise RuntimeError('prepared data changed; refusing publication')
            changed=git(self.outbox,'status','--porcelain','--untracked-files=all').splitlines()
            if any(not line[3:].startswith('data/') for line in changed):
                raise RuntimeError('outbox contains non-data changes')
            git(self.outbox,'add','--','data')
            if git(self.outbox,'diff','--cached','--name-only'):
                git(self.outbox,'commit','-m','chore(data): local Wind daily and catch-up')
            head=git(self.outbox,'rev-parse','HEAD')
        if head!=info['base']:
            if git(self.outbox,'rev-parse','HEAD^')!=info['base']:
                raise RuntimeError('unexpected outbox history')
            if any(not p.startswith('data/') for p in git(self.outbox,'diff','--name-only',info['base'],head).splitlines()):
                raise RuntimeError('outbox commit includes non-data changes')
            if git(self.outbox,'status','--porcelain'):
                raise RuntimeError('committed outbox has unexpected edits')
        git(self.outbox,'fetch','origin',self.branch)
        remote_head=git(self.outbox,'rev-parse','FETCH_HEAD')
        if remote_head not in {head,info['base']}:
            # A race never invokes force-push or silently resolves generated-data conflicts.
            ancestor=subprocess.run(['git','-C',str(self.outbox),'merge-base','--is-ancestor',head,remote_head],capture_output=True)
            if ancestor.returncode:
                raise RuntimeError('remote advanced independently; inspect retained outbox before retry')
        elif remote_head!=head:
            git(self.outbox,'push','origin',f'HEAD:refs/heads/{self.branch}')
        info.update(phase='pushed',commit=head)
        write_json_atomic(self.journal,info)
        self.sync()
        remove_checkout(self.outbox)
        self.journal.unlink()
        return True
