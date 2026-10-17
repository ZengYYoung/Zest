"""Keep valid scored targets; missing or rejected targets never become scores."""
from collections import Counter
import json

from .cognition_contracts import parse_response
from .evaluation_contracts import validate_evaluation


def receive_evaluation(receipt, payload):
    from .model_outputs import CognitionEvaluationResponse
    output = {'coverage': [], 'judgments': []}
    rejected, defaulted = [], []
    try:
        if receipt.get('raw_response') is not None:
            # The SDK still uses with_structured_output. A complete safe receipt
            # permits per-item named validation when whole-response parsing fails.
            snapshot = receipt.get('raw_response') or {}
            calls = snapshot.get('tool_calls', [])
            if snapshot.get('invalid_tool_calls') or len(calls) != 1 or calls[0]['name'] != 'CognitionEvaluationResponse':
                raise ValueError('no single valid structured tool call')
            raw = parse_response({'text': json.dumps(calls[0]['args'], ensure_ascii=False)})
        else:
            raw = parse_response(receipt)
    except Exception:
        raw = {}
        rejected.append({'kind': 'response', 'reason': 'unreadable_structured_response'})
    for field, target_field, id_field in (('coverage', 'requirements', 'id'), ('judgments', 'findings', 'finding_id')):
        targets = {r[id_field]: r for r in payload[target_field]}
        rows = raw.get(field, [])
        if field not in raw or rows is None:
            rows = []; defaulted.append({'field': field, 'value': 'empty_array'})
        if isinstance(rows, dict):
            rows = [rows]; defaulted.append({'field': field, 'value': 'singleton_array'})
        if not isinstance(rows, list):
            rejected.append({'kind': field, 'reason': 'invalid_list'})
            continue
        counts = Counter(r.get(id_field) for r in rows if isinstance(r, dict) and isinstance(r.get(id_field), str))
        for index, row in enumerate(rows):
            identity = row.get(id_field) if isinstance(row, dict) else None
            if not isinstance(identity, str) or identity not in targets or counts[identity] != 1:
                rejected.append({'kind': field, 'index': index, 'reason': 'invalid_or_duplicate_target'})
                continue
            try:
                normalized = CognitionEvaluationResponse.model_validate({field: [row]}).model_dump(mode='json')
                selected = {**payload, 'requirements': [], 'findings': []}
                selected[target_field] = [targets[identity]]
                if field == 'judgments':
                    selected['other_findings'] = [*payload['other_findings'],
                        *(f for f in payload['findings'] if f['finding_id'] != identity)]
                value = validate_evaluation(normalized, selected)[field][0]
            except Exception:
                rejected.append({'kind': field, 'id': identity, 'reason': 'invalid_score_or_reference'})
                continue
            output[field].append(value)
            if field == 'coverage':
                for name in ('finding_ids', 'evidence_ids', 'candidate_observation', 'reason'):
                    if name not in row: defaulted.append({'id': identity, 'field': name, 'value': 'empty'})
                    elif row[name] != value[name]: defaulted.append({'id': identity, 'field': name, 'value': 'normalized'})
    missing_coverage = [r['id'] for r in payload['requirements'] if r['id'] not in {c['id'] for c in output['coverage']}]
    missing_judgments = [f['finding_id'] for f in payload['findings'] if f['finding_id'] not in {j['finding_id'] for j in output['judgments']}]
    output['reception'] = {'complete': not missing_coverage and not missing_judgments and not rejected,
                           'missing_coverage_ids': missing_coverage, 'missing_judgment_ids': missing_judgments,
                           'rejected_items': rejected, 'defaulted_fields': defaulted}
    return output
