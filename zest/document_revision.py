"""Bounded evidence-bound revisions after known rejections, in the same budget."""
from .document_evidence import LAYOUT
from .files import FileReadError


def enabled(record):
    return record.get('document_continuation', {}).get('evidence_layout') == LAYOUT


def supported_projection(page, contract):
    from .document_pages import validate_page
    content, response = page.get('revised_content'), page.get('revision_review_response')
    if not content or not response or page.get('projection_review_response'):
        return None
    expected = {f'B{i}' for i in range(1, len(content['blocks']) + 1)}
    checks = response.get('checks', [])
    if len(checks) != len(expected) or {c['block_id'] for c in checks} != expected:
        return None
    supported = {c['block_id'] for c in checks if c['verdict'] == 'supported'}
    if not supported or supported == expected:
        return None
    candidate = {**content, 'blocks': [b for i, b in enumerate(content['blocks'], 1) if f'B{i}' in supported]}
    try:
        return validate_page(candidate, page['spec'], contract)
    except FileReadError:
        return None  # Never omit a planned fact just to make review pass.


def write_and_review(engine, ledger, page, payload, contract, envelope):
    from .document_pages import validate_page, validate_review
    actor = 'document-page-' + page['spec']['slug']

    def review_payload(content):
        return {**payload, 'blocks': [dict(b, block_id=f'B{i}') for i, b in enumerate(content['blocks'], 1)], 'limits': content['limits']}

    def revise():
        if 'revised_content' not in page:
            page['status'] = 'writing'; ledger.save()
            page['revised_content'] = validate_page(engine.call(ledger.run_id, actor, 2, 'document_write', page['revision_payload'], envelope), page['spec'], contract)
            ledger.save()
        if 'revised_review' not in page:
            page['status'] = 'reviewing'
            criteria = '仍须逐块核对所有断言和完整原文，并确认完整覆盖每条计划事实的重要内容，不能仅按 finding_ids 存在判定覆盖。issues 只列阻止发布的实质问题；无问题时必须为空数组。肯定评价、可选措辞建议放入 checks.reason，不作为 issues。清楚标为静态源码逻辑的说明不等于声称已运行；未标明的证据外推断仍应拒绝。'
            if 'projection_content' not in page:
                request = page.setdefault('revision_review_payload', {**review_payload(page['revised_content']), 'review_criteria': criteria})
                ledger.save()
                response = engine.call(ledger.run_id, actor, 3, 'document_review', request, envelope)
                page['revision_review_response'] = response; ledger.save()
                try:
                    page['revised_review'] = validate_review(response, page['revised_content'])
                except FileReadError as exc:
                    candidate = supported_projection(page, contract) if exc.code == 'document_review_rejected' else None
                    if candidate is None: raise
                    page['projection_content'] = candidate
                    page['projection_reason'] = '只移除明确未受支持且不影响计划事实覆盖的正文块，仍须独立复核全文。'
                    ledger.save()
            if 'projection_content' in page:
                request = page.setdefault('projection_review_payload', {**review_payload(page['projection_content']), 'review_criteria': criteria})
                ledger.save()
                response = engine.call(ledger.run_id, actor, 4, 'document_review', request, envelope)
                page['projection_review_response'] = response; ledger.save()
                try:
                    page['revised_review'] = validate_review(response, page['projection_content'])
                except FileReadError as exc:
                    if exc.code != 'document_review_rejected': raise
                    # One final targeted correction for a known newly exposed
                    # issue; identities 5/6 are fixed, so resume cannot loop.
                    request = page.setdefault('last_revision_payload', {**payload, 'revision_request': {
                        'previous_draft':page['projection_content'], 'review_feedback':response,
                        'goal':'只修正仍被明确指出的错误条件、错误因果和证据范围问题。逐字核对主体、条件、错误码及代词指代；不增加项目事实，不编造原文。保留已支持段落；给定事实与所附原文的支持范围有差异时如实说明限制，不把推断写成运行事实。所有计划事实仍须得到实质说明。这是本页最后一次修订。'}})
                    page['status']='writing'; ledger.save()
                    if 'last_content' not in page:
                        raw=engine.call(ledger.run_id,actor,5,'document_write',request,envelope)
                        page['last_write_response']=raw; ledger.save()
                        try:
                            page['last_content']=validate_page(raw,page['spec'],contract)
                        except FileReadError as exc:
                            if not isinstance(raw,dict) or not isinstance(raw.get('blocks'),list) or any(not isinstance(b,dict) for b in raw['blocks']):
                                page['coverage_revision_blocked']=True; ledger.save(); raise
                            covered={key for block in raw.get('blocks',[]) for key in block.get('finding_ids',[])}
                            required=set(page['spec']['finding_ids'])
                            if exc.code!='invalid_document_output' or not covered < required:
                                page['coverage_revision_blocked']=True; ledger.save(); raise
                            # Diagnose a coverage-only defect; publication still
                            # validates against the ORIGINAL complete fact set.
                            try: validate_page(raw,{**page['spec'],'finding_ids':sorted(covered)},contract)
                            except FileReadError:
                                page['coverage_revision_blocked']=True; ledger.save(); raise exc
                            request=page.setdefault('coverage_revision_payload',{**payload,'revision_request':{
                                'previous_draft':raw,'missing_finding_ids':sorted(required-covered),
                                'goal':'补足 missing_finding_ids 的实质说明与准确引用，不得删除其它计划事实。若给定记录的断言超出本页原文支持范围，单独说明该断言尚无法核实、原文实际支持到何处，并引用该事实 ID；不得机械挂 ID 或把不确定说成确定。保留已支持正文，不新增项目事实。'}})
                            ledger.save()
                            page['last_content']=validate_page(engine.call(ledger.run_id,actor,7,'document_write',request,envelope),page['spec'],contract)
                        ledger.save()
                    page['status']='reviewing'
                    request=page.setdefault('last_review_payload',{**review_payload(page['last_content']),'review_criteria':criteria})
                    ledger.save()
                    response=engine.call(ledger.run_id,actor,8 if 'coverage_revision_payload' in page else 6,'document_review',request,envelope)
                    page['last_review_response']=response; ledger.save()
                    try:
                        page['revised_review']=validate_review(response,page['last_content'])
                    except FileReadError as exc:
                        if exc.code!='document_review_rejected' or not response['checks'] or any(c['verdict']!='supported' for c in response['checks']):raise
                        from .document_evidence import SOURCE_NOTICE
                        request=page.setdefault('provenance_review_payload',{**review_payload(page['last_content']),
                            'review_criteria':criteria,'rendered_provenance':SOURCE_NOTICE})
                        ledger.save()
                        response=engine.call(ledger.run_id,actor,9,'document_review',request,envelope)
                        page['provenance_review_response']=response; ledger.save()
                        page['revised_review']=validate_review(response,page['last_content'])
        page['content'] = page.get('last_content', page.get('projection_content', page['revised_content'])); page['review'] = page['revised_review']

    if 'revision_payload' in page:
        revise()
        return
    if 'content' not in page:
        page['status'] = 'writing'; ledger.save()
        page['content'] = validate_page(engine.call(ledger.run_id, actor, 0, 'document_write', payload, envelope), page['spec'], contract)
        ledger.save()
    if 'review' not in page:
        page['status'] = 'reviewing'; ledger.save()
        response = engine.call(ledger.run_id, actor, 1, 'document_review', review_payload(page['content']), envelope)
        page['review_feedback'] = response; ledger.save()
        try:
            page['review'] = validate_review(response, page['content'])
        except FileReadError as exc:
            if exc.code != 'document_review_rejected' or not enabled({'document_continuation': envelope.get('document_continuation', {})}):
                raise
            page['revision_payload'] = {**payload, 'revision_request': {'previous_draft': page['content'], 'review_feedback': response,
                'goal': '仅依据同一批已核对事实与原文修订本页。删除或收窄反馈指出的证据外断言、无依据示例与过于绝对的使用建议；不要新增项目事实。检查每个块与 finding_ids 的实质对应关系。反馈中的肯定评价不需要改写。缺少资料如实说明，不把静态逻辑推断写成实测结果。'}}
            page['revision_number'] = 1; ledger.save()
            revise()
