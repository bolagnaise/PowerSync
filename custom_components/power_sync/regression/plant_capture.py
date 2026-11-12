"""Fresh cached meter samples. No history, refresh, template or device calls."""
import math
import time
from datetime import datetime

ROLES = ('battery_level','battery_power','solar_power','home_load','grid_power','grid_status')


class PlantCapture:
    def __init__(self,hass,entry_id):
        self.hass=hass;self.entities={};self.rows=[];self.unsupported=0
        try:
            from homeassistant.helpers import entity_registry as registry_module
            registry=hass.data.get(registry_module.DATA_REGISTRY)
            if registry is None or len(registry.entities)>20000:
                return
            for entity in registry.entities.values():
                if entity.config_entry_id!=entry_id or entity.disabled_by:
                    continue
                for role in ROLES:
                    if entity.unique_id==entry_id+'_'+role:
                        self.entities[role]=entity.entity_id
        except Exception:
            self.entities={}

    @property
    def available(self):
        return set(self.entities)==set(ROLES)

    def sample(self):
        if not self.available or len(self.rows)>=32:
            return
        try:
            now=time.time();row={'at':int(now)}
            for role,entity_id in self.entities.items():
                state=self.hass.states.get(entity_id)
                reported=getattr(state,'last_reported',None) or getattr(state,'last_updated',None)
                if state is None or not isinstance(reported,datetime) or not 0<=now-reported.timestamp()<=30:
                    raise ValueError
                if role=='grid_status':
                    value=state.state.strip().lower()
                    if value not in ('active','systemgridconnected','inactive','islanded','off-grid','systemislandedactive'):
                        raise ValueError
                    row['available']=value in ('active','systemgridconnected')
                    continue
                number=float(state.state)
                if not math.isfinite(number):
                    raise ValueError
                unit=state.attributes.get('unit_of_measurement')
                if role=='battery_level':
                    if unit!='%' or not 0<=number<=100:
                        raise ValueError
                    row['soc']=number/100
                else:
                    if unit not in ('W','kW') or abs(number)>100000:
                        raise ValueError
                    number=number/1000 if unit=='W' else number
                    row[{'battery_power':'power','solar_power':'solar','home_load':'load','grid_power':'grid'}[role]]= -number if role=='battery_power' else number
            if min(row['solar'],row['load'])<0 or abs(row['grid']+row['solar']-row['load']-row['power'])>.25:
                raise ValueError
            if not self.rows or row['at']>self.rows[-1]['at']:
                self.rows.append(row)
        except (ValueError,TypeError,AttributeError,OverflowError):
            self.unsupported+=1

    def export(self):
        return {'profile':'measured-battery-v1','rows':self.rows.copy(),'unsupported':self.unsupported,
                'soc_resolution':.001,'power_sign':'positive_into_battery'}

    def close(self):
        self.rows.clear()
