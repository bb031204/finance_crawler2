import unittest
from market_scope import broad_scope,ground_market
from industry_dictionary import IndustryDictionary

class ExpandedContextTests(unittest.TestCase):
    def test_industry_impact_cannot_emit_an_unusable_null_key(self):
        from offline_enrichment.retrace_news import schema
        impact=schema(['TRSR:bank'])['properties']['articles']['items']['properties']['industry_impacts']['items']
        self.assertEqual(impact['properties']['industry_key']['enum'],['TRSR:bank'])
        self.assertIn('industry_key',impact['required'])
        self.assertIn('industry_scope_quote',impact['required'])
        self.assertIn('confidence',impact['required'])

    def test_evidenced_macro_context_is_candidate_but_company_listing_is_not(self):
        for quote in ['The United States announced new tariffs on imported goods.',
                      'Global financial markets fell after the policy announcement.',
                      'US credit markets tightened as bond yields rose.']:
            self.assertTrue(broad_scope('US_EQUITY',quote),quote)
        for quote in ['Example Company (NASDAQ:ABC) reported strong quarterly earnings.',
                      'A bank is regulated by the Federal Reserve.']:
            self.assertFalse(broad_scope('US_EQUITY',quote),quote)
            self.assertFalse(broad_scope('NASDAQ_LISTED',quote),quote)

    def test_balanced_recall_does_not_starve_dataset_taxonomies(self):
        values=[(-100+i,str(i),{'industry_standard':'SEC-SIC'}) for i in range(20)]
        values.extend([(1,'trsr',{'industry_standard':'TRSR-INDUSTRY'}),(2,'sp',{'industry_standard':'SP500-SECTOR'})])
        got=IndustryDictionary.balanced(values,8,['SEC-SIC','TRSR-INDUSTRY','SP500-SECTOR'])
        self.assertEqual(len(got),8)
        self.assertIn('trsr',[r[1] for r in got]);self.assertIn('sp',[r[1] for r in got])

    def test_threshold_does_not_remove_literal_evidence_requirement(self):
        quote='US credit markets tightened as bond yields rose.'
        def note():return dict(content=quote,market_scope=['US_EQUITY'],scope_evidence=[dict(scope='US_EQUITY',evidence_quote=quote,rationale='Higher financing costs affect US risk assets.',confidence=.8)])
        self.assertTrue(ground_market(note(),quote,minimum=.75)['market_scope'])
        self.assertFalse(ground_market(note(),quote,minimum=.85)['market_scope'])
        self.assertFalse(ground_market(note(),'Company revenue increased.',minimum=.75)['market_scope'])
