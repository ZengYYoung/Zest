"""Deduplicate full source proofs in new document rounds without truncating them."""
from copy import deepcopy

from .cognition_store import digest
from .files import FileReadError

LAYOUT = 'shared_full_proofs_v1'
SOURCE_NOTICE = '本文的“给定事实”来自原调查的自动核对记录，不是本页重新调查所得的结论。原记录中的整体判断不等于本页对未展示调用链的重新确认；本页实际可核对范围以所附原文、正文和资料限制为准。'


def shared_proofs(payload):
    registry = {}

    def visit(value):
        if isinstance(value, list):
            return [visit(item) for item in value]
        if not isinstance(value, dict):
            return deepcopy(value)
        result = {}
        for key, item in value.items():
            if key == 'evidence' and isinstance(item, list) and item and all(
                    isinstance(p, dict) and isinstance(p.get('text'), str) and isinstance(p.get('evidence'), dict) for p in item):
                refs = []
                for proof in item:
                    identity = 'E' + digest(proof)
                    if identity in registry and registry[identity] != proof:
                        raise FileReadError('document_context_changed', '原文共享身份冲突，未发送请求。')
                    registry[identity] = deepcopy(proof)
                    refs.append({'proof_id': identity})
                result[key] = refs
            else:
                result[key] = visit(item)
        return result

    value = visit(payload)
    value['evidence_registry'] = registry
    value['evidence_delivery'] = ('findings 和 verification_history 内 evidence[].proof_id 指向 evidence_registry 中的完整原文与定位信息。'
                                  '同一原文只保存一份，内容没有截断；引用次数不代表独立证据。请按 proof_id 核对原文，再撰写或复核每项断言。')
    return value
