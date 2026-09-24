import unittest
import os
import requests
from unittest.mock import Mock, patch
from paper_search_mcp.academic_platforms.google_scholar import GoogleScholarSearcher

def check_scholar_accessible():
    """检查 Google Scholar 是否可访问"""
    try:
        response = requests.get("https://scholar.google.com", timeout=5)
        return response.status_code == 200
    except:
        return False

class TestGoogleScholarSearcher(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scholar_accessible = check_scholar_accessible()
        if not cls.scholar_accessible:
            print("\nWarning: Google Scholar is not accessible, some tests will be skipped")

    def setUp(self):
        self.searcher = GoogleScholarSearcher()

    def test_search(self):
        if not self.scholar_accessible:
            self.skipTest("Google Scholar is not accessible")
            
        papers = self.searcher.search("machine learning", max_results=5)
        print(f"\nFound {len(papers)} papers for query 'machine learning':")
        if len(papers) == 0:
            self.skipTest("Google Scholar returned 0 results (likely bot-detection/rate-limit)")

        for i, paper in enumerate(papers, 1):
            print(f"\n{i}. {paper.title}")
            print(f"   Authors: {', '.join(paper.authors)}")
            print(f"   Citations: {paper.citations}")
        self.assertTrue(len(papers) > 0)
        self.assertTrue(papers[0].title)

    def test_download_pdf_not_supported(self):
        with self.assertRaises(NotImplementedError):
            self.searcher.download_pdf("some_id", "./downloads")

    def test_read_paper_not_supported(self):
        message = self.searcher.read_paper("some_id")
        self.assertIn("Google Scholar doesn't support direct paper reading", message)

    def test_proxy_configuration(self):
        proxy_searcher = GoogleScholarSearcher(proxy_url="http://127.0.0.1:7890")
        self.assertEqual(proxy_searcher.session.proxies.get("http"), "http://127.0.0.1:7890")
        self.assertEqual(proxy_searcher.session.proxies.get("https"), "http://127.0.0.1:7890")

    def test_retry_configuration(self):
        retry_searcher = GoogleScholarSearcher(max_retries=5, retry_delay=3.0)
        self.assertEqual(retry_searcher.max_retries, 5)
        self.assertEqual(retry_searcher.retry_delay, 3.0)

    def test_serpapi_configuration_from_environment(self):
        with patch.dict(
            os.environ,
            {"PAPER_SEARCH_MCP_SERPAPI_API_KEY": "test-serpapi-key"},
        ):
            searcher = GoogleScholarSearcher()
        self.assertEqual(searcher.serpapi_api_key, "test-serpapi-key")

    def test_search_uses_serpapi_when_configured(self):
        searcher = GoogleScholarSearcher(serpapi_api_key="test-serpapi-key")
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "organic_results": [
                {
                    "result_id": "abc123",
                    "title": "A Useful Paper",
                    "link": "https://example.org/paper",
                    "snippet": "An abstract with doi:10.1000/test-doi",
                    "publication_info": {
                        "summary": "Ada Lovelace, Alan Turing - Journal, 2024",
                        "authors": [
                            {"name": "Ada Lovelace"},
                            {"name": "Alan Turing"},
                        ],
                    },
                    "inline_links": {"cited_by": {"total": 42}},
                    "resources": [
                        {
                            "file_format": "PDF",
                            "link": "https://example.org/paper.pdf",
                        }
                    ],
                }
            ]
        }
        searcher.session.get = Mock(return_value=response)

        papers = searcher.search("useful", max_results=5)

        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].paper_id, "gs_abc123")
        self.assertEqual(papers[0].authors, ["Ada Lovelace", "Alan Turing"])
        self.assertEqual(papers[0].citations, 42)
        self.assertEqual(papers[0].pdf_url, "https://example.org/paper.pdf")
        self.assertEqual(papers[0].doi, "10.1000/test-doi")
        _, kwargs = searcher.session.get.call_args
        self.assertEqual(kwargs["params"]["engine"], "google_scholar")
        self.assertEqual(kwargs["params"]["api_key"], "test-serpapi-key")

if __name__ == '__main__':
    unittest.main()
