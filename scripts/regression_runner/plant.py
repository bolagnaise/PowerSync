"""Independent action-driven battery plant and bounded calibration.

Power is positive into the battery. Exogenous load/solar are held common across
arms; each arm's actions produce its own energy, import/export and bill.
"""
from dataclasses import dataclass, replace
import math


class PlantError(ValueError):
    pass


def finite(*values):
    try:
        valid = all(type(value) in (int, float) and math.isfinite(value) for value in values)
    except OverflowError:
        valid = False
    if not valid:
        raise PlantError("invalid_number")


@dataclass(frozen=True)
class Parameters:
    capacity_kwh: float
    max_charge_kw: float
    max_discharge_kw: float
    charge_efficiency: float
    discharge_efficiency: float
    reserve: float
    delay_steps: int = 0
    uncertainty_kwh: float = 0

    def __post_init__(self):
        finite(self.capacity_kwh, self.max_charge_kw, self.max_discharge_kw,
               self.charge_efficiency, self.discharge_efficiency, self.reserve, self.uncertainty_kwh)
        if not (0 < self.capacity_kwh <= 1000 and 0 < self.max_charge_kw <= 1000 and 0 < self.max_discharge_kw <= 1000
                and 0 < self.charge_efficiency <= 1 and 0 < self.discharge_efficiency <= 1
                and 0 <= self.reserve <= 1 and self.uncertainty_kwh >= 0
                and type(self.delay_steps) is int and 0 <= self.delay_steps <= 60):
            raise PlantError("invalid_parameters")


class Plant:
    def __init__(self, parameters, initial_soc):
        finite(initial_soc)
        if not parameters.reserve <= initial_soc <= 1:
            raise PlantError("initial_soc_out_of_range")
        self.parameters = parameters
        self.energy = initial_soc * parameters.capacity_kwh
        self.pending = [0.0] * parameters.delay_steps
        self.cost = 0.0
        self.initial_energy = self.energy
        self.balance = dict(imported=0., exported=0., solar=0., load=0., loss=0.)

    def step(self, *, action_kw, load_kw, solar_kw, hours, import_price, export_price, available=True, strict=True):
        finite(action_kw, load_kw, solar_kw, hours, import_price, export_price)
        if min(load_kw, solar_kw) < 0 or not 0 < hours <= 1 or type(available) is not bool:
            raise PlantError("invalid_exogenous_inputs")
        p = self.parameters
        self.pending.append(action_kw)
        action = self.pending.pop(0)
        violations = []
        if not available:
            action = 0.0
        if action > p.max_charge_kw + 1e-9 or action < -p.max_discharge_kw - 1e-9:
            violations.append('power_limit')
            if strict:
                raise PlantError("power_limit")
            action = min(p.max_charge_kw, max(-p.max_discharge_kw, action))
        before = self.energy
        delta = action * hours * (p.charge_efficiency if action >= 0 else 1 / p.discharge_efficiency)
        after = before + delta
        if after < p.reserve * p.capacity_kwh - 1e-8 or after > p.capacity_kwh + 1e-8:
            violations.append('energy_limit')
            if strict:
                raise PlantError("energy_limit")
            after = min(p.capacity_kwh, max(p.reserve*p.capacity_kwh, after))
            delta = after-before
            action = delta/hours/(p.charge_efficiency if delta >= 0 else 1/p.discharge_efficiency)
        self.energy = min(p.capacity_kwh, max(p.reserve * p.capacity_kwh, after))
        grid = load_kw - solar_kw + action
        imported, exported = max(grid, 0) * hours, max(-grid, 0) * hours
        loss = action * hours - delta
        if abs(imported - exported + solar_kw * hours - load_kw * hours - delta - loss) > 1e-8:
            raise PlantError("conservation_failure")
        self.cost += imported * import_price - exported * export_price
        for key, value in dict(imported=imported, exported=exported, solar=solar_kw*hours, load=load_kw*hours, loss=loss).items():
            self.balance[key] += value
        balance = self.balance
        if abs(self.initial_energy+balance['imported']-balance['exported']+balance['solar']-balance['load']-balance['loss']-self.energy) > 1e-7:
            raise PlantError('cumulative_conservation_failure')
        return {"energy_kwh": self.energy, "import_kwh": imported, "export_kwh": exported,
                "loss_kwh": loss, "cost": self.cost, 'violations': violations, 'actual_power_kw': action}


