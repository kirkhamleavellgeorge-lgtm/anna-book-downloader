import html
import unittest

from anna_downloader.core import BookRequest, parse_candidates, rank_candidates, title_similarity


BASE = "https://annas-archive.pk"
SERIES = "科学帮帮忙"


def compact_result(index: int, title: str, extension: str = "pdf") -> str:
    """Synthetic public search markup with the observed language/format layout."""
    md5 = f"{index:032x}"
    return (
        f'<a href="/md5/{md5}" class="js-vim-focus">'
        '<div><div class="text-gray-500">'
        f'Chinese [zh], .{extension}, 16.5MB, Book (unknown)'
        f'</div><h3>{html.escape(title)}</h3>'
        '<div>天津:新蕾出版社, 2016</div>'
        '<div class="italic">测试作者</div></div></a>'
    )


class ChineseSeriesMatchingTests(unittest.TestCase):
    def setUp(self):
        self.series_titles = [
            "科学帮帮忙  变幻莫测的化学",
            "科学帮帮忙  万能的磁铁",
            "科学帮帮忙. 不可思议的水 Ke xue bang bang mang. Bu ke si yi de shui",
            "科学帮帮忙． 声音的魔法 Ke xue bang bang mang. Sheng yin de mo fa",
        ]
        rows = [compact_result(index, title) for index, title in enumerate(self.series_titles, 1)]
        rows.extend([
            compact_result(5, "刺猬帮帮忙", "epub"),
            compact_result(6, "秋吉同学帮帮忙", "epub"),
        ])
        self.books = parse_candidates("".join(rows), BASE)

    def test_parse_observed_chinese_compact_layout(self):
        self.assertEqual(len(self.books), 6)
        self.assertEqual([book.title for book in self.books[:4]], self.series_titles)
        self.assertTrue(all(book.extension == "pdf" for book in self.books[:4]))
        self.assertTrue(all("Chinese [zh]" in book.metadata for book in self.books))

    def test_series_request_accepts_pdf_fallback_and_rejects_unrelated_epubs(self):
        matches = rank_candidates(self.books, BookRequest(SERIES), preferred="epub", language="zh")
        self.assertEqual([book.title for book in matches], self.series_titles)

    def test_series_rule_still_obeys_language_filter(self):
        self.assertEqual(rank_candidates(self.books, BookRequest(SERIES), "epub", "en"), [])

    def test_complete_han_series_label_with_whitespace_boundary(self):
        for boundary in ("  ", "\u3000", "\t"):
            with self.subTest(boundary=repr(boundary)):
                self.assertGreaterEqual(title_similarity(SERIES, SERIES + boundary + "万能的磁铁"), 0.90)

    def test_complete_han_series_label_with_punctuation_boundary(self):
        for boundary in (". ", "． ", "。", "·", "•", "—", "–", "-"):
            with self.subTest(boundary=boundary):
                title = SERIES + boundary + "不可思议的水 Ke xue bang bang mang. Bu ke si yi de shui"
                self.assertGreaterEqual(title_similarity(SERIES, title), 0.90)

    def test_only_complete_leading_series_label_matches(self):
        for title in (
            "科学帮帮忙学  变幻莫测的化学",
            "前传科学帮帮忙  变幻莫测的化学",
            "科学帮帮忙续编",
            "刺猬帮帮忙",
            "秋吉同学帮帮忙",
        ):
            with self.subTest(title=title):
                self.assertLess(title_similarity(SERIES, title), 0.90)
        self.assertLess(title_similarity("帮帮忙", "科学帮帮忙  万能的磁铁"), 0.90)

    def test_generic_two_han_query_does_not_expand_to_series(self):
        self.assertEqual(title_similarity("科学", "科学"), 1)
        for title in ("科学  万能的磁铁", "科学. 万能的磁铁", "科学帮帮忙  万能的磁铁"):
            with self.subTest(title=title):
                self.assertLess(title_similarity("科学", title), 0.90)

    def test_short_english_partial_title_guard_is_preserved(self):
        for title in ("Learning Python", "Pythonic", "Python Complete", "Python. Complete"):
            with self.subTest(title=title):
                self.assertLess(title_similarity("Python", title), 0.90)

    def test_exact_title_outranks_series_volume_in_preferred_format(self):
        books = parse_candidates(
            compact_result(1, SERIES + "  万能的磁铁", "epub") + compact_result(2, SERIES), BASE
        )
        matches = rank_candidates(books, BookRequest(SERIES), "epub", "zh")
        self.assertEqual([book.title for book in matches], [SERIES, SERIES + "  万能的磁铁"])

    def test_existing_colon_and_bracket_subtitle_rules_are_preserved(self):
        for title in ("Python: Illustrated", "Python（Illustrated）", "Python [Illustrated]"):
            with self.subTest(title=title):
                self.assertGreaterEqual(title_similarity("Python", title), 0.90)


if __name__ == "__main__":
    unittest.main()
