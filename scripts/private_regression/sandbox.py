"""Fail-closed Docker boundary and independent wall-clock watchdog/reaper."""
from __future__ import annotations
import argparse
import ctypes
import math
import json
import os
from pathlib import Path
import re
import resource
import selectors
import secrets
import signal
import shutil
import subprocess
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

LABEL = "cc.powersync.private-replay"
PIDFD_SIGNAL_PROCESS_GROUP = 4  # Linux >=6.9, uapi/linux/pidfd.h
MAX_BYTES = 4*1024*1024
DOCKER = shutil.which("docker") or "/usr/bin/docker"
DOCKER_ENV = {'PATH':os.defpath, **{key:os.environ[key] for key in ('DOCKER_HOST','DOCKER_CONTEXT','DOCKER_CONFIG','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH') if key in os.environ}}


class SandboxError(RuntimeError):
    pass


class CandidateFailure(SandboxError):
    pass


def parse_output(raw):
    def number(text):
        value=float(text)
        if not math.isfinite(value):
            raise ValueError
        return value
    try:
        value=json.loads(raw,parse_float=number,parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        pending=[(value,0)];count=0
        while pending:
            item,depth=pending.pop();count+=1
            if depth>24 or count>100000:
                raise ValueError
            if type(item) in (int,float) and (not math.isfinite(item) or abs(item)>2**53-1):
                raise ValueError
            if type(item) is dict:
                pending.extend((child,depth+1) for child in item.values())
            elif type(item) is list:
                pending.extend((child,depth+1) for child in item)
        return value
    except (ValueError,UnicodeError,RecursionError,OverflowError,MemoryError):
        raise CandidateFailure('candidate_output_invalid') from None


def docker(*arguments):
    result = subprocess.run([DOCKER, *arguments], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, timeout=20, check=False,env=DOCKER_ENV)
    if result.returncode:
        raise SandboxError("sandbox_operation_failed")
    return result.stdout


def candidate_started(name):
    """Infrastructure failures cannot become evidence against a candidate."""
    state = json.loads(docker('inspect', name))[0]['State']
    if state.get('Error') or not state.get('StartedAt', '').startswith(('19', '20')):
        raise SandboxError('sandbox_candidate_not_started')
    return state


def daemon_identity():
    value=docker('info','--format','{{.ID}}').decode().strip()
    if not re.fullmatch(r'[A-Za-z0-9:_-]{16,128}',value):
        raise SandboxError('docker_identity_unavailable')
    return value


def kill_group(descriptor):
    """A held pidfd targets the original group even after its leader exits."""
    try:
        signal.pidfd_send_signal(descriptor,signal.SIGKILL,None,PIDFD_SIGNAL_PROCESS_GROUP)
    except ProcessLookupError:
        pass


def proc_namespace():
    if int(Path('/proc/self/stat').read_text().split(' ',1)[0])!=os.getpid():
        raise SandboxError('private_proc_namespace_mismatch')
    return os.readlink('/proc/self/ns/pid')


def no_swap():
    proc_namespace()
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if ctypes.CDLL(None).prctl(4, 0, 0, 0, 0) != 0:
        raise SandboxError("private_process_dumpable")
    if sys.platform != "linux" or len(Path("/proc/swaps").read_text().strip().splitlines()) != 1:
        raise SandboxError("private_host_swap_not_disabled")
    if Path('/proc/sys/kernel/core_pattern').read_text().lstrip().startswith('|'):
        raise SandboxError('private_host_core_handler_enabled')


def private_argv(arguments,job):
    """Only our disposable scorer and its named Docker attachment carry data."""
    if len(arguments)>=3 and arguments[-2:]==['--private-job-id',job] and any(Path(value).name=='job.py' for value in arguments):
        return True
    return any(value.startswith('ppr-'+job+'-') for value in arguments) and any(
        arguments[index:index+3]==['start','-a','-i'] for index in range(len(arguments)-2))


def private_processes_gone(job):
    proc_namespace()
    # This backstop also covers a stopped/dead watchdog and an already removed
    # container. Open pidfds and recheck argv before signalling; never kill by
    # a reusable numeric PID. No private bytes enter this process inventory.
    for path in Path('/proc').iterdir():
        if not path.name.isdecimal() or int(path.name)==os.getpid():continue
        try:
            arguments=(path/'cmdline').read_bytes().rstrip(b'\0').decode().split('\0')
            if not private_argv(arguments,job):continue
            descriptor=os.pidfd_open(int(path.name))
        except (FileNotFoundError,ProcessLookupError,PermissionError,UnicodeError):continue
        try:
            try:current=(path/'cmdline').read_bytes().rstrip(b'\0').decode().split('\0')
            except (FileNotFoundError,ProcessLookupError):continue
            if not private_argv(current,job):continue
            try:signal.pidfd_send_signal(descriptor,signal.SIGKILL)
            except ProcessLookupError:continue
            monitor=selectors.DefaultSelector()
            try:
                monitor.register(descriptor,selectors.EVENT_READ)
                if not monitor.select(5):return False
            finally:monitor.close()
        finally:os.close(descriptor)
    return True


def purge(job,daemon_id=None):
    if not re.fullmatch(r'[a-f0-9]{32,64}',job):
        raise SandboxError('invalid_cleanup_job')
    if daemon_id is not None and daemon_identity()!=daemon_id:
        raise SandboxError('cleanup_daemon_changed')
    if not private_processes_gone(job):return False
    query=('ps','-aq','--no-trunc','--filter','label='+LABEL+'='+job)
    for attempt in range(3):
        names=docker(*query).decode().split()
        if not names:return True
        for name in names:
            try:docker('rm','-f',name)
            except SandboxError:
                if name in docker(*query).decode().split():
                    if attempt==2:raise
        if attempt<2:time.sleep(.1)
    return not docker(*query).strip()


def reap(daemon_id=None):
    proc_namespace()
    if daemon_id is not None and daemon_identity()!=daemon_id:
        raise SandboxError('cleanup_daemon_changed')
    ids = docker("ps", "-aq", "--filter", "label=" + LABEL).decode().split()
    for identity in ids:
        try:info = json.loads(docker("inspect", identity))[0]
        except SandboxError:
            if identity in docker('ps','-aq','--filter','label='+LABEL).decode().split():raise
            continue
        deadline = info["Config"]["Labels"].get(LABEL + ".deadline", "0")
        labels=info['Config']['Labels']
        if labels.get(LABEL+'.pid_ns')!=os.readlink('/proc/self/ns/pid'):
            continue
        pid=labels.get(LABEL+'.owner_pid','')
        alive=False
        if pid.isdecimal():
            try:
                actual=Path('/proc')/pid/'stat'
                alive=actual.read_text().rsplit(')',1)[1].split()[19]==labels.get(LABEL+'.owner_start')
            except (OSError,IndexError):
                pass
        if not alive or not deadline.isdecimal() or int(deadline) <= time.time():
            job=labels.get(LABEL,'')
            if re.fullmatch(r'[a-f0-9]{32,64}',job):purge(job,daemon_id)


def watchdog(job, deadline, parent_fd,daemon_id):
    # An independent process survives controller death and uses the real clock.
    monitor = selectors.DefaultSelector()
    monitor.register(parent_fd, selectors.EVENT_READ)
    while time.time() < deadline:
        try:
            no_swap()
        except SandboxError:
            break
        if monitor.select(max(0, min(1, deadline-time.time()))):
            break
    try:
        kill_group(parent_fd)
        if not monitor.select(5):
            return
    finally:
        monitor.close()
    # Process memory dies before fallible daemon cleanup; host reaping is
    # still required if bounded retries cannot confirm container deletion.
    for attempt in range(3):
        try:
            if purge(job,daemon_id):
                break
        except Exception:
            time.sleep(.2)


class Sandbox:
    def __init__(self, job, deadline,daemon_id=None):
        if not re.fullmatch(r"[a-f0-9]{32,64}", job) or not time.time() < deadline <= time.time()+1800:
            raise SandboxError("invalid_job_lease")
        no_swap()
        if os.getpgrp()!=os.getpid():
            raise SandboxError('private_process_group_required')
        self.daemon_id=daemon_identity()
        if daemon_id is not None and self.daemon_id!=daemon_id:
            raise SandboxError('cleanup_daemon_changed')
        reap(self.daemon_id)
        self.job, self.deadline = job, deadline
        self.cpu = min(os.sched_getaffinity(0))
        parent_fd = os.pidfd_open(os.getpid())
        try:
            # Refuse unsupported kernels before any private input reaches an
            # image. The pidfd group flag avoids numeric process-group reuse.
            signal.pidfd_send_signal(parent_fd,0,None,PIDFD_SIGNAL_PROCESS_GROUP)
            self.watchdog = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "watch", job, str(int(deadline)), str(parent_fd),self.daemon_id],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, pass_fds=(parent_fd,))
        finally:
            os.close(parent_fd)

    def run(self, image, component, bundle, *, mode='solver'):
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image) or not re.fullmatch(r"[a-f0-9]{64}", component):
            raise SandboxError("immutable_image_required")
        if time.time() >= self.deadline:
            raise SandboxError("job_expired")
        if self.watchdog.poll() is not None:
            raise SandboxError("watchdog_unavailable")
        if self.deadline-time.time()<75:
            raise SandboxError('insufficient_run_and_cleanup_budget')
        # Captured reference plans and outcomes stay outside candidate memory.
        from regression_runner.trusted_contract import validate_bundle
        from regression_runner.trusted_contract import digest, canonical
        if mode == 'solver':
            validate_bundle(bundle)
            private = {key: bundle[key] for key in ("schema_version", "layer", "source", "decision_time", "decision_timezone", "currency", "provided_inputs", "inputs", "state", "coverage")}
            private["outcome"] = "exception"
            private["result"] = {"schedule": [], "feasible": None, "solver": None, "exception": "other"}
            private["digest"] = digest(private)
            raw = canonical({"component_sha256": component, "bundle": private})
        elif mode == 'temporal':
            raw = canonical({'component_sha256':component,'mode':'temporal','sequence':{'initial':bundle['initial'],'events':bundle['events']}})
        elif mode == 'health':
            raw = canonical({'component_sha256':component,'mode':'health'})
        else:
            raise SandboxError('unsupported_private_profile')
        if len(raw) > MAX_BYTES:
            raise SandboxError("input_limit")
        name='ppr-'+self.job+'-'+secrets.token_hex(8)
        process = None
        try:
            docker("create", '--name='+name,"--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--memory=512m", "--memory-swap=512m", "--cpus=1", "--cpuset-cpus="+str(self.cpu), "--pids-limit=32", "--ulimit=core=0:0",
            "--ulimit=nofile=64:64", "--log-driver=none", "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=16m",
            "--label="+LABEL+"="+self.job, "--label="+LABEL+".deadline="+str(int(self.deadline)),
            '--label='+LABEL+'.owner_pid='+str(os.getpid()),
            '--label='+LABEL+'.pid_ns='+os.readlink('/proc/self/ns/pid'),
            '--label='+LABEL+'.owner_start='+Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19], '-i', image)
            info = json.loads(docker("inspect", name))[0]
            host = info["HostConfig"]
            # Tower has no swap and no per-container swap controller. This
            # profile requires the host-wide no-swap check and pidfd watchdog;
            # a newly enabled swap device aborts the private process as well.
            no_swap()
            if (info["Image"] != image or not host["ReadonlyRootfs"] or host["NetworkMode"] != "none" or host["Binds"]
                or host["LogConfig"]["Type"] != "none" or host["Memory"] != 512*1024*1024 or host["MemorySwap"] not in (-1, host["Memory"])
                or host["PidsLimit"] != 32 or host["NanoCpus"] != 1000000000 or host["Privileged"]
                or host['CpusetCpus'] != str(self.cpu)
                or "ALL" not in host["CapDrop"] or not any(value.startswith("no-new-privileges") for value in host["SecurityOpt"])
                or info["Config"]["User"] != "65534:65534" or any(mount["Type"] != "tmpfs" or mount["Destination"] != "/tmp" for mount in info["Mounts"])):
                raise SandboxError("sandbox_profile_not_enforced")
            if self.deadline-time.time()<75:
                raise SandboxError('sandbox_startup_budget_exhausted')
            run_deadline=time.time()+10
            process = subprocess.Popen([DOCKER, "start", "-a", "-i", name], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=DOCKER_ENV, bufsize=0)
            selector = selectors.DefaultSelector()
            os.set_blocking(process.stdin.fileno(), False)
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE)
            selector.register(process.stdout, selectors.EVENT_READ)
            offset, output = 0, bytearray()
            try:
                while selector.get_map():
                    if self.watchdog.poll() is not None:
                        raise SandboxError("watchdog_unavailable")
                    if time.time() >= run_deadline:
                        candidate_started(name)
                        raise CandidateFailure("candidate_timeout")
                    for event, _ in selector.select(timeout=.2):
                        if event.fileobj is process.stdin:
                            try:
                                written = os.write(process.stdin.fileno(), raw[offset:offset+65536]); offset += written
                            except BrokenPipeError:
                                candidate_started(name)
                                raise CandidateFailure("candidate_input_refused") from None
                            if offset == len(raw):
                                selector.unregister(process.stdin); process.stdin.close()
                        else:
                            part = os.read(process.stdout.fileno(), 65536)
                            if not part:
                                selector.unregister(process.stdout)
                            output.extend(part)
                            if len(output) > MAX_BYTES:
                                raise CandidateFailure("candidate_output_limit")
                try:
                    code = process.wait(timeout=max(.1, run_deadline-time.time()))
                except subprocess.TimeoutExpired:
                    candidate_started(name)
                    raise CandidateFailure("candidate_timeout") from None
                if code:
                    candidate_started(name)
                    raise CandidateFailure("candidate_exit")
                value = parse_output(output)
                if type(value) is not dict:
                    raise CandidateFailure("candidate_output_invalid")
                return value
            finally:
                selector.close()
        finally:
            if process and process.poll() is None:
                process.kill(); process.wait()
            try:docker("rm", "-f", name)
            except SandboxError:
                if name in docker('ps','-aq','--filter','name=^/'+name+'$','--format','{{.Names}}').decode().split():raise

    def close(self):
        confirmed = purge(self.job,self.daemon_id)
        if confirmed:
            self.watchdog.terminate()
            self.watchdog.wait(timeout=5)
        return confirmed


if __name__ == "__main__":
    if len(sys.argv)==3 and sys.argv[1]=='reap':
        reap(sys.argv[2])
    elif len(sys.argv) == 6 and sys.argv[1] == "watch":
        watchdog(sys.argv[2], int(sys.argv[3]), int(sys.argv[4]),sys.argv[5])
    elif len(sys.argv)==6 and sys.argv[1]=='probe':
        sandbox=Sandbox(sys.argv[2],time.time()+120,sys.argv[5])
        try:result=sandbox.run(sys.argv[3],sys.argv[4],None,mode='health')
        finally:
            if not sandbox.close():raise SandboxError('probe_cleanup_unconfirmed')
        print(json.dumps(result,allow_nan=False))
    else:
        raise SystemExit(2)
