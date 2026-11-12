"""Trusted-side image/source verification before admitting any private input."""
import hashlib
import io
import json
from pathlib import PurePosixPath
import selectors
import subprocess
import tarfile
import time
from .sandbox import DOCKER,DOCKER_ENV,docker,SandboxError


def tree_digest(raw):
    files={};total=0
    with tarfile.open(fileobj=io.BytesIO(raw),mode='r:') as archive:
        for member in archive:
            path=PurePosixPath(member.name)
            if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0]!='power_sync':
                raise SandboxError('image_source_path_invalid')
            if member.isdir():
                continue
            name='/'.join(path.parts[1:])
            if not member.isfile() or not name or name in files or '__pycache__' in path.parts or name.endswith('.pyc') or member.size>8*1024*1024:
                raise SandboxError('image_source_invalid')
            total+=member.size
            if total>64*1024*1024 or len(files)>=20000:
                raise SandboxError('image_source_budget')
            stream=archive.extractfile(member)
            files[name]=stream.read(member.size+1)
            if len(files[name])!=member.size:
                raise SandboxError('image_source_truncated')
    digest=hashlib.sha256()
    for name,content in sorted(files.items()):
        encoded=name.encode();digest.update(len(encoded).to_bytes(8,'big')+encoded+len(content).to_bytes(8,'big')+content)
    return digest.hexdigest()


def verify_image(image,component):
    container=docker('create','--network=none','--read-only','--log-driver=none',image).decode().strip()
    process=None
    try:
        process=subprocess.Popen([DOCKER,'cp',container+':/opt/custom_components/power_sync','-'],stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,stdin=subprocess.DEVNULL,env=DOCKER_ENV,bufsize=0)
        monitor=selectors.DefaultSelector();monitor.register(process.stdout,selectors.EVENT_READ)
        deadline=time.monotonic()+20;raw=bytearray()
        try:
            while monitor.get_map():
                if time.monotonic()>=deadline:
                    raise SandboxError('image_verification_timeout')
                for event,_ in monitor.select(.2):
                    block=event.fileobj.read(65536)
                    if not block:
                        monitor.unregister(event.fileobj)
                    raw.extend(block)
                    if len(raw)>96*1024*1024:
                        raise SandboxError('image_source_budget')
            if process.wait(timeout=2) or tree_digest(raw)!=component:
                raise SandboxError('image_source_mismatch')
        finally:
            monitor.close()
    finally:
        if process and process.poll() is None:
            process.kill();process.wait()
        docker('rm','-f',container)
