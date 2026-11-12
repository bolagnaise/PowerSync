"""Public synthetic fault injection against real immutable candidate images."""
import argparse
import ast
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
import secrets
import hashlib
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'custom_components/power_sync'))
from replay_optimizer import capture_synthetic
from regression_runner import trusted_contract as contract
from diagnostics_access import wire
from diagnostics_access.private_capture import encrypt,aad
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PrivateFormat,PublicFormat,NoEncryption
from private_regression.sandbox import purge


def mutate(root,mode):
    path=root/'optimization/battery_optimizer.py'
    tree=ast.parse(path.read_text());cls=next(node for node in tree.body if isinstance(node,ast.ClassDef) and node.name=='BatteryOptimizer')
    function=next(node for node in cls.body if isinstance(node,ast.FunctionDef) and node.name=='optimize')
    if mode=='nondeterministic':
        class Returns(ast.NodeTransformer):
            def visit_FunctionDef(self,node):
                return node
            def visit_AsyncFunctionDef(self,node):
                return node
            def visit_Return(self,node):
                value=ast.Assign(targets=[ast.Name(id='_fixture_result',ctx=ast.Store())],value=node.value or ast.Constant(None))
                change=ast.parse('if hasattr(_fixture_result,"schedule") and _fixture_result.schedule.actions:\n import time\n _fixture_result.schedule.actions[0].power_w=time.time_ns()/1e9\nreturn _fixture_result').body
                return [value,*change]
        transformer=Returns();function.body=[item for node in function.body for item in (lambda value:value if isinstance(value,list) else [value])(transformer.visit(node))]
    else:
        payload={'crash':'import os\nos._exit(23)', 'hang':'import time\ntime.sleep(30)',
                 'nan':'import os\nos.write(1,b"NaN")\nos._exit(0)'}[mode]
        function.body=ast.parse(payload).body+function.body
    path.write_text(ast.unparse(ast.fix_missing_locations(tree))+'\n')


def main(base,output):
    metadata=json.loads((base/'artifact.json').read_bytes());root=base/'custom_components/power_sync'
    bundle=capture_synthetic(root,'flat-tariff','AUD')
    from power_sync.regression.environment import binding
    bundle['source']=binding(root,sys.modules['power_sync.optimization.battery_optimizer'])
    bundle['digest']=contract.digest({key:value for key,value in bundle.items() if key!='digest'})
    output.mkdir(parents=True,exist_ok=False)
    for mode in ('crash','hang','nan','nondeterministic'):
        source=output/(mode+'-source');candidate=output/(mode+'-artifact')
        shutil.copytree(root,source/'custom_components/power_sync',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        mutate(source/'custom_components/power_sync',mode)
        with (output/(mode+'-build.log')).open('wb') as log:
            subprocess.run([sys.executable,str(Path(__file__).with_name('build.py')),'--source',str(source),'--output',str(candidate),'--base',metadata['base']],stdout=log,stderr=subprocess.STDOUT,check=True)
        changed=json.loads((candidate/'artifact.json').read_bytes())
        pair={'baseline':{'root':str(root),'image':metadata['image'],'component':metadata['component']},
              'candidate':{'root':str(candidate/'custom_components/power_sync'),'image':changed['image'],'component':changed['component']},
              'runtime':metadata['runtime'],'source':bundle['source'],'scorer':changed['scorer'],'schema':changed['schema'],'currency':'AUD'}
        digest=hashlib.sha256(wire.json_bytes(pair)).hexdigest();context={'root':'a'*64,'grant':'b'*32,'job':secrets.token_hex(32),'pair':digest}
        private=X25519PrivateKey.generate()
        encrypted=encrypt(contract.canonical({'cycles':[{'attempts':[bundle]}]}),wire.b64(private.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)),aad(**context))
        request={'encrypted':encrypted,'decryption_key':wire.b64(private.private_bytes(Encoding.Raw,PrivateFormat.Raw,NoEncryption())),
                 'pair':pair,'context':context,'deadline':time.time()+120}
        process=subprocess.run([sys.executable,str(Path(__file__).with_name('job.py'))],input=json.dumps(request).encode(),stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=130)
        assert process.returncode==0,'fault disappeared without a receipt: '+mode
        report=json.loads(process.stdout)
        assert report['status']=='fail',(mode,report)
        assert purge(context['job'])
        print(json.dumps({'fixture':mode,'status':report['status'],'purge':'confirmed'}),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--artifact',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();main(args.artifact,args.output)
