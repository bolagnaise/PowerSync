"""Serial, bounded fresh-job scheduler. No unattended capture backlog."""
import argparse
import hashlib
import json
from pathlib import Path
import signal
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'custom_components/power_sync'))
from diagnostics_access.bridge import private_file
from diagnostics_access import wire
from private_regression.controller import run
from private_regression.campaign_cli import PROFILES
from regression_runner.campaign import Campaign,HoldoutLedger


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);parser.add_argument('--once',action='store_true')
    args=parser.parse_args();config=private_file(args.config)
    if set(config)!={'pair','campaign','cohort_key','ledger','clients','interval_seconds'} or type(config['interval_seconds']) is not int or not 900<=config['interval_seconds']<=86400 or type(config['clients']) is not list or not 1<=len(config['clients'])<=16:
        raise ValueError('invalid_service_config')
    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,stop)
    signal.signal(signal.SIGINT,stop)
    while True:
        started=time.monotonic()
        pair=json.loads(Path(config['pair']).read_bytes())
        campaign=Campaign(**json.loads(Path(config['campaign']).read_bytes()))
        if sorted(pair.get('required_profiles',[]))!=sorted(PROFILES) or hashlib.sha256(wire.json_bytes(pair)).hexdigest()!=campaign.pair:
            raise ValueError('campaign_pair_changed')
        secret=wire.unb64(private_file(config['cohort_key'])['secret'],size=32)
        HoldoutLedger(config['ledger']).consume(campaign,secret)
        for path in config['clients']:
            try:
                result=run(private_file(path),pair)
                print(json.dumps({'event':'job_finished','purge':result['purge'],'status':result['summary']['status']}),flush=True)
            except KeyboardInterrupt:
                return
            except Exception:
                # Never log exception repr, raw inputs, config, keys or output.
                print(json.dumps({'event':'job_not_accepted'}),flush=True)
        if args.once:
            return
        remaining=max(1,config['interval_seconds']-(time.monotonic()-started))
        try:
            time.sleep(remaining)
        except KeyboardInterrupt:
            return


if __name__=='__main__':
    main()
