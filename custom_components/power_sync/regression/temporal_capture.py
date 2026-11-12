"""Expendable, bounded observation of actual reserve-restoration calls."""
from contextvars import ContextVar
from functools import wraps
import math
import time

_span = ContextVar('powersync_private_temporal_span', default=None)
METHODS = {'idle','self_consumption','charge','export',None}


def snapshot(owner, monitoring=False):
    value = {'reserve':getattr(owner,'_pre_idle_backup_reserve',None),
        'held_reserve':getattr(owner,'_idle_hold_reserve',None), 'last_action':getattr(owner,'_last_executed_action',None),
        'ev_preserve':getattr(owner,'_scheduled_ev_no_discharge_active',False), 'monitoring':monitoring,
        'configured_reserve':getattr(getattr(owner,'_config',None),'backup_reserve',None)}
    for key in ('reserve','held_reserve','configured_reserve'):
        number = value[key]
        if number is not None and (type(number) not in (int,float) or not math.isfinite(number) or not 0 <= number <= 100):
            raise ValueError('unsupported_temporal_state')
    if value['last_action'] not in METHODS or type(value['ev_preserve']) is not bool:
        raise ValueError('unsupported_temporal_state')
    return value


class TemporalCapture:
    def __init__(self):
        self.sequences=[]
        self.unsupported=0
        self.closed=False

    def begin(self, owner):
        if self.closed or len(self.sequences)>=32:
            self.unsupported+=1
            return None
        return {'initial':snapshot(owner),'response':[],'started':time.monotonic()}

    def finish(self, owner, ticket, result):
        if self.closed or type(result) is not bool:
            self.unsupported+=1
            return
        elapsed=int(time.monotonic()-ticket['started'])
        if not 0 <= elapsed <= 90:
            self.unsupported+=1
            return
        initial=ticket['initial']
        state=snapshot(owner,initial['monitoring'])
        events=[{'at':0,'type':'response','value':value} for value in ticket['response']]
        events.append({'at':0,'type':'call','method':'restore'})
        # Acknowledgement is a boundary response, never physical-effect proof.
        commands=[{'at':0,'generation':0,'operation':'set_backup_reserve','reserve':initial['reserve']}] if ticket['response'] else []
        self.sequences.append({'initial':initial,'events':events,'expected':{'commands':commands,
            'returns':[{'at':0,'generation':0,'value':result}], 'state':state},'observed_duration_seconds':elapsed})

    def export(self):
        return {'profile':'reserve-recovery-temporal-v1','sequences':self.sequences.copy(),
                'complete':bool(self.sequences) and self.unsupported==0,'unsupported':self.unsupported}

    def close(self):
        self.closed=True;self.sequences.clear()


def monitoring(owner, value):
    span=_span.get()
    if span and span[0] is owner and type(value) is bool:
        span[2]['initial']['monitoring']=value


def boundary(owner, value):
    span=_span.get()
    if span:
        recorder=span[1]
        try:
            if span[0] is not owner or value is not None and type(value) is not bool:
                raise ValueError
            span[2]['response'].append(value)
        except Exception:
            recorder.unsupported+=1


def boundary_exception(owner):
    span=_span.get()
    if span and span[0] is owner:
        span[2]['response'].append('exception')


def observe_reserve_restore(function):
    @wraps(function)
    async def observe(owner,*args,**kwargs):
        recorder=getattr(owner,'_regression_temporal_recorder',None)
        if recorder is None:
            return await function(owner,*args,**kwargs)
        ticket=None
        try:
            if kwargs.get('bypass_monitoring') or len(args)>2 and args[2]:
                recorder.unsupported+=1
            else:
                ticket=recorder.begin(owner)
        except Exception:
            recorder.unsupported+=1
        token=_span.set((owner,recorder,ticket)) if ticket is not None else None
        try:
            result=await function(owner,*args,**kwargs)
            if ticket is not None:
                try:
                    recorder.finish(owner,ticket,result)
                except Exception:
                    recorder.unsupported+=1
            return result
        except BaseException:
            recorder.unsupported+=1
            raise
        finally:
            if token is not None:
                _span.reset(token)
    return observe
