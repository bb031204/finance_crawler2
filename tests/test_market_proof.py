import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from finance_agent.core import digest
from market_scope import broad_scope,ground_market,evidence_pack,validate_pack,quote_candidates,constrain_market_schema
from market_relations import preserve_relations


class MarketProofTests(unittest.TestCase):
    def note(self,quote):
        return {'content':quote,'market_scope':['US_EQUITY'],'scope_evidence':[{
            'scope':'US_EQUITY','evidence_quote':quote,'rationale':'Monetary policy affects economy-wide financing conditions.','confidence':.95}]}

    def test_company_listing_never_exchange_scope(self):
        self.assertFalse(broad_scope('NASDAQ_LISTED','Example Inc. (NASDAQ: EX) reported quarterly earnings.'))
        self.assertFalse(broad_scope('US_EQUITY','Example Inc. increases its credit facility to $350 million.'))
        self.assertFalse(broad_scope('US_EQUITY','Example Bank is regulated by the Federal Reserve.'))

    def test_broad_exchange_rule(self):
        self.assertTrue(broad_scope('NASDAQ_LISTED','NASDAQ listing rules will apply to all listed companies.'))

    def test_fomc_evidence_and_proof(self):
        quote='The Federal Reserve raised the federal funds rate target range.'
        note=ground_market(self.note(quote),quote)
        self.assertEqual(note['market_scope'],['US_EQUITY'])
        self.assertTrue(note['scope_evidence'][0]['original_evidence_verified'])
        summary=ground_market(self.note(quote),quote,'stored_summary_only')
        self.assertFalse(summary['scope_evidence'][0]['original_evidence_verified'])

    def test_invented_or_low_confidence_rejected(self):
        quote='The Federal Reserve raised interest rates.'
        self.assertEqual(ground_market(self.note(quote),'Company earnings increased.')['market_scope'],[])
        note=self.note(quote);note['scope_evidence'][0]['confidence']=.5
        self.assertEqual(ground_market(note,quote)['market_scope'],[])

    def test_duplicate_scope_evidence_rejected(self):
        quote='The Federal Reserve raised interest rates.'
        note=self.note(quote);note['scope_evidence']*=2
        self.assertEqual(ground_market(note,quote)['market_scope'],[])

    def test_pack_detects_missing_quote_and_tamper(self):
        source='The Federal Reserve raised interest rates.'
        row={'source_id':'a','url':'https://example.com/a','时间':'2017-01-01','主要内容':source}
        audit=dict(record_hash=digest(row),source_body_hash=digest(source.encode()),events=[{'quote':source}])
        pack=evidence_pack(row,audit,source)
        self.assertTrue(validate_pack(pack,row,audit))
        pack['evidence']=[]
        with self.assertRaises(ValueError):validate_pack(pack,row,audit)
        audit['events']=[{'quote':'Invented evidence'}]
        with self.assertRaises(ValueError):evidence_pack(row,audit,source)

    def test_merge_preserves_market_evidence(self):
        quote='The Federal Reserve raised interest rates.'
        note=ground_market(self.note(quote),quote)
        final=preserve_relations({'content':'Rates changed.','securities':[],'industries':[]},[note],quote)
        self.assertEqual(final['market_scope'],['US_EQUITY'])

    def test_export_market_scope(self):
        from structured_export import project
        quote='The Federal Reserve raised interest rates.'
        row={'source_id':'a','url':'https://example.com/a','时间':'2017-01-01','主要内容':quote,'关键词':[]}
        note=ground_market(self.note(quote),quote)
        result=project([('US',row,note)])
        self.assertEqual(len(result['article_market_scopes']),1)

    def test_quote_choices_are_literal_and_bounded(self):
        source='A company reported quarterly earnings. The Federal Reserve raised interest rates. '+('银行公布财报。'*100)
        for limit in (180,350):
            choices=quote_candidates(source,limit)
            self.assertTrue(choices)
            self.assertTrue(all(q in source and 8<=len(q)<=limit for q in choices))

    def test_source_bound_decode_schema_for_company_and_fomc(self):
        def make():return {'properties':{'market_scope':{},'scope_evidence':{}}},{'properties':{}}
        schema,definition=make();constrain_market_schema(schema,'Example Bank reported earnings (NASDAQ: EX).',definition)
        self.assertEqual(schema['properties']['market_scope']['maxItems'],0)
        source='The Federal Reserve raised the federal funds rate target range.'
        schema,definition=make();constrain_market_schema(schema,source,definition)
        self.assertEqual(schema['properties']['market_scope']['items']['enum'],['US_EQUITY'])
        self.assertTrue(all(q in source for q in definition['properties']['evidence_quote']['enum']))


if __name__=='__main__':unittest.main()
