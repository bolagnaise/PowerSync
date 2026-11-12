"""Freeze/aggregate advisory fleet campaigns from signed coordination metadata."""
import argparse
from dataclasses import asdict
import hashlib
import hmac
import json
import os
from pathlib import Path
import time
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'custom_components/power_sync'))
from diagnostics_access.bridge import Bridge,private_file
from diagnostics_access.provision import write_new
from diagnostics_access import wire
from regression_runner.campaign import Campaign,HoldoutLedger,holdout,seven_day
from private_regression.fingerprints import scorer_digest,schema_digest

PROFILES=['solver_v1','reserve-recovery-temporal-v1','calibrated-plant-v1']


def freeze(pair,key,*,minimum=5,tolerance=.01):
    if sorted(pair.get('required_profiles',[]))!=sorted(PROFILES) or pair.get('terminal_price') is None or pair['scorer']!=scorer_digest() or pair['schema']!=schema_digest():
        raise ValueError('complete_campaign_profiles_required')
    digest=hashlib.sha256(wire.json_bytes(pair)).hexdigest()
    cohort=hashlib.sha256(key).hexdigest()
    return Campaign(pair['baseline']['component'],pair['candidate']['component'],pair['runtime'],pair['scorer'],pair['schema'],cohort,tolerance,minimum,pair=digest,currency=pair['currency'])


def collect(config,campaign,secret):
    client=Bridge(config,config['profile'])
    rows=[];reservations=[]
    try:
        client.connect()
        if client.kind!='optimizer_contribution':
            raise ValueError('contribution_grant_required')
        offset=0
        installation=hmac.digest(secret,('logical-installation:'+config['root']).encode(),'sha256').hex()
        while True:
            page=client.command({'type':'job_list','family':client.family,'binding':client.binding,'offset':offset})
            for metadata in page['records']:
                if metadata['pair']!=campaign.pair:
                    continue
                identity=hashlib.sha256((config['root']+':'+config['grant']+':'+metadata['id']).encode()).hexdigest()
                reservations.append({'job':identity,'installation':installation,'created_at':metadata['created_at'],'campaign':campaign.digest})
                summary=metadata.get('summary')
                if metadata['state']=='purged' and summary and metadata.get('completed_at'):
                    rows.append({'job':identity,'installation':installation,'completed_at':metadata['completed_at'],
                        'campaign':campaign.digest,'pair':campaign.pair,'scorer':campaign.scorer,'schema':campaign.schema,
                        'purge':'confirmed','cohort':holdout(secret,campaign,installation),**summary})
            offset=page['next_offset']
            if offset is None:
                break
    finally:
        client.close()
    return rows,reservations


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=('new-key','freeze','report'))
    parser.add_argument('--key',required=True)
    parser.add_argument('--pair')
    parser.add_argument('--campaign')
    parser.add_argument('--output')
    parser.add_argument('--config',action='append',default=[])
    parser.add_argument('--ledger')
    parser.add_argument('--start',type=int)
    parser.add_argument('--minimum',type=int,default=5)
    parser.add_argument('--tolerance',type=float,default=.01)
    args=parser.parse_args()
    if args.action=='new-key':
        write_new(args.key,{'secret':wire.b64(os.urandom(32))})
        print(json.dumps({'status':'created'}));return
    secret=wire.unb64(private_file(args.key)['secret'],size=32)
    if args.action=='freeze':
        if not args.pair or not args.output or not args.ledger:
            parser.error('freeze requires --pair --output and --ledger')
        campaign=freeze(json.loads(Path(args.pair).read_bytes()),secret,minimum=args.minimum,tolerance=args.tolerance)
        HoldoutLedger(args.ledger).consume(campaign,secret)
        write_new(args.output,asdict(campaign))
        print(json.dumps({'campaign':campaign.digest,'status':'frozen_advisory'}));return
    if not args.campaign or not args.ledger or args.start is None or not args.config:
        parser.error('report requires --campaign --ledger --start and enrolled --config files')
    campaign=Campaign(**json.loads(Path(args.campaign).read_bytes()))
    rows=[];reservations=[];unavailable=0
    for path in args.config:
        try:
            found,pending=collect(private_file(path),campaign,secret)
            rows.extend(found);reservations.extend(pending)
        except Exception:
            unavailable+=1
    report=seven_day(campaign,rows,start=args.start,now=int(time.time()),reservations=reservations,
        holdout_secret=secret,ledger=HoldoutLedger(args.ledger))
    if unavailable and report['status']=='pass':
        report['status']='pending'
    report.update(unavailable_grants=unavailable,release_policy='advisory')
    if args.output:
        write_new(args.output,report)
    print(json.dumps(report,allow_nan=False))


if __name__=='__main__':
    main()
