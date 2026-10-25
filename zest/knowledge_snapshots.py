"""Append-per-run knowledge snapshots and portable, non-authoritative manifests."""
from copy import deepcopy
import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

from .cognition_store import CognitionStore, digest, valid_id
from .files import FileReadError
from .resources import _directory, _regular, record_state

MANIFEST = '.zest/docs/generated/knowledge.json'
MAX_BYTES = 4 * 1024 * 1024


def read_record(path):
    _directory(path.parent)
    _regular(path)
    if not path.exists():
        return None
    try:
        if path.stat().st_size > MAX_BYTES:
            raise ValueError
        record = json.loads(path.read_bytes())
        if record['sha256'] != digest(record['payload']) or record['payload']['version'] != 1:
            raise ValueError
        return record['payload']
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise FileReadError('knowledge_snapshot_error', '知识清单无法校验，未猜测页面基线。') from exc


def snapshot_path(state_dir, run_id):
    directory = record_state(state_dir, 'cognition', valid_id(run_id))
    return directory / 'knowledge-snapshots' / (run_id + '.json')


def save_snapshot(state_dir, run_id, value):
    from .initialization import atomic
    raw = encode(value)
    atomic(snapshot_path(state_dir, run_id), raw)


def encode(value):
    from .document_pages import safe_text
    raw = safe_text(json.dumps({'payload': value, 'sha256': digest(value)}, ensure_ascii=False))
    if len(raw) > MAX_BYTES:
        raise FileReadError('knowledge_snapshot_budget', '知识清单超过保存容量。')
    return raw


def artifact_integrity(project, hashes):
    from .documents import _parents
    from .files import MAX_FILE_BYTES
    root = Path(project['root_path'])
    issues = []
    for path, expected in hashes.items():
        if not path.startswith('.zest/docs/'): continue
        try:
            _parents(root, path)
            target = root / path
            if not target.exists():
                issues.append({'path': path, 'status': 'missing'})
            elif target.stat().st_size > MAX_FILE_BYTES or hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                issues.append({'path': path, 'status': 'modified'})
        except (FileReadError, OSError):
            issues.append({'path': path, 'status': 'unavailable'})
    return issues


def fingerprint(fact):
    return digest({'section': fact['section'], 'statement': fact['statement'],
                   'paths': sorted({p['evidence']['path'] for p in fact['evidence']})})


def new_claim(fact, commit):
    return {'id': 'K' + fingerprint(fact)[:24], 'revision': 1, 'fact': deepcopy(fact),
            'origin': fact['id'], 'last_verified_commit': commit, 'evidence_checked_commit': commit,
            'status': 'verified', 'previous': None}


def page_bindings(spec, claims):
    return {key: {'revision': claims[key]['revision'],
                  'statement_sha256': hashlib.sha256(claims[key]['fact']['statement'].encode()).hexdigest()}
            for key in spec['finding_ids'] if key in claims}


