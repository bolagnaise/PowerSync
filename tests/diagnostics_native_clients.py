"""Native MCP discovery without starting an LLM turn or loading user MCPs."""
import asyncio
import json
import os
from pathlib import Path
import tempfile


async def codex(config, directory):
    from private_regression.sandbox import kill_group
    fixture = Path(__file__).with_name('diagnostics_stdio_fixture.py')
    python = '/mnt/user/developer/codex-storage/workspaces/power-sync-test-venv/bin/python'
    home = directory/'codex-config'
    home.mkdir(mode=0o700)
    (home/'config.toml').write_text('[mcp_servers.powersync]\ncommand = '+json.dumps(python)+'\nargs = '+json.dumps([str(fixture.resolve()), '--profile', 'codex', '--config', str(config.resolve())])+'\n[mcp_servers.powersync.env]\nDIAGNOSTICS_TEST_URL = '+json.dumps(os.environ['DIAGNOSTICS_TEST_URL'])+'\n')
    env = {**os.environ, 'CODEX_HOME': str(home)}
    process = await asyncio.create_subprocess_exec('/root/.local/bin/codex', 'app-server', '--listen', 'stdio://',
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=env,start_new_session=True)
    descriptor=os.pidfd_open(process.pid)
    async def rpc(identity, method, params):
        process.stdin.write((json.dumps({'id':identity,'method':method,'params':params})+'\n').encode());await process.stdin.drain()
        while True:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=30)
            if not line:
                raise AssertionError('Codex app server exited')
            reply = json.loads(line)
            if reply.get('id') == identity:
                assert 'error' not in reply, reply.get('error')
                return reply['result']
    try:
        await rpc(1, 'initialize', {'clientInfo':{'name':'powersync-compatibility','version':'1'}, 'capabilities':{'experimentalApi':True}})
        process.stdin.write(b'{"method":"initialized","params":{}}\n');await process.stdin.drain()
        status = await rpc(2, 'mcpServerStatus/list', {'serverName':'powersync','detail':'toolsAndAuthOnly'})
        rows = status.get('data', [])
        assert len(rows) == 1 and any('ha_get_capabilities' in name for name in rows[0]['tools']), 'Codex did not discover HA tools'
    finally:
        kill_group(descriptor)
        await process.wait()
        os.close(descriptor)


async def claude(config, directory):
    fixture = Path(__file__).with_name('diagnostics_stdio_fixture.py').resolve()
    python = '/mnt/user/developer/codex-storage/workspaces/power-sync-test-venv/bin/python'
    url = os.environ.get('DIAGNOSTICS_CLAUDE_TEST_URL', os.environ['DIAGNOSTICS_TEST_URL'])
    prefix = ['docker', 'exec', '-e', 'CLAUDE_CONFIG_DIR='+str(directory/'claude-config'), 'claude-server', '/usr/local/bin/claude-auth']
    add = await asyncio.create_subprocess_exec(*prefix, 'mcp', 'add-json', '--scope', 'user', 'powersync', json.dumps({'type':'stdio','command':python,'args':[str(fixture),'--profile','claude','--config',str(config.resolve())],'env':{'DIAGNOSTICS_TEST_URL':url}}),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, errors = await asyncio.wait_for(add.communicate(), timeout=20)
    assert add.returncode == 0, errors.decode()[:500]
    process = await asyncio.create_subprocess_exec(*prefix, 'mcp', 'list',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=40)
    assert process.returncode == 0 and b'powersync' in stdout and b'Connected' in stdout, 'Claude MCP health check failed: '+stdout.decode()[:500]+stderr.decode()[:500]


async def native(clients):
    # This path is shared by the two existing client containers. Only synthetic
    # invitation keys are written here, with owner-only file/directory modes.
    base = Path(__file__).resolve().parents[2]/'ha-diagnostics-service-stage'
    with tempfile.TemporaryDirectory(prefix='native-clients-', dir=base) as name:
        directory = Path(name)
        for client, function in zip(clients, (codex, claude)):
            config = directory/(client.profile+'.json')
            config.write_text(json.dumps(client.config));config.chmod(0o600)
            await function(config, directory)
