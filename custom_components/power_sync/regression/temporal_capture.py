"""Expendable, bounded observation of actual reserve-restoration calls."""
from contextvars import ContextVar
from functools import wraps
import math
import time

_span = ContextVar('powersync_private_temporal_span', default=None)
METHODS = {'idle','self_consumption','charge','export',None}


def snapshot(owner, monitoring=None):
    if monitoring is None:
        monitoring=owner._monitoring_mode_active()
    hass_data=getattr(getattr(owner,'hass',None),'data',{})
    live=hass_data.get('power_sync',{}).get(getattr(owner,'entry_id',None),{}).get('scheduled_ev_preserve_state',{})
    value = {'reserve':getattr(owner,'_pre_idle_backup_reserve',None),
        'held_reserve':getattr(owner,'_idle_hold_reserve',None), 'last_action':getattr(owner,'_last_executed_action',None),
        'ev_preserve':getattr(owner,'_scheduled_ev_no_discharge_active',False), 'scheduled_ev_state':bool(live.get('active',False)), 'monitoring':monitoring,
        'configured_reserve':getattr(getattr(owner,'_config',None),'backup_reserve',None)}
    for key in ('reserve','held_reserve','configured_reserve'):
        number = value[key]
        if number is not None and (type(number) not in (int,float) or not math.isfinite(number) or not 0 <= number <= 100):
            raise ValueError('unsupported_temporal_state')
    if value['last_action'] not in METHODS or type(value['ev_preserve']) is not bool or type(value['monitoring']) is not bool:
        raise ValueError('unsupported_temporal_state')
    return value


class TemporalCapture:
    def __init__(self):
        self.sequences=[]
        self.unsupported=0
        self.closed=False
        self.pending={}

    def begin(self, owner):
        if self.closed or len(self.sequences)>=32:
            self.unsupported+=1
            return None
        ticket={'initial':snapshot(owner),'response':[],'commands':[],'started':time.monotonic()}
        self.pending[id(ticket)]=ticket
        return ticket

    def finish(self, owner, ticket, result):
        self.pending.pop(id(ticket),None)
        if self.closed or type(result) is not bool:
            self.unsupported+=1
            return
        elapsed=int(time.monotonic()-ticket['started'])
        if not 0 <= elapsed <= 90:
            self.unsupported+=1
            return
        initial=ticket['initial']
        state=snapshot(owner)
        events=[{'at':0,'type':'response','value':value} for value in ticket['response']]
        events.append({'at':0,'type':'call','method':'restore'})
        # Acknowledgement is a boundary response, never physical-effect proof.
        commands=ticket['commands'].copy()
        self.sequences.append({'initial':initial,'events':events,'expected':{'commands':commands,
            'returns':[{'at':0,'generation':0,'value':result}], 'state':state},'observed_duration_seconds':elapsed})

    def export(self):
        return {'profile':'reserve-recovery-temporal-v1','sequences':self.sequences.copy(),
                'complete':bool(self.sequences) and self.unsupported==0,'unsupported':self.unsupported}

    def close(self):
        self.closed=True;self.sequences.clear()
        for ticket in tuple(self.pending.values()):
            ticket.clear()
        self.pending.clear()


def monitoring(owner, value):
    span=_span.get()
    if span and not span[1].closed and span[0] is owner and type(value) is bool:
        span[2]['initial']['monitoring']=value


def boundary(owner, value):
    span=_span.get()
    if span and not span[1].closed:
        recorder=span[1]
        try:
            if span[0] is not owner or value is not None and type(value) is not bool:
                raise ValueError
            if len(span[2]['response'])>=32:
                raise ValueError
            span[2]['response'].append(value)
        except Exception:
            recorder.unsupported+=1


def boundary_exception(owner):
    span=_span.get()
    if span and not span[1].closed and span[0] is owner:
        if len(span[2]['response'])<32:
            span[2]['response'].append('exception')
        else:
            span[1].unsupported+=1


def command_argument(owner,value):
    span=_span.get()
    if span and not span[1].closed and span[0] is owner:
        try:
            if type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=100 or len(span[2]['commands'])>=32:
                raise ValueError
            span[2]['commands'].append({'at':int(time.monotonic()-span[2]['started']),'generation':0,'operation':'set_backup_reserve','reserve':value})
        except Exception:
            span[1].unsupported+=1


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