def dependencies(root, envelope, seeds):
    """Bounded static import hints, not a promise to model dynamic dependencies."""
    from .project_sources import load_source
    catalog = {f['evidence']['path']: f for f in envelope['files']}
    java_names = {}
    for path in catalog:
        if path.endswith(('.java', '.xml')):
            java_names.setdefault(PurePosixPath(path).stem, []).append(path)
    edges, queue, used, complete = {}, list(seeds), 0, True
    while queue:
        path = queue.pop()
        if path in edges:
            continue
        if len(edges) >= 512 or used > 8 * 1024 * 1024:
            complete = False
            break
        edges[path] = []
        suffix = PurePosixPath(path).suffix
        if path not in catalog:
            continue
        if suffix not in ('.py', '.js', '.jsx', '.ts', '.tsx', '.mjs', '.java', '.xml'):
            if suffix not in ('.md', '.rst', '.txt', '.json', '.yml', '.yaml', '.toml'):
                complete = False  # Unknown code languages receive broad invalidation.
            continue
        try:
            body = load_source(root, catalog[path])['body']
            used += len(body.encode())
            targets = []
            parent = str(PurePosixPath(path).parent)
            def resolve(name):
                for suffix in ('', '.py', '/__init__.py', '.ts', '.tsx', '.js', '.jsx', '/index.ts', '/index.js'):
                    candidate = name + suffix
                    if candidate in catalog:
                        targets.append(candidate)
                        return
            if path.endswith('.py'):
                tree = ast.parse(body)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names: resolve(alias.name.replace('.', '/'))
                    elif isinstance(node, ast.ImportFrom):
                        package = PurePosixPath(parent)
                        for _ in range(max(0, node.level - 1)): package = package.parent
                        prefix = (str(package) + '/' if node.level else '') + (node.module or '').replace('.', '/')
                        prefix = prefix.removeprefix('./')
                        resolve(prefix)
                        for alias in node.names: resolve(prefix.rstrip('/') + '/' + alias.name)
                    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == '__import__':
                        complete = False
                    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'import_module':
                        complete = False
            elif path.endswith(('.java', '.xml')):
                # Conservative class/interface/implementation and MyBatis mapper
                # hints. DI/reflection/runtime behavior is still not proven.
                for name in set(re.findall(r'\b[A-Z][A-Za-z0-9_]*\b', body)) | {PurePosixPath(path).stem}:
                    targets.extend(java_names.get(name, []))
                    targets.extend(java_names.get(name + 'Impl', []))
                for package in re.findall(r'\bimport\s+(?:static\s+)?([\w.]+)\.\*\s*;', body):
                    token = package.replace('.', '/') + '/'
                    targets.extend(p for p in catalog if p.endswith('.java') and token in p)
                targets = [p for p in targets if p != path]
            else:
                import posixpath
                for name in re.findall(r'''(?:from\s*|require\(\s*|import\(\s*|import\s*)['"]([^'"]+)['"]''', body):
                    if name.startswith('.'):
                        resolve(posixpath.normpath(parent + '/' + name))
                if re.search(r'\bimport\(\s*[^\s\'"]', body): complete = False
            edges[path] = sorted(set(targets))
            queue.extend(targets)
        except (FileReadError, SyntaxError, ValueError):
            complete = False
    return {'edges': edges, 'complete': complete, 'kind': 'static_import_hints_only'}


def closure(paths, graphs):
    result, pending = set(paths), list(paths)
    while pending:
        path = pending.pop()
        for graph in graphs:
            for target in graph.get('edges', {}).get(path, []):
                if target not in result:
                    result.add(target); pending.append(target)
    return sorted(result)


def seed_snapshot(state_dir, project, dossier, ledger):
    from .document_pages import facts_for
    raw = facts_for(dossier)
    claims, aliases = {}, {}
    for old_id, fact in raw.items():
        claim = new_claim(fact, dossier['commit'])
        aliases[old_id] = claim['id']
        claims[claim['id']] = claim
    pages = []
    for page in ledger.value.get('pages', []):
        spec = deepcopy(page['spec'])
        spec['finding_ids'] = list(dict.fromkeys(aliases[x] for x in spec['finding_ids']))
        pages.append({'spec': spec, 'status': page['status'], 'published_sha256': page.get('published_sha256'),
                      'claim_bindings': page_bindings(spec, claims) if page['status'] == 'completed' else {},
                      'last_generated_commit': dossier['commit'] if page['status'] == 'completed' else None,
                      'last_verified_commit': dossier['commit'] if page['status'] == 'completed' else None,
                      'evidence_checked_commit': dossier['commit'], 'source_run_id': dossier['run_id']})
    envelope = CognitionStore(state_dir).show(dossier['run_id'], include_envelope=True)['envelope']
    paths = {p['evidence']['path'] for c in claims.values() for p in c['fact']['evidence']}
    return {'version': 1, 'commit': dossier['commit'], 'run_id': dossier['run_id'],
            'claims': claims, 'pages': pages, 'graph': dependencies(project['root_path'], envelope, paths),
            'coverage_gaps': [], 'manifest_sha256': None}


def validate_local(state_dir, baseline):
    """The portable file is data; only local reviewed records supply authority."""
    if baseline.get('trust') == 'shared_unverified':
        return
    for claim in baseline['claims'].values():
        if claim['status'] == 'retracted' or claim.get('authority') == 'shared_unverified':
            continue
        origin = claim['origin']
        run_id, finding_id = origin.split(':', 1)
        current = CognitionStore(state_dir).reviewed_finding(run_id, finding_id)
        if current['body'] != claim['fact']['statement']:
            raise FileReadError('knowledge_baseline_changed', '继承事实已失效或改变，未继续复用。')


