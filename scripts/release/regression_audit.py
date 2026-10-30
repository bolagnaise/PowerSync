"""Controller-side collection and completion audit for the full suite."""
import json
import os
from pathlib import Path

import pytest

_collected = []
_collections = {}
_reports = []
_errors = []
_worker = False


def pytest_configure(config):
    global _worker
    _worker = hasattr(config, "workerinput")


def pytest_collection_finish(session):
    _collected[:] = [item.nodeid for item in session.items]


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_node_collection_finished(node, ids):
    _collections[node.gateway.id] = list(ids)


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node, error):
    if error:
        _errors.append(f"{node.gateway.id}: {error}")


def pytest_collectreport(report):
    if report.failed:
        _errors.append(str(report.longrepr))


def pytest_runtest_logreport(report):
    _reports.append({"nodeid": report.nodeid, "when": report.when,
                     "outcome": report.outcome,
                     "worker": getattr(report, "worker_id", "serial"),
                     "xfail": hasattr(report, "wasxfail")})


def pytest_sessionfinish(session, exitstatus):
    if not _worker:
        path = Path(os.environ["PS_REGRESSION_AUDIT"])
        data = {"run_id": os.environ["PS_REGRESSION_RUN_ID"],
                "collected": _collected, "collections": _collections,
                "reports": _reports, "errors": _errors, "exitstatus": int(exitstatus)}
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data))
        temporary.replace(path)
