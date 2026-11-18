import unittest
from numeric_qualifiers import validate_numeric_evidence


class NumericEvidenceTests(unittest.TestCase):
    def check(self, summary, source):
        validate_numeric_evidence(summary, source)

    def test_invented_value_and_changed_unit_are_rejected(self):
        for summary, source in [
            ('Revenue grew over 99%.', 'Revenue grew over 15%.'),
            ('Revenue was $15 billion.', 'Revenue was $15 million.'),
            ('Revenue was 15亿元。', 'Revenue was 15万元。'),
            ('The rate fell 50%.', 'The rate fell 50 basis points.'),
            ('Net income was $5 million.', 'Net income was ($5 million).'),
            ('Net income was $5 million.', 'Net loss was $5 million.'),
            ('Revenue grew 20%.', 'Revenue grew over twenty percent.'),
        ]:
            with self.subTest(summary=summary), self.assertRaises(ValueError):
                self.check(summary, source)

    def test_equivalent_values_and_units_pass(self):
        for summary, source in [
            ('Revenue was $1.5 billion.', 'Revenue was $1,500 million.'),
            ('营收为1.5亿元。', '营收为15000万元。'),
            ('The rate fell 0.5 percentage points.', 'The rate fell 50 basis points.'),
            ('The rate fell 0.5 percentage points.', 'The rate fell 1/2 percentage point.'),
            ('Net income was -$5 million.', 'Net income was ($5 million).'),
            ('Revenue was $15m.', 'Revenue was $15 million.'),
            ('Revenue was $15 million.', 'Revenue was 15 million U.S. dollars.'),
            ('The target range is 0.25% to 0.5%.', 'The target range is 1/4 to 1/2 percent.'),
            ('Net income was -$5 million.', 'Net loss was $5 million.'),
            ('The company signed a 3-year agreement.', 'The company signed a three-year agreement.'),
            ('The company reported turnover growth of 50%.', 'Turnover grew 50%.'),
        ]:
            with self.subTest(summary=summary):
                self.check(summary, source)

    def test_rounding_requires_explicit_approximation(self):
        self.check('Revenue was about $1.2 billion.', 'Revenue was $1.23 billion.')
        with self.assertRaises(ValueError):
            self.check('Revenue was $1.2 billion.', 'Revenue was $1.23 billion.')

    def test_changed_metric_report_year_and_direction_are_rejected(self):
        for summary, source in [
            ('Net income was $15 million.', 'Revenue was $15 million. Net income was $3 million.'),
            ('In 2022, revenue was $15 million.', 'In 2021, revenue was $15 million. In 2022, revenue was $20 million.'),
            ('Revenue fell 15%.', 'Revenue increased 15%.'),
            ('In 2021Q2, revenue was $15 million.', 'In 2021Q1, revenue was $15 million. In 2021Q2, revenue was $20 million.'),
            ('Net income was 15.', 'Revenue was 15. Net income was 3.'),
        ]:
            with self.subTest(summary=summary), self.assertRaises(ValueError):
                self.check(summary, source)

    def test_explicit_company_attribution_is_checked(self):
        entities=[{'name':'Alpha Bank'}, {'name':'Beta Bank'}]
        source='Alpha Bank revenue was $15 million. Beta Bank revenue was $99 million.'
        validate_numeric_evidence('Alpha Bank revenue was $15 million.', source, entities)
        with self.assertRaises(ValueError):
            validate_numeric_evidence('Alpha Bank revenue was $99 million.', source, entities)

    def test_numbers_cannot_be_borrowed_from_another_metric(self):
        with self.assertRaises(ValueError):
            self.check('Revenue grew 15%.', 'Revenue was $15 million. Net income grew 15%.')

    def test_dates_years_and_stock_codes_are_not_negative_amounts(self):
        self.check('On 2022-05-25, Bank 600000 reported revenue growth of 15%.',
                   'On 2022-05-25, Bank 600000 reported revenue growth of 15%.')

    def test_comparison_year_does_not_taint_the_next_sentence(self):
        self.check('In 2026 revenue was $12 million, up 8%. Battery costs rose 5%.',
                   'In 2026 revenue was $12 million, up 8% from the same period in 2025. Battery costs rose 5%.')

    def test_crawler_defers_bad_numbers_then_saves_corrected_record(self):
        import json
        import tempfile
        from pathlib import Path
        import httpx
        from qwen_agent import Qwen
        from simple_crawler import Crawler
        from test_simple_crawler import config,candidate,FakeSources
        class Pages(FakeSources):
            def fetch(self,item):
                self.fetched+=1
                return dict(url=item['url'],title='Example Bank results',origin_market='CN',
                    text='Example Bank reported revenue growth of 15 percent during 2026.',
                    date_context='datePublished=2026-11-02',article_binding_version=4)
        value={'number':99}
        def reply(request):
            payload=json.loads(request.content)
            if 'action' in payload['format']['properties']:
                output={'action':'fetch','reason':'Financial operating information'}
            else:
                output=dict(useful=True,market='CN',category='Operating results',industry='Banking',
                    securities=[],industries=[],impact_scope=['company'],keywords=['Example Bank'],reason='Revenue growth',
                    content=f"Example Bank reported revenue growth of {value['number']} percent during 2026.")
            return httpx.Response(200,json={'message':{'content':json.dumps(output)}})
        parent=Path(__file__).resolve().parents[1]/'.runtime/test_numeric_evidence'
        parent.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as directory:
            root=Path(directory);cfg=config()
            client=httpx.Client(base_url='http://localhost',transport=httpx.MockTransport(reply))
            model=Qwen(cfg.ollama,root,client);model.model_digest='offline-numeric'
            pages=Pages();crawler=Crawler(cfg,root,pages,model)
            try:
                crawler.queue(candidate('numeric-evidence'))
                bad=crawler.run(resume=True)
                self.assertEqual(bad['saved'],0);self.assertEqual(bad['errors'][0]['reason_code'],'model_error')
                self.assertEqual(len(crawler.state['pending']),1)
                value['number']=15
                model.extraction_fingerprint+='-corrected-offline-revision'
                good=crawler.run(resume=True)
                self.assertEqual(good['saved'],1);self.assertEqual(pages.fetched,1)
                path=next((root/'data/CN').glob('*.json'))
                row=json.loads(path.read_text(encoding='utf-8'))
                self.assertEqual(row['时间'],'2026-11-02');self.assertEqual(len(row),11)
                audit=json.loads((root/'.runtime/simple/audit'/(row['source_id']+'.json')).read_text(encoding='utf-8'))
                self.assertEqual(audit['numeric_evidence_version'],'numeric-evidence-1')
                self.assertEqual(audit['raw_cleanup_status'],'deleted')
            finally:crawler.close()


if __name__ == '__main__':
    unittest.main()