def relocate(root, envelope, fact):
    """Locate complete identical evidence; repeated text is never guessed."""
    from .cognition_tools import PackageTools
    tools = PackageTools(envelope, root=root)
    catalog = {s['evidence']['path']: s for s in envelope['files']}
    proofs, states = [], []
    for old in fact['evidence']:
        ev = old['evidence']; source = catalog.get(ev['path'])
        if source is None:
            return {'status': 'missing', 'evidence': []}
        try:
            loaded = tools.source(source['id'])
            lines = tools.lines(loaded)
            needle = old['text'].split('\n')
            if loaded['evidence']['blob_id'] == ev['blob_id']:
                offset = ev['start_line'] - 1
                matches = [offset] if [x.removesuffix('\r') for x in lines[offset:offset + len(needle)]] == needle else []
            else:
                matches = [i for i in range(len(lines) - len(needle) + 1)
                           if [x.removesuffix('\r') for x in lines[i:i + len(needle)]] == needle]
            if len(matches) != 1:
                return {'status': 'ambiguous' if matches else 'changed', 'evidence': []}
            start = matches[0] + 1
            proof = tools.read(source['id'], start, start + len(needle) - 1)
            if proof['text'] != old['text']:
                return {'status': 'changed', 'evidence': []}
            proofs.append(proof)
            states.append('unchanged' if source['evidence']['blob_id'] == ev['blob_id'] else 'relocated')
        except FileReadError:
            return {'status': 'unavailable', 'evidence': []}
    return {'status': 'relocated' if 'relocated' in states else 'unchanged', 'evidence': proofs}


def freeze_context(state_dir, project, envelope, base_run_id):
    from .document_pages import DocumentPages
    baseline = read_record(snapshot_path(state_dir, base_run_id))
    if baseline is None:
        ledger = DocumentPages(state_dir, base_run_id)
        if not ledger.value or not ledger.value.get('pages'):
            return None  # Legacy dossiers have no trustworthy page ownership.
        baseline = seed_snapshot(state_dir, project, CognitionStore(state_dir).dossier(base_run_id), ledger)
    validate_local(state_dir, baseline)
    return prepare_context(project, envelope, baseline)


def check_context(state_dir, envelope):
    context = envelope.get('incremental_context')
    if context is None: return
    from .incremental_contracts import incremental_contract
    if (envelope.get('incremental_contract') != incremental_contract() or context.get('version') != 1
            or context.get('target_commit') != envelope['commit']
            or context.get('baseline_sha256') != digest(context['baseline'])):
        raise FileReadError('cognition_contract_changed', '知识继承契约、目标提交或快照身份不一致。')
    validate_local(state_dir, context['baseline'])


def prepare_context(project, envelope, baseline):
    from .git import compare_commits
    paths = {p['evidence']['path'] for c in baseline['claims'].values() for p in c['fact']['evidence']}
    graph = dependencies(project['root_path'], envelope, paths)
    changes = compare_commits(Path(project['root_path']), baseline['commit'], envelope['commit'])
    # Generated artifacts are represented by page hashes, never source effects.
    changed = {c['path'] for c in changes if not c['path'].startswith('.zest/docs/initialization/') and c['path'] != MANIFEST}
    global_files = {'pyproject.toml', 'package.json', 'package-lock.json', 'pom.xml', 'go.mod', 'Cargo.toml', 'tsconfig.json',
                    'requirements.txt', 'uv.lock', 'poetry.lock', 'yarn.lock', 'pnpm-lock.yaml', 'build.gradle', 'Dockerfile'}
    broad = (not graph['complete'] or not baseline.get('graph', {}).get('complete', False)
             or any(PurePosixPath(p).name in global_files or PurePosixPath(p).suffix in ('.properties', '.yml', '.yaml', '.sql') for p in changed))
    checks = {}
    for key, claim in baseline['claims'].items():
        if claim['status'] == 'retracted':
            continue
        check = relocate(project['root_path'], envelope, claim['fact'])
        deps = closure([p['evidence']['path'] for p in claim['fact']['evidence']], [graph, baseline.get('graph', {})])
        check.update(dependency_paths=deps, affected=bool(broad and changed or changed.intersection(deps)
                                                       or check['status'] not in ('unchanged', 'relocated')
                                                       or claim['status'] in ('needs_review', 'unresolved')
                                                       or baseline.get('trust') == 'shared_unverified'))
        checks[key] = check
    return {'version': 1, 'baseline': baseline, 'baseline_sha256': digest(baseline),
            'target_commit': envelope['commit'], 'checks': checks, 'graph': graph,
            'changes': sorted(changed),
            'coverage_changes': sorted({p['path'] for p in envelope.get('refresh_context', {}).get('focus', [])}
                                       if 'refresh_context' in envelope else changed),
            'static_dependency_limitations': True}


