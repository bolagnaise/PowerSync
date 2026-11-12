"""Frozen fresh-pair campaigns, holdout consumption and seven-day receipts.

Only trusted purge-confirmed summaries enter an aggregate. No raw cases, case
fingerprints, schedules, input text or trajectories belong in this module.
"""
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
from dataclasses import dataclass, asdict


class CampaignError(ValueError):
    pass


@dataclass(frozen=True)
class Campaign:
    baseline: str
    candidate: str
    runtime: str
    scorer: str
    schema: str
    cohort: str
    max_cost_delta: float
    min_installations: int
    required_days: int = 7
    minimum_daily_pairs: int = 1
    pair: str = "0"*64
    currency: str = 'AUD'
    max_mean_cost_delta: float = 0.0

    def __post_init__(self):
        for value in (self.baseline, self.candidate, self.runtime, self.scorer, self.schema, self.cohort, self.pair):
            if type(value) is not str or len(value) != 64 or any(char not in '0123456789abcdef' for char in value):
                raise CampaignError("immutable_artifact_required")
        if any(type(value) is not int for value in (self.min_installations, self.required_days, self.minimum_daily_pairs)) or type(self.max_cost_delta) not in (int, float) or not math.isfinite(self.max_cost_delta) or not 1 <= self.min_installations <= 10000 or self.required_days != 7 or not 1 <= self.minimum_daily_pairs <= 10000:
            raise CampaignError("invalid_campaign")
        if type(self.currency) is not str or len(self.currency)!=3 or not self.currency.isascii() or not self.currency.isupper() or not self.currency.isalpha() or type(self.max_mean_cost_delta) not in (int,float) or not math.isfinite(self.max_mean_cost_delta):
            raise CampaignError('invalid_campaign_units')

    @property
    def digest(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def holdout(secret, campaign, installation, *, consumed=False):
    if type(secret) is not bytes or len(secret) < 32 or consumed:
        raise CampaignError("holdout_rotation_required")
    digest = hmac.digest(secret, (campaign.cohort + ":" + installation).encode(), "sha256")
    return "holdout" if int.from_bytes(digest[:8], "big") % 5 == 0 else "development"


FIELDS = frozenset({"job", "campaign", "installation", "completed_at", "purge", "status", "cost_delta", "violations", "coverage", "cohort", "pair", "scorer", "schema", "attempts"})


def receipt(value, campaign):
    if type(value) is not dict or set(value) != FIELDS:
        raise CampaignError("untrusted_summary")
    if value["campaign"] != campaign.digest or value["pair"] != campaign.pair or value["scorer"] != campaign.scorer or value["schema"] != campaign.schema or value["purge"] != "confirmed" or value["status"] not in ("pass", "fail", "insufficient", "invalid") or value["cohort"] not in ("development", "holdout"):
        raise CampaignError("ineligible_summary")
    for key in ("job", "installation"):
        if type(value[key]) is not str or len(value[key]) != 64 or any(char not in '0123456789abcdef' for char in value[key]):
            raise CampaignError("invalid_identity")
    if type(value["attempts"]) is not int or not 0<=value["attempts"]<=32 or type(value["completed_at"]) is not int or type(value["violations"]) is not int or value["violations"] < 0 or type(value["cost_delta"]) not in (int, float) or not math.isfinite(value["cost_delta"]) or value["coverage"] not in ("complete", "insufficient"):
        raise CampaignError("untrusted_summary")
    return dict(value)


class HoldoutLedger:
    """Durable metadata only. One consumed cohort epoch cannot score a new pair."""
    def __init__(self, path):
        path = Path(path)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise CampaignError('private_ledger_required')
        finally:
            os.close(descriptor)
        self.db = sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS holdout (epoch TEXT PRIMARY KEY, campaign TEXT NOT NULL, secret_hash TEXT NOT NULL)')
        self.db.commit()

    def consume(self, campaign, secret):
        if type(secret) is not bytes or len(secret) < 32:
            raise CampaignError('private_holdout_key_required')
        digest = hashlib.sha256(secret).hexdigest()
        if not hmac.compare_digest(digest,campaign.cohort):
            raise CampaignError('holdout_key_changed')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute('SELECT campaign,secret_hash FROM holdout WHERE epoch=?', (campaign.cohort,)).fetchone()
            if row and row != (campaign.digest, digest):
                raise CampaignError('holdout_rotation_required')
            self.db.execute('INSERT OR IGNORE INTO holdout VALUES (?,?,?)', (campaign.cohort, campaign.digest, digest))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise



def seven_day(campaign, receipts, *, start, now, reservations=None, holdout_secret=None, ledger=None):
    if type(start) is not int or type(now) is not int or now < start or len(receipts) > 100000:
        raise CampaignError("invalid_observation_window")
    authorised = ledger is not None and type(holdout_secret) is bytes and reservations is not None and campaign.pair != '0'*64
    if authorised:
        ledger.consume(campaign, holdout_secret)
    end = start + 7*86400
    jobs, rows, failed = set(), [], 0
    for raw in receipts:
        row = receipt(raw, campaign)
        if row['job'] in jobs:
            raise CampaignError('duplicate_receipt')
        jobs.add(row['job'])
        if not start <= row['completed_at'] < min(now+1, end):
            continue
        if authorised and row['cohort'] != holdout(holdout_secret, campaign, row['installation']):
            raise CampaignError('holdout_assignment_changed')
        rows.append(row)
        # A known failure remains visible alongside invalid or missing work.
        failed += row['status'] == 'fail' or row['violations'] > 0 or row['cost_delta'] > campaign.max_cost_delta
    expected = {}
    for reservation in reservations or []:
        if type(reservation) is not dict or set(reservation) != {'job','installation','created_at','campaign'} or reservation['campaign'] != campaign.digest or type(reservation['created_at']) is not int:
            raise CampaignError('invalid_reservation')
        if start <= reservation['created_at'] < min(now+1,end):
            if reservation['job'] in expected:
                raise CampaignError('duplicate_reservation')
            expected[reservation['job']] = reservation
    if authorised and any(row['job'] not in expected or row['installation'] != expected[row['job']]['installation'] for row in rows):
        raise CampaignError('unreserved_receipt')
    completed = {row['job'] for row in rows}
    missing = len(set(expected)-completed)
    invalid = sum(row['status'] in ('invalid','insufficient') or row['coverage'] != 'complete' for row in rows)
    eligible = [row for row in rows if row['status'] in ('pass','fail') and row['coverage'] == 'complete']
    per_site = {}
    for row in eligible:
        day = (row['completed_at']-start)//86400
        per_site.setdefault(row['installation'], {}).setdefault(day, []).append(row)
    # Repeated retries/captures from one site do not buy more cohort weight.
    covered = {site for site, days in per_site.items() if len(days) == 7}
    days = [sum(day in per_site[site] for site in covered) for day in range(7)]
    holdouts = {row['installation'] for row in eligible if row['cohort'] == 'holdout'} & covered
    development = covered-holdouts
    site_means = [sum(sum(row['cost_delta'] for row in items)/len(items) for items in per_site[site].values())/len(per_site[site]) for site in covered]
    mean = sum(site_means)/len(site_means) if site_means else None
    dispersion=(sum((value-mean)**2 for value in site_means)/(len(site_means)-1)/len(site_means))**.5 if len(site_means)>1 else 0
    # Conservative stepwise two-sided Student-t quantiles. Logical installs
    # are not proven independent households; this is a dispersion guard.
    quantile=12.707
    for degrees,value in ((2,4.303),(3,3.183),(4,2.777),(5,2.571),(9,2.263),(19,2.094),(29,2.046)):
        if len(site_means)-1>=degrees:
            quantile=value
    upper=None if mean is None else mean+quantile*dispersion
    mean_exceeded=mean is not None and mean>campaign.max_mean_cost_delta
    dispersion_passed=upper is not None and upper<=campaign.max_mean_cost_delta
    complete = (authorised and now >= end and len(covered) >= campaign.min_installations and bool(holdouts) and bool(development)
        and all(count >= campaign.minimum_daily_pairs for count in days) and len(expected)>0 and not missing and not invalid and dispersion_passed)
    return {'campaign':campaign.digest, 'status':'fail' if failed or mean_exceeded else 'pass' if complete else 'pending',
        'logical_installations':len(covered), 'unique_households':None, 'eligible_pairs':len(eligible),
        'holdout_installations':len(holdouts), 'failed_pairs':int(failed), 'invalid_pairs':invalid, 'missing_pairs':missing,
        'reserved_pairs':len(expected), 'daily_counts':days, 'mean_cost_delta':mean,'dispersion_upper_bound':upper,'currency':campaign.currency,
        'aggregate_mean_exceeded':mean_exceeded,'dispersion_guard_passed':dispersion_passed,
        'observed_from':min((row['completed_at'] for row in rows),default=None),
        'observed_until':max((row['completed_at'] for row in rows),default=None)}


def acceptance(*, software, protected_main, codex, claude, runner, consent, calibration, observation):
    evidence = {"software_integration": software, "protected_main_release": protected_main,
        "codex_compatibility": codex, "claude_compatibility": claude, "production_runner_activation": runner,
        "owner_consent": consent, "physical_calibration": calibration, "seven_day_observation": observation}
    if any(value not in ("passed", "pending", "failed") for value in evidence.values()):
        raise CampaignError("invalid_acceptance")
    return {"evidence": evidence, "fleet_gate": "passed" if all(value == "passed" for value in evidence.values()) else "failed" if "failed" in evidence.values() else "pending"}
