import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

import draft_all as a  # noqa: E402

BODY = """Intro with [CONTRIBUTING](https://example.org/c).

### Papers

- [Paper One](https://arxiv.org/abs/2611.00001) - arXiv, 2026-11-13. Filter: x.
- [Paper One again](https://arxiv.org/pdf/2611.00001v2) - arXiv, 2026-11-13.

### Repositories

- [org/repo](https://github.com/org/repo) - GitHub, 2026-10-13.
* [A post](https://example.org/post) - Blog.
"""


class CandidateTests(unittest.TestCase):
    def test_only_list_links_deduplicated(self):
        self.assertEqual(
            a.candidate_urls(BODY),
            ["https://arxiv.org/abs/2611.00001", "https://github.com/org/repo", "https://example.org/post"],
        )

    def test_read_output(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".out", delete=False) as f:
            f.write("status=ok\nbranch=draft/x\ntitle=Add X = Y\n")
        try:
            self.assertEqual(a.read_output(f.name), {"status": "ok", "branch": "draft/x", "title": "Add X = Y"})
        finally:
            os.unlink(f.name)


if __name__ == "__main__":
    unittest.main()
