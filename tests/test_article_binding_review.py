import json
import unittest
from web_sources import read_html, usable_page_cache


class ArticleBindingReviewTests(unittest.TestCase):
    def page(self, items, heading='Company results and outlook'):
        raw='<title>'+heading+' | Publisher</title><article><h1>'+heading+'</h1><p>Visible main article.</p></article>'
        raw+='<script type="application/ld+json">'+json.dumps(items)+'</script>'
        return read_html(raw.encode(), 'https://example.com/2021/article/12345', False)

    def article(self, **extra):
        return dict({'@type':'NewsArticle','headline':'Company results and outlook',
            'articleBody':'MAIN earnings report with original financial and operating details. '*3,
            'datePublished':'2021-06-17'}, **extra)

    def test_exact_url_defeats_prefix_title_from_other_article(self):
        main=self.article(url='https://example.com/2021/article/12345')
        other=self.article(url='https://example.com/2026/article/99999',headline='Company results',
            articleBody='FUTURE bankrupt company recommended story. '*4,datePublished='2026-01-01')
        page=self.page([main,other])
        self.assertIn('MAIN',page['text']);self.assertNotIn('FUTURE',page['text'])
        self.assertNotIn('2026',page['date_context'])

    def test_title_fallback_requires_equality_and_no_conflicting_url(self):
        for other in [
            self.article(headline='Company results',articleBody='FUTURE recommended story. '*5),
            self.article(url='https://example.com/other',articleBody='FUTURE recommended story. '*5)
        ]:
            with self.subTest(other=other):
                page=self.page([self.article(),other])
                self.assertIn('MAIN',page['text']);self.assertNotIn('FUTURE',page['text'])

    def test_year_path_component_is_not_article_id(self):
        other=self.article(id=2021,headline='Unrelated title',articleBody='FUTURE unrelated story. '*5)
        self.assertNotIn('FUTURE',self.page([other])['text'])

    def test_old_matched_cache_is_invalidated(self):
        page=self.page([self.article()]);page['url']='https://example.com/2021/article/12345'
        self.assertTrue(usable_page_cache(page,page['url']))
        page['article_binding_version']=3
        self.assertFalse(usable_page_cache(page,page['url']))


if __name__=='__main__':unittest.main()
