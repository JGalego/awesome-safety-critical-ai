import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

import draft as d  # noqa: E402

README = """# T

## <a id="articles"></a>📝 Articles

<small>[🔼 Back to top](#toc)</small>

* (Alshiekh *et al.*, 2017) [Safe RL via Shielding](https://arxiv.org/abs/1708.08611)
* (Yang, 2017) [Something](https://example.org/yang)

## <a id="blogs"></a>✍️ Blogs / News

* (Acubed, 2023) [Airbus Validates Vision](https://acubed.airbus.com/blog/a)
* (Bits & Chips, 2024) [Verifying AI](https://bits-chips.com/article/v/)

## <a id="tools"></a>🛠️ Tools

### Data Management

* [`cleanlab/cleanlab`](https://github.com/cleanlab/cleanlab): data-centric AI package
* [`pydantic/pydantic`](https://github.com/pydantic/pydantic): data validation

### Oldies 🕰️

* [`pralab/secml`](https://github.com/pralab/secml): security evaluation

## <a id="videos"></a>📺 Videos
"""


def page(kind, url, title, text="", **kw):
    return d.Page(url, title, text or f"{title}. A study of safety assurance for machine learning.", "2026", kind, **kw)


class StructureTests(unittest.TestCase):
    def test_tool_subsections_skip_hand_curated(self):
        names = [s.heading for s in d.tool_subsections(README.split("\n"))]
        self.assertEqual(names, ["Data Management"])

    def test_sort_key_forms(self):
        self.assertEqual(d.sort_key("* (Alshiekh *et al.*, 2017) [x](u)"), "alshiekh")
        self.assertEqual(d.sort_key("* (Bits & Chips, 2024) [x](u)"), "bits & chips")
        self.assertEqual(d.sort_key("* [`pydantic/pydantic`](u): x"), "pydantic/pydantic")

    def test_author_labels(self):
        self.assertEqual(d.author_label(["Ada Yang"]), "Yang")
        self.assertEqual(d.author_label(["A Li", "B Chen"]), "Li & Chen")
        self.assertEqual(d.author_label(["A Li", "B Chen", "C Wu"]), "Li, Chen & Wu")
        self.assertEqual(d.author_label(["A Li", "B Chen", "C Wu", "D Xu"]), "Li *et al.*")