def impact_view(context):
    from .cognition_contracts import canonical
    all_claims = [{'id': key, 'statement': context['baseline']['claims'][key]['fact']['statement'],
                   'dependency_paths': check['dependency_paths'], 'evidence_status': check['status']}
                  for key, check in context['checks'].items() if check['affected']]
    shown = []
    for item in all_claims:
        if len(canonical([*shown, item])) > 8000: break
        shown.append(item)
    return {'semantics': '旧事实只有证据检查，不表示新版本语义已确认；调查受影响断言及新模块。',
            'affected_claims': shown, 'changed_paths': context['changes'][:100],
            'coverage': {'shown_claims': len(shown), 'total_affected_claims': len(all_claims),
                         'shown_paths': min(100, len(context['changes'])), 'total_paths': len(context['changes']),
                         'omitted_is_not_completed': True}}


def shared_context(project, envelope):
    """Rebuild shared identities as unverified leads, never as write authority."""
    from .project_sources import load_source
    from .files import _validate_path, read_project_file
    source = next((s for s in envelope['files'] if s['evidence']['path'] == MANIFEST), None)
    if source is None: return None
    try:
        record = json.loads(load_source(project['root_path'], source)['body'])
        baseline = record['payload']
        if record['sha256'] != digest(baseline) or baseline['version'] != 1:
            raise ValueError
        if not isinstance(baseline['claims'], dict) or len(baseline['claims']) > 512 or not 1 <= len(baseline['pages']) <= 6:
            raise ValueError
        for key, claim in baseline['claims'].items():
            if not re.fullmatch(r'K[0-9a-f]{24}', key) or key != claim['id'] or type(claim['revision']) is not int or not 1 <= claim['revision'] <= 100000:
                raise ValueError
            if not isinstance(claim['fact']['statement'], str) or not 0 < len(claim['fact']['statement']) <= 8000:
                raise ValueError
            if claim['fact']['section'] not in {'purpose', 'architecture', 'flows', 'dependencies', 'development', 'constraints'}:
                raise ValueError
            if not 1 <= len(claim['fact']['evidence']) <= 32: raise ValueError
            hydrated = []
            for proof in claim['fact']['evidence']:
                ev = proof['evidence']; _validate_path(ev['path'])
                if type(ev['start_line']) is not int or type(ev['end_line']) is not int or not 0 < ev['start_line'] <= ev['end_line']:
                    raise ValueError
                # Rehydrate only from the exact original Git source. Shallow or
                # missing history remains a visible preparation problem.
                original = read_project_file(project, ev['path'], commit=ev['commit'], start_line=ev['start_line'], end_line=ev['end_line'])
                text = '\n'.join(line['text'] for line in original['lines'])
                if (original['evidence']['blob_id'] != ev['blob_id'] or hashlib.sha256(text.encode()).hexdigest() != proof['text_sha256']):
                    raise ValueError
                hydrated.append({**proof, 'text': text})
            claim['fact']['evidence'] = hydrated
            claim['authority'] = 'shared_unverified'
        names = set()
        for page in baseline['pages']:
            spec = page['spec']; slug = spec['slug']
            if not re.fullmatch(r'[a-z][a-z0-9-]{0,47}', slug) or slug == 'index' or slug in names: raise ValueError
            names.add(slug)
            if not set(spec['finding_ids']) <= baseline['claims'].keys(): raise ValueError
            # Imported checksums prove consistency, not permission to overwrite
            # a team member's document or an edited manifest.
            page['source_sha256'] = page.pop('published_sha256', None)
            page['status'] = 'blocked'
        for page in baseline['pages']:
            if not set(page['spec']['related_pages']) <= names - {page['spec']['slug']}: raise ValueError
        for path, targets in baseline.get('graph', {}).get('edges', {}).items():
            _validate_path(path)
            for target in targets: _validate_path(target)
        baseline['trust'] = 'shared_unverified'
        baseline['manifest_sha256'] = None
        return prepare_context(project, envelope, baseline)
    except (ValueError, KeyError, TypeError, FileReadError) as exc:
        raise FileReadError('shared_knowledge_invalid', '共享知识清单或其原始 Git 证据无法核对；未采纳为已确认知识。') from exc


