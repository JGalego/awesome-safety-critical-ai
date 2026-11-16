"""Offline tests for scout.py. Run with: python3 -m unittest discover scripts"""

import unittest

from scout import Candidate, known_keys, normalize, parse_arxiv, parse_feed, parse_verdicts, render

ARXIV = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2610.01234v2</id>
    <published>2026-10-20T10:00:00Z</published>
    <title>A  Verified
      Thing</title>
    <summary>We write  proofs.</summary>
  </entry>
  <entry><id>http://example.com/not-arxiv</id><title>x</title></entry>
</feed>"""

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item><title>Post</title><link>https://blog.example/p</link>
  <pubDate>Mon, 02 Nov 2026 09:00:00 +0000</pubDate>
  <description>&lt;p&gt;Hello &lt;b&gt;proof&lt;/b&gt;&lt;/p&gt;</description></item>
</channel></rss>"""

ATOM_FEED = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><title>Entry</title><link href="https://blog.example/e"/>
  <updated>2026-11-03T08:00:00Z</updated><summary>Body</summary></entry>
</feed>"""


class NormalizeTests(unittest.TestCase):
    def test_arxiv_spellings_match(self):
        keys = {normalize(u) for u in (
            "https://arxiv.org/abs/2605.01660",
            "https://arxiv.org/pdf/2605.01660v3",
            "http://arxiv.org/abs/2605.01660v1",
        )}
        self.assertEqual(keys, {"arxiv:2605.01660"})

    def test_github_and_doi(self):
        self.assertEqual(normalize("https://github.com/Acme/Tool.git"), "github:acme/tool")
        self.assertEqual(normalize("https://github.com/acme/tool/tree/main"), "github:acme/tool")
        self.assertEqual(normalize("https://doi.org/10.1145/ABC"), "doi:10.1145/abc")
        self.assertEqual(normalize("https://www.Example.com/a/"), "example.com/a")

    def test_known_keys_reads_markdown_and_bare_urls(self):
        keys = known_keys("- [P](https://arxiv.org/abs/2601.00001) - x. See https://github.com/a/b.")
        self.assertEqual(keys, {"arxiv:2601.00001", "github:a/b"})


class ParseTests(unittest.TestCase):
    def test_arxiv_entries(self):
        cands = parse_arxiv(ARXIV)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].id, "arxiv:2610.01234")
        self.assertEqual(cands[0].title, "A Verified Thing")
        self.assertEqual(cands[0].summary, "We write proofs.")
        self.assertEqual(cands[0].url, "https://arxiv.org/abs/2610.01234")
        self.assertEqual(cands[0].date, "2026-10-20")

    def test_rss_and_atom(self):
        rss = parse_feed(RSS, "Blog")[0]
        self.assertEqual((rss.url, rss.date, rss.summary), ("https://blog.example/p", "2026-11-02", "Hello proof"))
        atom = parse_feed(ATOM_FEED, "Blog")[0]
        self.assertEqual((atom.url, atom.date), ("https://blog.example/e", "2026-11-03"))

    def test_bad_xml_is_empty(self):
        self.assertEqual(parse_arxiv("<not xml"), [])
        self.assertEqual(parse_feed("<not xml", "x"), [])


class VerdictTests(unittest.TestCase):
    def test_fenced_json(self):
        text = 'Sure!\n```json\n[{"id": "a", "keep": true, "reason": "has code"}, {"id": "b", "keep": false}]\n```'
        self.assertEqual(parse_verdicts(text), {"a": (True, "has code"), "b": (False, "")})

    def test_garbage_and_bad_types(self):
        self.assertEqual(parse_verdicts("no json"), {})
        self.assertEqual(parse_verdicts("[not json]"), {})
        self.assertEqual(parse_verdicts('[{"id": "a", "keep": "yes"}]'), {})


class RenderTests(unittest.TestCase):
    def test_groups_and_drops(self):
        cands = [
            Candidate("p", "Paper", "https://arxiv.org/abs/1", "paper", "s", "2026-11-01", "arXiv", True, "has code"),
            Candidate("q", "Slop", "https://x.example/q", "post", "s", "2026-11-02", "Blog", False, "filler"),
        ]
        out = render(cands, True, 1, 10, "m")
        self.assertIn("### Papers", out)
        self.assertIn("[Paper](https://arxiv.org/abs/1)", out)
        self.assertNotIn("Slop", out)
        self.assertNotIn("unfiltered", out)

    def test_unfiltered_warning(self):
        cands = [Candidate("p", "Paper", "https://arxiv.org/abs/1", "paper", "s", "2026-11-01", "arXiv")]
        self.assertIn("unfiltered", render(cands, False, 0, 10, "m"))



class ChatFallbackTests(unittest.TestCase):
    def setUp(self):
        import scout
        self.scout = scout
        self.calls = []

    def _fake(self, answers):
        def fake(url, headers=None, data=None, timeout=30, retry_on=(), pauses=()):
            import json as _json
            model = _json.loads(data)["model"]
            self.calls.append((model, pauses))
            answer = answers[model]
            if answer is None:
                return None
            return _json.dumps({"choices": [{"message": {"content": answer}}]}).encode()
        return fake

    def test_falls_back_when_the_first_model_is_busy(self):
        from unittest import mock
        with mock.patch.object(self.scout, "fetch", self._fake({"a": None, "b": "hello"})):
            self.assertEqual(self.scout.chat([], "k", "a, b"), "hello")
        self.assertEqual([m for m, _ in self.calls], ["a", "b"])
        # The first model gets a short wait because a fallback is available.
        self.assertEqual(self.calls[0][1], (15,))
        self.assertEqual(self.calls[1][1], (15, 45, 90))

    def test_stops_at_the_first_answer(self):
        from unittest import mock
        with mock.patch.object(self.scout, "fetch", self._fake({"a": "hi", "b": "no"})):
            self.assertEqual(self.scout.chat([], "k", "a,b"), "hi")
        self.assertEqual([m for m, _ in self.calls], ["a"])

    def test_none_when_every_model_fails(self):
        from unittest import mock
        with mock.patch.object(self.scout, "fetch", self._fake({"a": None, "b": None})):
            self.assertIsNone(self.scout.chat([], "k", "a,b"))
        with mock.patch.object(self.scout, "fetch", self._fake({"a": None})):
            self.assertIsNone(self.scout.chat([], "k", "a"))
        self.assertEqual(self.calls[-1][1], (15, 45, 90))

if __name__ == "__main__":
    unittest.main()
