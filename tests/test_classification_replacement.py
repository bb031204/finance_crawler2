import copy
import unittest
from classification_projection import CONTRACT,ground_classification,replace_classification
from market_scope import evidence_pack,validate_pack
from finance_agent.core import digest


class ClassificationReplacementTests(unittest.TestCase):
    def row(self):
        return {'source_id':'a','url':'https://example.test/a','title':None,'时间':'2017-01-01',
            '主要内容':'Acme reported its own quarterly profit.',
            '涉及行业':[{'名称':'wrong old sector','依据':'legacy_metadata'}],
            '影响范围':['company','industry','market']}

    def note(self):
        return {'content':self.row()['主要内容'],'classification_contract':CONTRACT,
            'impact_scope':['company'],'impact_scope_evidence':[{'scope':'company',
                'quote':self.row()['主要内容'],'rationale':'The report describes only Acme earnings.','confidence':.9}],
            'industries':[],'industry_impacts':[],'market_scope':[],'scope_evidence':[]}

    def test_empty_fresh_result_clears_wrong_legacy_labels(self):
        old=self.row();note=self.note();note.update(impact_scope=[],impact_scope_evidence=[])
        new,audit=replace_classification(old,note)
        self.assertEqual(new['涉及行业'],[]);self.assertEqual(new['影响范围'],[])
        self.assertEqual(old,self.row())
        self.assertEqual(audit['classification_replacement']['previous_industries'],old['涉及行业'])

    def test_company_only_does_not_keep_old_industry_or_market(self):
        note=ground_classification(self.note(),self.row()['主要内容'],'stored_summary_only',.75)
        new,_=replace_classification(self.row(),note)
        self.assertEqual(new['影响范围'],['company'])

    def test_fabricated_or_low_confidence_scope_quote_is_omitted(self):
        for quote,score in [('Invented description of company profits.',.99),(self.row()['主要内容'],.5)]:
            note=self.note();note['impact_scope_evidence'][0].update(quote=quote,confidence=score)
            new=ground_classification(note,self.row()['主要内容'],'stored_summary_only',.75)
            self.assertEqual(new['impact_scope'],[])

    def test_industry_mention_alone_does_not_create_industry_scope(self):
        note=self.note();note['impact_scope']=['industry'];note['impact_scope_evidence'][0]['scope']='industry'
        self.assertEqual(ground_classification(note,self.row()['主要内容'],'stored_summary_only',.75)['impact_scope'],[])

    def test_typed_market_alone_cannot_override_general_scope_redecision(self):
        note=self.note();note.update(market_scope=['US_EQUITY'],scope_evidence=[{'scope':'US_EQUITY'}])
        new=ground_classification(note,self.row()['主要内容'],'stored_summary_only',.75)
        self.assertEqual(new['market_scope'],[]);self.assertEqual(new['scope_evidence'],[])

    def test_market_scope_requires_a_verified_typed_scope(self):
        note=self.note();note['impact_scope']=['market'];note['impact_scope_evidence'][0]['scope']='market'
        self.assertEqual(ground_classification(note,self.row()['主要内容'],'stored_summary_only',.75)['impact_scope'],[])

    def test_unselected_industry_cannot_propagate(self):
        note=self.note();note['industry_impacts']=[{'propagation_approved':True}]
        new=ground_classification(note,self.row()['主要内容'],'stored_summary_only',.75)
        self.assertFalse(new['industry_impacts'][0]['propagation_approved'])

    def test_dictionary_key_fills_standard_name_and_preserves_original_evidence(self):
        note=self.note();note['industries']=[{'name':'banks','industry_name_standard':'STATE COMMERCIAL BANKS',
            'industry_key':'SEC-SIC:6022','dictionary_sha256':'pinned','quote':'banks','basis':'inferred'}]
        new,audit=replace_classification(self.row(),note)
        self.assertEqual(new['涉及行业'],[{'名称':'STATE COMMERCIAL BANKS','依据':'inferred'}])
        self.assertEqual(audit['industries'][0]['name'],'banks')
        self.assertNotIn('wrong old sector',str(new['涉及行业']))

    def test_old_schema_cannot_be_silently_promoted(self):
        note=self.note();note.pop('classification_contract')
        with self.assertRaisesRegex(ValueError,'fresh_classification_required'):replace_classification(self.row(),note)

    def test_general_scope_evidence_is_in_hash_bound_pack(self):
        row=self.row();note=self.note();note.update(record_hash=digest(row),source_body_hash=digest(row['主要内容'].encode()))
        pack=evidence_pack(row,note,row['主要内容'])
        self.assertEqual(pack['evidence'][0]['field'],'impact_scope_evidence')
        self.assertTrue(validate_pack(pack,row,note))
        tampered=copy.deepcopy(note);tampered['impact_scope_evidence']=[]
        with self.assertRaisesRegex(ValueError,'incomplete'):validate_pack(pack,row,tampered)

    def test_duplicate_quotes_cannot_discard_valid_scope_or_prefer_fabrication(self):
        note=self.note();note['impact_scope_evidence'].append(dict(note['impact_scope_evidence'][0],
            quote='Invented company earnings quotation.',confidence=.99))
        result=ground_classification(note,self.row()['主要内容'],'stored_summary_only',.75)
        self.assertEqual(result['impact_scope'],['company'])
        self.assertEqual(result['impact_scope_evidence'][0]['quote'],self.row()['主要内容'])

    def test_multiple_scopes_are_kept_only_with_both_evidence_gates(self):
        note=self.note();note['impact_scope']=['company','industry']
        note['impact_scope_evidence'].append(dict(note['impact_scope_evidence'][0],scope='industry'))
        note['industry_impacts']=[{'propagation_approved':True}]
        result=ground_classification(note,self.row()['主要内容'],'stored_summary_only',.75)
        self.assertEqual(result['impact_scope'],['company','industry'])

    def test_impact_only_code_has_foreign_key_without_duplicate_model_mention(self):
        from structured_export import project
        row=dict(self.row(),关键词=[],股票实体=[],分类='policy',时间信息={},涉及行业=[])
        impact={'industry_name_original':'banks','industry_key':'SEC-SIC:6022','industry_standard':'SEC-SIC',
            'industry_version':'v1','industry_code':'6022','industry_name_standard':'STATE COMMERCIAL BANKS',
            'event_index':None,'evidence_type':'inferred','quote':'banks','impact_role':'uncertain','direction':'uncertain',
            'mechanism':'Credit conditions affect funding costs.','horizon':'short','confidence':.9}
        tables=project([('US',row,{'industry_impacts':[impact]})])
        self.assertEqual(tables['article_industries'][0]['industry_code'],'6022')
        self.assertEqual(tables['article_industry_impacts'][0]['industry_code'],'6022')