def portable(snapshot):
    value = deepcopy(snapshot)
    value.pop('manifest_sha256', None)
    value.pop('parent_snapshot', None)  # Private snapshots include source bodies.
    for claim in value['claims'].values():
        # No private request/receipt data or raw source bodies are shared.
        for proof in claim['fact']['evidence']:
            proof.pop('lines', None)
            text = proof.pop('text', '')
            proof['text_sha256'] = hashlib.sha256(text.encode()).hexdigest()
    return value


def publish_snapshot(state_dir, project, dossier, ledger, hashes):
    from .documents import _parents, _create
    from .initialization import atomic
    snapshot = deepcopy(ledger.value.get('incremental', {}).get('snapshot'))
    if snapshot is None:
        snapshot = seed_snapshot(state_dir, project, dossier, ledger)
    else:
        rows = {p['spec']['slug']: p for p in snapshot['pages']}
        for page in ledger.value['pages']:
            row = rows[page['spec']['slug']]
            row['status'] = page['status']
            row['evidence_checked_commit'] = dossier['commit']
            if page['status'] == 'completed':
                row.update(last_generated_commit=dossier['commit'], last_verified_commit=dossier['commit'],
                           published_sha256=page['published_sha256'], source_run_id=dossier['run_id'],
                           claim_bindings=page_bindings(page['spec'], snapshot['claims']))
        snapshot.update(run_id=dossier['run_id'], commit=dossier['commit'])
    snapshot['coverage_gaps'] = ledger.value.get('plan', {}).get('coverage_gaps', snapshot.get('coverage_gaps', []))
    root = Path(project['root_path']); _parents(root, MANIFEST)
    path = root / MANIFEST
    raw = encode(portable(snapshot)); sha = hashlib.sha256(raw).hexdigest()
    from .files import MAX_FILE_BYTES
    if len(raw) > MAX_FILE_BYTES:
        raise FileReadError('shared_manifest_budget', '共享清单超过安全文件读取容量，尚未发布。')
    previous = hashes.get(MANIFEST) or ledger.value.get('manifest_sha256')
    conflict = None
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() not in (sha, previous):
        conflict = MANIFEST  # Foreign or edited manifests never grant write rights.
        candidate = '.zest/docs/generated/candidates/' + dossier['run_id'] + '.json'
        _parents(root, candidate)
        destination = root / candidate
        expected = hashes.get(candidate) or ledger.value.get('manifest_candidate_sha256')
        if not destination.exists():
            _create(root, candidate, raw)
        elif hashlib.sha256(destination.read_bytes()).hexdigest() in (sha, expected):
            if destination.read_bytes() != raw: atomic(destination, raw)
        else:
            raise FileReadError('knowledge_candidate_conflict', '共享清单候选已有人工改动，已保留。')
        hashes[candidate] = sha
        ledger.value.update(manifest_candidate=candidate, manifest_candidate_sha256=sha)
    else:
        if path.exists():
            if path.read_bytes() != raw: atomic(path, raw)
        else:
            _create(root, MANIFEST, raw)
        hashes[MANIFEST] = sha
        ledger.value['manifest_sha256'] = sha
        ledger.save()
    snapshot['manifest_sha256'] = sha if not conflict else previous
    save_snapshot(state_dir, dossier['run_id'], snapshot)
    ledger.value['knowledge_summary'] = {status: sum(c['status'] == status for c in snapshot['claims'].values())
                                         for status in ('verified', 'inherited', 'needs_review', 'unresolved', 'retracted')}
    ledger.save()
    return conflict