def calibrate(parameters, training, withheld, *, tolerance_kwh=.05, resolution_kwh=.005):
    """Fit efficiency only, using separate observed rows. Never invent physics.

    Each row has before/after energy, measured AC battery power and interval.
    Returned coverage is a range, not permission to extrapolate another site.
    """
    if not 4 <= len(training) <= 10000 or not 2 <= len(withheld) <= 10000:
        raise PlantError("calibration_coverage")
    samples = {"charge": [], "discharge": []}
    if any(type(row.get('at')) is not int for row in training+withheld) or max(row['at'] for row in training) >= min(row['at'] for row in withheld):
        raise PlantError('withheld_time_leakage')
    for row in training:
        before, after, power, hours = (row[key] for key in ("before", "after", "power", "hours"))
        finite(before, after, power, hours)
        if not 0 < hours <= 1 or not 0 <= before <= parameters.capacity_kwh or not 0 <= after <= parameters.capacity_kwh:
            raise PlantError("calibration_range")
        if abs(power)<.1 or abs(after-before) < 2*resolution_kwh:
            continue
        if power > 0:
            samples["charge"].append((after-before, power*hours))
        else:
            samples["discharge"].append((-power*hours, before-after))
    if min(map(len, samples.values())) < 2:
        raise PlantError("calibration_direction_coverage")
    ratios = {key:sum(row[0] for row in values)/sum(row[1] for row in values) for key,values in samples.items()}
    if any(not .7 <= ratio <= 1 for ratio in ratios.values()):
        raise PlantError('implausible_calibration')
    fitted = replace(parameters, charge_efficiency=ratios['charge'], discharge_efficiency=ratios['discharge'])
    errors = []
    directions=set()
    for row in withheld:
        finite(*[row[key] for key in ("before", "after", "power", "hours")])
        hours, power = row["hours"], row["power"]
        if not 0 < hours <= 1 or abs(power) > max(abs(item["power"]) for item in training) or not 0 <= row['before'] <= parameters.capacity_kwh or not 0 <= row['after'] <= parameters.capacity_kwh:
            raise PlantError("withheld_out_of_range")
        if abs(power)<.1 or abs(row['after']-row['before'])<2*resolution_kwh:
            continue
        directions.add(power>0)
        predicted = row["before"] + power * hours * (fitted.charge_efficiency if power >= 0 else 1/fitted.discharge_efficiency)
        errors.append(abs(predicted-row["after"]))
    if len(errors)<2 or len(directions)!=2:
        raise PlantError('withheld_direction_coverage')
    return fitted, {"valid": max(errors) <= tolerance_kwh, "withheld_count": len(errors),
        "max_error_kwh": max(errors), "max_power_kw": max(abs(row["power"]) for row in training),
        "min_energy_kwh": min(row["before"] for row in training), "max_energy_kwh": max(row["before"] for row in training)}


def paired(parameters, initial_soc, exogenous, baseline, candidate, *, terminal_price=None):
    if not 1 <= len(exogenous) <= 10080 or len(baseline) != len(exogenous) or len(candidate) != len(exogenous):
        raise PlantError("trajectory_coverage")
    scores = []
    if terminal_price is not None:
        finite(terminal_price)
        if terminal_price < 0:
            raise PlantError('invalid_terminal_price')
    for actions in (baseline, candidate):
        plant = Plant(parameters, initial_soc)
        violations = 0
        for inputs, action in zip(exogenous, actions):
            result = plant.step(action_kw=action, strict=False, **inputs)
            violations += len(result['violations'])
        scores.append({"cost": plant.cost, "terminal_kwh": plant.energy, 'violations': violations})
    return {"baseline": scores[0], "candidate": scores[1], "cost_delta": scores[1]["cost"]-scores[0]["cost"],
            "terminal_delta_kwh": scores[1]["terminal_kwh"]-scores[0]["terminal_kwh"],
            'common_score_delta': None if terminal_price is None else scores[1]['cost']-scores[0]['cost']-terminal_price*(scores[1]['terminal_kwh']-scores[0]['terminal_kwh'])}


def sensitivity(parameters, initial_soc, exogenous, baseline, candidate, *, terminal_price=None):
    results = []
    variants = [parameters]
    for sign in (-1, 1):
        if parameters.capacity_kwh + sign*parameters.uncertainty_kwh <= 0:
            raise PlantError('uncertainty_out_of_range')
        variants.extend([replace(parameters, capacity_kwh=parameters.capacity_kwh + sign*parameters.uncertainty_kwh),
            replace(parameters, charge_efficiency=min(1,max(.01,parameters.charge_efficiency+sign*.01)), discharge_efficiency=min(1,max(.01,parameters.discharge_efficiency+sign*.01))),
            replace(parameters, reserve=min(1,max(0,parameters.reserve+sign*.01))),
            replace(parameters, delay_steps=min(60,max(0,parameters.delay_steps+sign)))])
    for altered in variants:
        results.append(paired(altered, initial_soc, exogenous, baseline, candidate,terminal_price=terminal_price))
    return {"cost_delta_min": min(row["cost_delta"] for row in results),
            "cost_delta_max": max(row["cost_delta"] for row in results), "arms": len(results),
            'max_common_score_delta':None if terminal_price is None else max(row['common_score_delta'] for row in results),
            'baseline_violations':sum(row['baseline']['violations'] for row in results),
            'candidate_violations':sum(row['candidate']['violations'] for row in results)}
