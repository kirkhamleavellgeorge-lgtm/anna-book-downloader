import unittest

from anna_downloader.core import (
    BookRequest, Candidate, CandidateGroup, group_candidates, rank_candidates, title_similarity,
)


def candidate(index, title, extension="epub", metadata="zh, epub", author="测试作者"):
    md5 = f"{index:032x}"
    return Candidate(md5, title, author, extension, metadata, "https://example.test/md5/" + md5)


class VolumeGroupingTests(unittest.TestCase):
    def test_formats_of_same_full_title_share_one_group(self):
        epub = candidate(1, "科学帮帮忙：第一集")
        pdf = candidate(2, "科学帮帮忙: 第一集", "pdf")
        groups = group_candidates([epub, pdf])
        self.assertEqual(len(groups), 1)
        self.assertIsInstance(groups[0], CandidateGroup)
        self.assertEqual(groups[0].title, epub.title)
        self.assertEqual(groups[0].candidates, (epub, pdf))

    def test_subtitles_and_explicit_title_volumes_stay_separate(self):
        titles = ["科学帮帮忙 万能的磁铁", "科学帮帮忙 变幻莫测的化学", "科学帮帮忙第1集", "科学帮帮忙第2集"]
        groups = group_candidates([candidate(index, title) for index, title in enumerate(titles, 1)])
        self.assertEqual([group.title for group in groups], titles)
        self.assertEqual(len({group.key for group in groups}), 4)

    def test_group_and_fallback_order_preserve_incoming_rank(self):
        first_epub = candidate(1, "系列：上册")
        second_pdf = candidate(2, "系列：下册", "pdf")
        first_pdf = candidate(3, "系列：上册", "pdf")
        second_epub = candidate(4, "系列：下册")
        groups = group_candidates([first_epub, second_pdf, first_pdf, second_epub])
        self.assertEqual([group.candidates for group in groups], [(first_epub, first_pdf), (second_pdf, second_epub)])

    def test_metadata_only_volumes_get_distinct_groups_and_visible_labels(self):
        first = candidate(1, "科学帮帮忙", metadata="zh | pdf | 第1集 | 科学帮帮忙 | 测试作者")
        second = candidate(2, "科学帮帮忙", metadata="zh | epub | 第2集 | 科学帮帮忙 | 测试作者")
        first_pdf = candidate(3, "科学帮帮忙", "pdf", "zh | pdf | 第1集 | 科学帮帮忙 | 测试作者")
        groups = group_candidates([first, second, first_pdf])
        self.assertEqual([group.title for group in groups], ["科学帮帮忙（第1集）", "科学帮帮忙（第2集）"])
        self.assertEqual(groups[0].candidates, (first, first_pdf))
        self.assertNotEqual(groups[0].key, groups[1].key)

    def test_common_explicit_metadata_markers(self):
        markers = ["第一卷", "第二卷", "上册", "中册", "下册", "Vol. 1", "Volume: 2", "Part 1", "Part 2"]
        groups = group_candidates([
            candidate(index, "A Series", metadata=f"en, epub, [{marker}], Author")
            for index, marker in enumerate(markers, 1)
        ])
        self.assertEqual(len(groups), len(markers))
        self.assertEqual([group.title for group in groups], [f"A Series（{marker}）" for marker in markers])

    def test_english_metadata_volume_spelling_and_zeroes_group_together(self):
        books = [candidate(index, "Series", metadata=f"epub | {marker}") for index, marker in enumerate(("Vol. 01", "Volume 1", "Vol1"), 1)]
        self.assertEqual(group_candidates(books)[0].candidates, tuple(books))

    def test_repeated_title_and_author_are_removed_before_metadata_inspection(self):
        first = candidate(1, "Part 1", metadata="Part 1 | epub | Author", author="Author")
        second = candidate(2, "Part 1", "pdf", "pdf", author="Author")
        self.assertEqual(len(group_candidates([first, second])), 1)
        first = candidate(3, "Series", metadata="Series | epub | Part 1", author="Part 1")
        second = candidate(4, "Series", "pdf", "Series | pdf", author="Part 1")
        self.assertEqual(len(group_candidates([first, second])), 1)

    def test_duplicate_metadata_volume_already_in_title_does_not_split_versions(self):
        first = candidate(1, "Series Vol. 1", metadata="Series Vol. 1 | epub | Volume 1")
        second = candidate(2, "Series Vol. 1", "pdf", "pdf")
        self.assertEqual(group_candidates([first, second])[0].candidates, (first, second))

    def test_year_bare_number_and_words_in_prose_do_not_create_volumes(self):
        metadata = ["zh | epub | 1984", "zh | pdf | 1", "zh | epub | Part 1 Press", "zh | pdf | 我们在第一卷遇到的作者", "zh | epub | 上册编写委员会", "zh | epub | This description mentions Vol. 1"]
        books = [candidate(index, "1984", metadata=value) for index, value in enumerate(metadata, 1)]
        groups = group_candidates(books)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].title, "1984")

    def test_empty_input(self):
        self.assertEqual(group_candidates([]), [])


class ExplicitVolumeMatchingTests(unittest.TestCase):
    def test_attached_or_bracketed_chinese_volume_suffix_is_ranked(self):
        for query, title in (
            ("科学帮帮忙", "科学帮帮忙第一集"),
            ("科学帮帮忙", "科学帮帮忙（第2册）"),
            ("红楼梦", "红楼梦上册"),
            ("红楼梦", "红楼梦下册"),
        ):
            with self.subTest(query=query, title=title):
                book = candidate(1, title)
                self.assertGreaterEqual(title_similarity(query, title), 0.90)
                self.assertEqual(rank_candidates([book], BookRequest(query), "epub", "zh"), [book])

    def test_explicit_english_volume_suffix_is_ranked(self):
        for title in ("Python Vol1", "Python Volume 2", "Python (Part 1)"):
            with self.subTest(title=title):
                self.assertGreaterEqual(title_similarity("Python", title), 0.90)

    def test_bare_numbers_and_nonvolume_suffixes_do_not_expand_short_queries(self):
        for title in ("Python 1984", "Python 2", "Pythonic", "Python Complete"):
            with self.subTest(title=title):
                self.assertLess(title_similarity("Python", title), 0.90)

    def test_requested_volume_does_not_fuzzily_match_a_different_volume(self):
        for query, title in (
            ("科学帮帮忙第一集", "科学帮帮忙第二集"),
            ("科学帮帮忙第1卷", "科学帮帮忙第2卷"),
            ("Long Science Series Vol1", "Long Science Series Vol2"),
            ("Long Science Series Part 1", "Long Science Series Part 2"),
            ("科学帮帮忙上册", "科学帮帮忙下册"),
            ("Long Science Series Vol1", "Long Science Series"),
        ):
            with self.subTest(query=query, title=title):
                self.assertLess(title_similarity(query, title), 0.90)

    def test_explicit_volume_still_accepts_edition_and_format_alternatives(self):
        for title in ("Long Science Series Vol1", "Long Science Series Vol1: Revised"):
            with self.subTest(title=title):
                self.assertGreaterEqual(title_similarity("Long Science Series Vol1", title), 0.90)


if __name__ == "__main__":
    unittest.main()
