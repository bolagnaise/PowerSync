"""Synthetic loopback transport for client interoperability checks only."""
import os
from pathlib import Path
import sys
import urllib.request
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'custom_components/power_sync'))
from diagnostics_access import bridge, wire


def transport(self, route, value):
    request = urllib.request.Request(os.environ['DIAGNOSTICS_TEST_URL']+route, data=wire.json_bytes(value), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=15) as response:
        return wire.loads(response.read(420*1024+1), maximum=420*1024)


bridge.Bridge.http = transport
bridge.main()
