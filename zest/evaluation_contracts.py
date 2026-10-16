"""Frozen sample-specific references and deterministic evaluation accounting."""
import hashlib
import re

from .cognition_contracts import canonical, items, keys, strings, text
from .cognition_context import compact_proof, review_payload, review_unknowns
from .cognition_review import validate_judgments
from .files import FileReadError

CATEGORIES = ('facts', 'flows', 'unknowns')


def criteria_input(value, commit):
    keys(value, ('version', 'commit', 'reference_phase', 'items', 'thresholds'))
    text(canonical(value), 512000)
    if type(value['version']) is not int or value['version'] != 1 or value['commit'] != commit:
        raise FileReadError('invalid_evaluation_criteria', '评分参考版本或 commit 不匹配。')
    if value['reference_phase'] not in ('pre_run_declared', 'retrospective'):
        raise FileReadError('invalid_evaluation_criteria', '须声明参考为事前冻结或事后补建；声明本身不证明时间。')
    ids, attributes, counts = set(), [], dict.fromkeys(CATEGORIES, 0)
    for item in items(value['items'], 200, minimum=1):
        keys(item, ('id', 'category', 'core', 'attributes'))
        identity = item['id']
        if not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', identity) or identity in ids:
            raise FileReadError('invalid_evaluation_criteria', '参考项身份须唯一，使用字母开头的短标识。')
        ids.add(identity)
        if item['category'] not in CATEGORIES or type(item['core']) is not bool:
            raise FileReadError('invalid_evaluation_criteria', '参考项类别或核心标志无效。')
        counts[item['category']] += 1
        seen = set()
        for attribute in items(item['attributes'], 30, minimum=1):
            keys(attribute, ('id', 'statement', 'sources'))
            aid = attribute['id']
            if not isinstance(aid, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', aid) or aid in seen:
                raise FileReadError('invalid_evaluation_criteria', '参考属性身份须在项内唯一。')
            seen.add(aid); text(attribute['statement'], 1600)
            items(attribute['sources'], 30, minimum=1)
            attributes.append({'id': identity + '/' + aid, 'statement': attribute['statement'], 'sources': attribute['sources'], 'category': item['category']})
    thresholds = value['thresholds']
    keys(thresholds, (*CATEGORIES, 'all_core', 'max_flagged_auto_reviewed_findings'))
    for category in CATEGORIES:
        n = thresholds[category]
        if type(n) is not int or not 0 <= n <= counts[category]:
            raise FileReadError('invalid_evaluation_criteria', '覆盖门槛须位于本类别参考项范围内。')
    if type(thresholds['all_core']) is not bool or type(thresholds['max_flagged_auto_reviewed_findings']) is not int or thresholds['max_flagged_auto_reviewed_findings'] < 0:
        raise FileReadError('invalid_evaluation_criteria', '核心或误收门槛无效。')
    return attributes


def original_proof(files, location):
    keys(location, ('source_id', 'start_line', 'end_line'))
    source = next((s for s in files if s['id'] == location['source_id']), None)
    start, end = location['start_line'], location['end_line']
    if source is None or type(start) is not int or type(end) is not int:
        raise FileReadError('invalid_evaluation_source', '评分来源必须定位到固定包原文。')
    ev = source['evidence']
    if hashlib.sha256(source['body'].encode()).hexdigest() != ev['content_sha256']:
        raise FileReadError('invalid_evaluation_source', '评分原文哈希不匹配。')
    lines = source['body'].split('\n')
    if lines[-1] == '': lines.pop()
    if not 1 <= start <= end <= len(lines):
        raise FileReadError('invalid_evaluation_source', '评分原文行段越界。')
    body = '\n'.join(line.removesuffix('\r') for line in lines[start-1:end])
    if not body.strip():
        raise FileReadError('invalid_evaluation_source', '空原文不能作为评分依据。')
    return {'source_id': source['id'], 'evidence': {**ev, 'start_line': start, 'end_line': end}, 'text': body}


def evaluation_units(record, criteria):
    attributes = criteria_input(criteria, record['commit'])
    files = record['envelope']['files']
    # Reverify all saved proofs against the frozen native bytes, without touching
    # the current sample worktree or trusting an old review's supported boolean.
    observed = [f for f in record['findings'] if f['finding']['kind'] == 'observed']
    for finding in observed:
        for proof in finding['finding']['evidence']:
            expected = original_proof(files, {'source_id': proof['source_id'], **{k: proof['evidence'][k] for k in ('start_line', 'end_line')}})
            if expected['evidence'] != proof['evidence'] or expected['text'] != proof['text']:
                raise FileReadError('invalid_evaluation_source', '候选结论保存的原文与固定包不一致。')
    unknowns = review_unknowns(record['jobs'])
    statements = [{'finding_id': f['id'], 'kind': f['finding']['kind'], 'statement': f['finding']['statement']} for f in record['findings']]
    published = [item for item in statements if any(f['id'] == item['finding_id'] and f['status'] == 'auto_reviewed' and f['finding']['kind'] == 'observed' for f in observed)]
    units = []
    for attribute in attributes:
        proofs = [original_proof(files, location) for location in attribute['sources']]
        evidence = {f'P{i+1}': compact_proof(proof) for i, proof in enumerate(proofs)}
        candidates = published + ([{'finding_id': u['reference_id'], 'kind': 'unknown', 'statement': u['statement']} for u in unknowns] if attribute['category'] == 'unknowns' else [])
        units.append({'kind': 'coverage', 'payload': {'commit': record['commit'],
            'requirements': [{'id': attribute['id'], 'statement': attribute['statement'], 'evidence_ids': list(evidence)}],
            'candidates': candidates, 'evidence': evidence, 'findings': [], 'other_findings': [], 'report_unknowns': []}})
    for finding in observed:
        other = [s for s in statements if s['finding_id'] != finding['id']]
        payload = review_payload(record['commit'], [finding], other, unknowns)
        payload.update(requirements=[], candidates=[])
        units.append({'kind': 'semantics', 'payload': payload})
    return units


def validate_evaluation(response, payload):
    keys(response, ('coverage', 'judgments'))
    expected = {r['id']: r for r in payload['requirements']}
    for judgment in items(response['coverage'], len(expected), minimum=len(expected)):
        keys(judgment, ('id', 'finding_ids', 'candidate_observation', 'evidence_ids', 'verdict', 'reason'))
        identity = judgment['id']
        if not isinstance(identity, str) or identity not in expected:
            raise FileReadError('invalid_evaluation_output', '评分参考身份重复或无效。')
        requirement = expected.pop(identity)
        if judgment['verdict'] not in ('full', 'partial', 'missing', 'reference_unsupported'):
            raise FileReadError('invalid_evaluation_output', '覆盖判定无效。')
        matched = judgment['verdict'] in ('full', 'partial')
        strings(judgment['finding_ids'], None, allowed={f['finding_id'] for f in payload['candidates']}, minimum=1 if matched else 0)
        strings(judgment['evidence_ids'], None, allowed=set(requirement['evidence_ids']), minimum=1 if matched else 0)
        text(judgment['candidate_observation'], 1600)
        text(judgment['reason'], 1600)
    return {'coverage': response['coverage'], 'judgments': validate_judgments(
        response['judgments'], payload, require_evidence_observation=True)}


def aggregate(envelope, results):
    coverage, judgments = {}, {}
    for result in results:
        coverage.update({c['id']: c for c in result['coverage']})
        judgments.update({j['finding_id']: j for j in result['judgments']})
    references, counts = [], {c: {'full': 0, 'total': 0} for c in CATEGORIES}
    core_passed = True
    for item in envelope['criteria']['items']:
        attributes = [coverage.get(item['id'] + '/' + a['id']) for a in item['attributes']]
        full = all(a and a['verdict'] == 'full' for a in attributes)
        invalid = any(a and a['verdict'] == 'reference_unsupported' for a in attributes)
        counts[item['category']]['total'] += 1
        counts[item['category']]['full'] += int(full)
        if item['core'] and not full: core_passed = False
        references.append({'id': item['id'], 'full': full, 'reference_unsupported': invalid, 'attributes': attributes})
    observed = envelope['snapshot']['findings']
    flagged, occurrences, auto_occurrences, untrusted_auto = [], 0, 0, 0
    for finding in observed:
        judgment = judgments.get(finding['id'])
        if not judgment: continue
        occurrences += len(judgment['checks'])
        if finding['status'] == 'auto_reviewed':
            auto_occurrences += len(judgment['checks'])
            untrusted_auto += sum(c['verdict'] != 'supported' for c in judgment['checks'])
            if not judgment['supported']: flagged.append(finding['id'])
    # Conflicts are symmetric for scoring too, without mutating source knowledge.
    for identity, judgment in judgments.items():
        for conflict in judgment['conflicts']:
            target = conflict['reference_id']
            if any(f['id'] == target and f['status'] == 'auto_reviewed' for f in observed) and target not in flagged:
                flagged.append(target)
    complete = len(results) == len(envelope['batches'])
    thresholds = envelope['criteria']['thresholds']
    gates = {c: counts[c]['full'] >= thresholds[c] for c in CATEGORIES}
    gates.update(core=not thresholds['all_core'] or core_passed,
                 automatic_review=len(flagged) <= thresholds['max_flagged_auto_reviewed_findings'])
    invalid_reference = any(r['reference_unsupported'] for r in references)
    return {'status': 'completed' if complete else 'partial', 'coverage': counts, 'references': references,
            'judgments': list(judgments.values()), 'flagged_auto_reviewed_findings': sorted(flagged),
            'atomic_occurrences': occurrences, 'auto_reviewed_atomic_occurrences': auto_occurrences,
            'untrusted_auto_reviewed_atomic_occurrences': untrusted_auto,
            'reviewed_observed_findings': len(judgments), 'total_observed_findings': len(observed),
            'gates': gates, 'reference_issue': invalid_reference,
            'overall_passed': complete and not invalid_reference and all(gates.values()),
            'limitations': ['model_judge_not_human_gold', 'atomic_occurrences_not_semantically_deduplicated',
                            'missing_batches_not_passed', 'declared_reference_phase_not_time_attestation']}


def merge_payloads(units):
    """Deduplicate original proofs, rebasing P identities without text loss."""
    proofs, identities, requirements, findings = {}, {}, [], []
    candidates, others, unknowns = {}, {}, {}
    for unit in units:
        p = unit['payload']; mapping = {}
        for ref, proof in p['evidence'].items():
            key = canonical(proof)
            if key not in identities:
                identities[key] = f'P{len(proofs)+1}'
                proofs[identities[key]] = proof
            mapping[ref] = identities[key]
        for r in p['requirements']:
            requirements.append({**r, 'evidence_ids': [mapping[rid] for rid in r['evidence_ids']]})
        for f in p['findings']:
            findings.append({**f, 'evidence_ids': [mapping[rid] for rid in f['evidence_ids']]})
        candidates.update({c['finding_id']: c for c in p['candidates']})
        others.update({c['finding_id']: c for c in p['other_findings']})
        unknowns.update({u['reference_id']: u for u in p['report_unknowns']})
    return {'commit': units[0]['payload']['commit'], 'requirements': requirements, 'findings': findings,
            'evidence': proofs, 'candidates': list(candidates.values()),
            'other_findings': [v for k, v in others.items() if k not in {f['finding_id'] for f in findings}],
            'report_unknowns': list(unknowns.values())}
