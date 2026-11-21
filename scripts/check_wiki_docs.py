#!/usr/bin/env python3
"""Check configuration documentation coverage, source drift and a local wiki.

The source-only mode has no network dependency. Full validation needs an explicit
wiki checkout. Updating signatures acknowledges review; it does not write prose.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter, deque
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MAP = ROOT / 'docs/wiki-configuration-map.json'
COMPONENTS = (
    'config_flow.py', 'const.py', 'battery_backend/profiles.py',
    'battery_backend/discovery.py', 'settings_metadata.py', 'strings.json',
    'translations/en.json', 'services.yaml', 'switch.py', 'select.py',
    'number.py', 'sensor.py', 'binary_sensor.py', 'button.py', 'automations/actions.py',
    'licensing/presentation.py',
    'amber_smartshift/__init__.py', 'amber_smartshift/client.py',
    'amber_smartshift/runtime.py', 'amber_smartshift/controls.py',
    'amber_smartshift/flow.py', 'amber_smartshift/entities.py',
    'retail_plan_steps.py', 'retail_plan_flow.py',
)

def digest(data: str) -> str:
    return hashlib.sha256(data.encode()).hexdigest()

def source_state(root: Path) -> dict:
    component_hashes = {}
    for name in COMPONENTS:
        text = (root / 'custom_components/power_sync' / name).read_text()
        if name.endswith('.py'):
            text = ast.dump(ast.parse(text), include_attributes=False)
        elif name.endswith('.json'):
            text = json.dumps(json.loads(text), sort_keys=True, separators=(',', ':'))
        component_hashes[name] = digest(text)
    tree = ast.parse((root/'custom_components/power_sync/config_flow.py').read_text())
    methods = sorted(f'{cls.name}.{node.name}' for cls in tree.body if isinstance(cls, ast.ClassDef)
                     for node in cls.body if isinstance(node, ast.AsyncFunctionDef) and node.name.startswith('async_step_'))
    constants = {}
    const_tree = ast.parse((root/'custom_components/power_sync/const.py').read_text())
    for node in const_tree.body:
        if isinstance(node, ast.Assign):
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = value
    profile_tree = ast.parse((root/'custom_components/power_sync/battery_backend/profiles.py').read_text())
    profiles = sorted(ast.literal_eval(node.args[0]) for node in ast.walk(profile_tree)
                      if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                      and node.func.id == '_p' and len(node.args) >= 4)
    providers = sorted(constants['ELECTRICITY_PROVIDERS'])
    strings = json.loads((root/'custom_components/power_sync/strings.json').read_text())
    english = json.loads((root/'custom_components/power_sync/translations/en.json').read_text())
    ui_labels = {}
    for scope in ('config', 'options'):
        a = strings.get(scope,{}).get('step',{})
        b = english.get(scope,{}).get('step',{})
        for step in a.keys() | b.keys():
            sa, sb = a.get(step,{}), b.get(step,{})
            labels = {**sa.get('data',{}), **sb.get('data',{})}
            ga, gb = sa.get('sections',{}), sb.get('sections',{})
            for group in ga.keys() | gb.keys():
                labels.update({**ga.get(group,{}).get('data',{}), **gb.get(group,{}).get('data',{})})
            ui_labels.update({f'{scope}.{step}.{key}':label for key,label in labels.items()})
    return {'components': component_hashes, 'step_methods': methods,
            'profiles': profiles, 'providers': providers, 'ui_labels': dict(sorted(ui_labels.items()))}

def slug(text: str) -> str:
    # GitHub keeps unicode word characters and removes punctuation/formatting.
    text = re.sub(r'<[^>]+>', '', text)
    return re.sub(r'[^\w\s-]', '', text.lower()).replace(' ', '-')

def markdown_info(text: str) -> tuple[set[str], list[str]]:
    anchors = set(re.findall(r'<a\s+(?:[^>]*\s)?id=["\']([^"\']+)["\']', text))
    counts: Counter = Counter()
    lines = []
    fence = None
    for line in text.splitlines():
        match = re.match(r'^\s*(`{3,}|~{3,})', line)
        if match:
            token = match[1][0]
            fence = None if fence == token else token if fence is None else fence
            continue
        if fence:
            continue
        heading = re.match(r'^#{1,6}\s+(.+?)(?:\s+#+)?$', line)
        if heading:
            key = slug(heading[1])
            index = counts[key]
            counts[key] += 1
            anchors.add(key + (f'-{index}' if index else ''))
        lines.append(line)
    visible = '\n'.join(lines)
    links = [x[1] for x in re.findall(r'(!?)\[[^\]]*\]\(([^)]+)\)', visible)]
    links += re.findall(r'<img\b[^>]*\bsrc=["\']([^"\']+)["\']', visible)
    return anchors, links

def check_source(root: Path, manifest: dict) -> list[str]:
    errors = []
    actual = source_state(root)
    expected = manifest['source_state']
    for name, value in actual['components'].items():
        if expected['components'].get(name) != value:
            errors.append(f'Source documentation review required: {name}')
    recorded = {row['owner'] for row in manifest['steps']}
    if recorded != set(actual['step_methods']):
        errors.append('Step coverage differs: added=' + repr(sorted(set(actual['step_methods'])-recorded)) + ' removed=' + repr(sorted(recorded-set(actual['step_methods']))))
    if actual['step_methods'] != expected['step_methods']:
        errors.append('Step-method signature changed; update destinations/exclusions after review.')
    if set(actual['profiles']) != {row['id'] for row in manifest['profiles']}:
        errors.append('Connection-profile coverage differs from the current registry.')
    if set(actual['providers']) != {row['id'] for row in manifest['providers']}:
        errors.append('Provider coverage differs from the current choices.')
    if set(actual['ui_labels']) != set(manifest['ui_field_dispositions']):
        errors.append('UI field keys changed; assign documentation destinations or explicit source-only exclusions.')
    for row in manifest['fields']:
        key = '.'.join((row['scope'],row['form'],row['field']))
        if actual['ui_labels'].get(key) != row['label']:
            errors.append('Field label changed or removed: '+key)
    for row in manifest['steps']:
        if not row.get('pages') and row.get('disposition') != 'legacy_or_unproven_exclusion':
            errors.append('Missing documentation destination: '+row['owner'])
    return errors

def check_wiki(wiki: Path, manifest: dict) -> list[str]:
    errors = []
    pages = {path.stem:path for path in wiki.glob('*.md')}
    texts = {name:path.read_text() for name,path in pages.items()}
    info = {name:markdown_info(text) for name,text in texts.items()}
    graph = {name:set() for name in pages}
    for name, (_,links) in info.items():
        for raw in links:
            target = raw.strip().strip('<>')
            if urlsplit(target).scheme or target.startswith('//'):
                continue
            path, _, anchor = unquote(target).partition('#')
            if path.startswith('images/'):
                if not (wiki/path).is_file():
                    errors.append(f'{name}: missing image {path}')
                continue
            dest = Path(path).name.removesuffix('.md') if path else name
            if dest not in pages:
                errors.append(f'{name}: missing wiki page {dest} ({raw})')
                continue
            graph[name].add(dest)
            if anchor and anchor not in info[dest][0]:
                errors.append(f'{name}: missing anchor {dest}#{anchor}')
    for name, anchors in manifest['compatibility_anchors'].items():
        if name not in pages:
            errors.append('Removed public page: '+name)
        else:
            for anchor in anchors:
                if anchor not in info[name][0]:
                    errors.append(f'Removed compatibility anchor: {name}#{anchor}')
    for row in manifest['steps']:
        if row['disposition']=='legacy_or_unproven_exclusion':
            continue
        for page in row['pages']:
            if page not in pages:
                errors.append(f'{row["owner"]}: missing destination {page}')
    for row in manifest['fields']:
        page = row['page'].removesuffix('.md')
        if page not in texts or row['label'].replace('|','\\|') not in texts[page]:
            errors.append(f'Missing field reference: {row["scope"]}.{row["form"]}.{row["field"]} -> {page}')
    for row in manifest['profiles']:
        if row['page'] not in texts or row['label'] not in texts[row['page']]:
            errors.append('Missing connection-profile reference: '+row['id'])
    for row in manifest['providers']:
        if row['page'] not in pages:
            errors.append('Missing provider reference: '+row['id'])
    for row in manifest['menus']:
        if not row.get('label'):
            if not row.get('waiver'):
                errors.append('Unexplained menu label gap: '+row['entry'])
        elif row['label'] not in texts.get('Configuration','') and row['label'] not in texts.get('Advanced-Features',''):
            errors.append('Missing menu label: '+row['label'])
    seen = {name for name in ('Home','_Sidebar') if name in graph}
    todo = deque(seen)
    while todo:
        for target in graph[todo.popleft()]-seen:
            seen.add(target)
            todo.append(target)
    for orphan in sorted(pages.keys()-seen):
        errors.append('Page not reachable from Home/sidebar: '+orphan)
    for name,text in texts.items():
        if any(line.rstrip()!=line for line in text.splitlines()):
            errors.append('Trailing whitespace: '+name)
        if re.search(r'Generating an explanation is always a deliberate action|Profit Max target time / SOC|On the "Secondary Sungrow Inverter" step',text):
            errors.append('Obsolete configuration instruction: '+name)
        if re.search(r'Normal intervals include their start and end|enabling one turns the other off|enabling either goal disables the other',text):
            errors.append('Incorrect boundary/goal contract: '+name)
        if re.search(r'\{(?:auth_url|amber_url|teslemetry_url|brand)\}',text):
            errors.append('Unresolved form-description placeholder: '+name)
        if re.search(r'(?:set|Set) (?:your )?export[^\n]*to \*?\*?0c',text):
            errors.append('Review advice to falsify base export rates: '+name)
    return errors

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=ROOT)
    parser.add_argument('--manifest',type=Path,default=DEFAULT_MAP)
    parser.add_argument('--wiki',type=Path)
    parser.add_argument('--source-only',action='store_true')
    parser.add_argument('--refresh-source-signatures',action='store_true',help='After documentation-impact review, acknowledge source changes in the manifest.')
    args = parser.parse_args()
    if not args.source_only and args.wiki is None and not args.refresh_source_signatures:
        parser.error('Pass --source-only or --wiki /path/to/wiki.')
    manifest = json.loads(args.manifest.read_text())
    if args.refresh_source_signatures:
        manifest['source_state'] = source_state(args.root)
        args.manifest.write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+'\n')
        print('Source signatures updated; review coverage and validate the wiki before publication.')
        return 0
    errors = check_source(args.root,manifest)
    if args.wiki is not None:
        errors.extend(check_wiki(args.wiki,manifest))
    if errors:
        for error in errors:
            print(error,file=sys.stderr)
        print(f'FAILED: {len(errors)} documentation issue(s).',file=sys.stderr)
        return 1
    print(f'PASS: {len(manifest["steps"])} step destinations/exclusions; '+(f'{len(manifest["profiles"])} profiles, {len(manifest["providers"])} providers, {len(manifest["fields"])} field references and wiki links/anchors.' if args.wiki else 'source signatures match.'))
    return 0

if __name__=='__main__':
    raise SystemExit(main())