class DraftTests(unittest.TestCase):
    def test_article_goes_in_alphabetical_position(self):
        p = page(d.ARTICLES, "https://arxiv.org/abs/2611.00001", "Runtime Monitoring of Nets", authors=["A Brown", "B Li"])
        r = d.draft(README, p, {"fits": True, "evidence": p.text}, set())
        self.assertTrue(r.ok, r.problems)
        self.assertEqual(r.entry, "* (Brown & Li, 2026) [Runtime Monitoring of Nets](https://arxiv.org/abs/2611.00001)")
        lines = r.lines
        self.assertLess(lines.index(r.entry), lines.index("* (Yang, 2017) [Something](https://example.org/yang)"))
        self.assertGreater(lines.index(r.entry), lines.index("* (Alshiekh *et al.*, 2017) [Safe RL via Shielding](https://arxiv.org/abs/1708.08611)"))

    def test_article_without_authors_is_rejected(self):
        p = page(d.ARTICLES, "https://arxiv.org/abs/2611.00001", "T")
        self.assertFalse(d.draft(README, p, {"fits": True, "evidence": p.text}, set()).ok)

    def test_blog_checks_title_and_source(self):
        text = "Why Assurance Cases Matter. A post on the Example Journal about assurance cases."
        p = page(d.BLOGS, "https://example.org/post", "Why Assurance Cases Matter", text)
        ok = d.draft(README, p, {"fits": True, "evidence": p.text, "title": "Why Assurance Cases Matter", "source": "Example Journal"}, set())
        self.assertTrue(ok.ok, ok.problems)
        self.assertEqual(ok.entry, "* (Example Journal, 2026) [Why Assurance Cases Matter](https://example.org/post)")
        bad = d.draft(README, p, {"fits": True, "evidence": p.text, "title": "Invented Title", "source": "Example Journal"}, set())
        self.assertFalse(bad.ok)
        bad = d.draft(README, p, {"fits": True, "evidence": p.text, "title": "Why Assurance Cases Matter", "source": "Nowhere Weekly"}, set())
        self.assertFalse(bad.ok)

    def test_blog_source_may_match_the_host(self):
        p = page(d.BLOGS, "https://www.acubed.com/blog/x", "A Post Title")
        r = d.draft(README, p, {"fits": True, "evidence": p.text, "title": "A Post Title", "source": "Acubed"}, set())
        self.assertTrue(r.ok, r.problems)

    def test_tool_entry_and_position(self):
        p = page(d.TOOLS, "https://github.com/great-expectations/great_expectations", "great-expectations/great_expectations",
                 "great-expectations/great_expectations. data quality checks for 50 pipelines in production")
        a = {"fits": True, "evidence": p.text, "subsection": "Data Management", "description": "data quality checks for 50 pipelines"}
        r = d.draft(README, p, a, set())
        self.assertTrue(r.ok, r.problems)
        self.assertEqual(r.entry, "* [`great-expectations/great_expectations`](https://github.com/great-expectations/great_expectations): data quality checks for 50 pipelines")
        self.assertLess(r.lines.index(r.entry), r.lines.index("* [`pydantic/pydantic`](https://github.com/pydantic/pydantic): data validation"))

    def test_tool_description_rules(self):
        p = page(d.TOOLS, "https://github.com/a/lib", "a/lib", "a/lib. checks the safety assurance of machine learning")
        for desc in ("Capitalized start", "ends with a period.", "a blazing fast thing", "checks 99 things"):
            a = {"fits": True, "evidence": p.text, "subsection": "Data Management", "description": desc}
            self.assertFalse(d.draft(README, p, a, set()).ok, desc)

    def test_tool_in_hand_curated_subsection_is_rejected(self):
        p = page(d.TOOLS, "https://github.com/a/lib", "a/lib", "a/lib. checks the safety assurance of machine learning")
        a = {"fits": True, "evidence": p.text, "subsection": "Oldies 🕰️", "description": "checks things"}
        self.assertFalse(d.draft(README, p, a, set()).ok)

    def test_scope_evidence_is_required_and_must_be_on_the_page(self):
        p = page(d.BLOGS, "https://example.org/post", "A Post Title")
        for label, ev in {"missing": None, "short": "Safety.", "not on page": "This quote was never on the page at all."}.items():
            r = d.draft(README, p, {"fits": True, "evidence": ev, "title": "A Post Title", "source": "Example"}, set())
            self.assertFalse(r.ok, label)
            self.assertIn("quote", r.problems[0])

    def test_declined_known_and_page_problems(self):
        p = page(d.BLOGS, "https://example.org/post", "T")
        self.assertIn("declined", d.draft(README, p, {"fits": False, "reason": "filler"}, set()).problems[0])
        self.assertFalse(d.draft(README, p, {"fits": True, "evidence": p.text}, {d.normalize(p.url)}).ok)
        p.problems = ["The repository is archived."]
        self.assertEqual(d.draft(README, p, {"fits": True, "evidence": p.text}, set()).problems, ["The repository is archived."])

    def test_parse_answer_and_comment(self):
        self.assertEqual(d.parse_answer('x {"fits": true, "reason": "r"} y')["reason"], "r")
        self.assertIsNone(d.parse_answer("no json"))
        self.assertEqual(d.COMMENT_URL.search("/draft <https://a.b/c>").group(1), "https://a.b/c")

    def test_published_year(self):
        self.assertEqual(d.published_year('<meta property="article:published_time" content="2025-03-04T00:00:00Z">'), "2025")
        self.assertIsNone(d.published_year("<html></html>"))

    def test_works_on_the_real_readme(self):
        path = os.path.join(os.path.dirname(__file__), "..", "README.md")
        with open(path, encoding="utf-8") as f:
            real = f.read()
        lines = real.split("\n")
        self.assertIsNotNone(d.top_section(lines, d.ARTICLES))
        self.assertIsNotNone(d.top_section(lines, d.BLOGS))
        self.assertGreater(len(d.tool_subsections(lines)), 5)
        p = page(d.ARTICLES, "https://arxiv.org/abs/2611.00001", "A Paper", authors=["A Zed"])
        r = d.draft(real, p, {"fits": True, "evidence": p.text}, set())
        self.assertTrue(r.ok, r.problems)
        self.assertEqual(len(r.lines), len(lines) + 1)


if __name__ == "__main__":
    unittest.main()
