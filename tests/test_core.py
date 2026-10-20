import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from anna_downloader.core import (
    BookRequest, Candidate, History, free_download_links, parse_candidates, parse_requests,
    rank_candidates, ready_download_links, safe_filename, validate_download, verification_kind, verification_required,
)


BASE = "https://annas-archive.pk"
ID1, ID2 = "a" * 32, "b" * 32


def epub_bytes():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        archive.writestr("content.opf", '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">test</dc:identifier><dc:title>Open Test Book</dc:title><dc:language>en</dc:language></metadata><manifest><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="chapter"/></spine></package>')
        archive.writestr("chapter.xhtml", '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Test</title></head><body><p>Locally generated test book.</p></body></html>')
    return buffer.getvalue()


def compact(md5=ID1, title="Pride and Prejudice", author="Jane Austen", language="en", extension="epub"):
    return f'<a href="/md5/{md5}" class="js-vim-focus"><div><div class="text-gray-500">{language} [English], {extension}, 1.2MB</div><h3>{title}</h3><div>Publisher, 1813</div><div class="italic">{author}</div></div></a>'


class CoreTests(unittest.TestCase):
    def test_parse_batch_and_author(self):
        requests = parse_requests("\ufeff#注释\n红楼梦 | 曹雪芹\n\n红楼梦|曹雪芹\nPride and Prejudice\n | nobody")
        self.assertEqual(requests, [BookRequest("红楼梦", "曹雪芹"), BookRequest("Pride and Prejudice")])

    def test_parse_current_compact_and_deduplicate(self):
        results = parse_candidates(compact() * 2, BASE)
        self.assertEqual(len(results), 1)
        self.assertEqual((results[0].title, results[0].author, results[0].extension), ("Pride and Prejudice", "Jane Austen", "epub"))

    def test_parse_current_standard_layout(self):
        html = f'<div class="flex"><a href="/md5/{ID1}">cover</a><div class="max-w-full"><div><a class="js-vim-focus" href="/md5/{ID1}">红楼梦</a><a href="/search?q=author">曹雪芹</a></div><div>description</div><div>zh [Chinese], PDF, 3MB</div></div></div>'
        result = parse_candidates(html, BASE)[0]
        self.assertEqual((result.title, result.author, result.extension), ("红楼梦", "曹雪芹", "pdf"))

    def test_matching_author_format_and_language(self):
        books = parse_candidates(compact(extension="pdf") + compact(ID2, extension="epub"), BASE)
        ranked = rank_candidates(books, BookRequest("Pride and Prejudice", "Austen Jane"), "epub", "en")
        self.assertEqual([b.md5 for b in ranked], [ID2, ID1])
        self.assertFalse(rank_candidates(books, BookRequest("Pride and Prejudice", "Someone Else"), "epub", ""))
        self.assertFalse(rank_candidates(books, BookRequest("Pride and Prejudice"), "epub", "zh"))

    def test_do_not_accept_short_partial_title(self):
        books = parse_candidates(compact(title="Learning Python"), BASE)
        self.assertFalse(rank_candidates(books, BookRequest("Python"), "epub", ""))

    def test_subtitle_match(self):
        books = parse_candidates(compact(title="Pride and Prejudice: Illustrated"), BASE)
        self.assertTrue(rank_candidates(books, BookRequest("Pride and Prejudice"), "epub", ""))

    def test_untrusted_and_paid_links_not_selected(self):
        html = f'<a href="/fast_download/{ID1}">Fast</a><a href="https://elsewhere.test/slow_download/{ID1}/0/0">Free</a><a href="/slow_download/{ID1}/0/0">Slow</a>'
        self.assertEqual(free_download_links(html, BASE), [f"{BASE}/slow_download/{ID1}/0/0"])

    def test_ready_link_waits_for_countdown(self):
        link = '<a href="/files/book.epub">Download now</a>'
        self.assertEqual(ready_download_links('<span class="js-partner-countdown">1</span>' + link, BASE), [])
        self.assertEqual(ready_download_links(link, BASE), [BASE + "/files/book.epub"])
        self.assertEqual(ready_download_links('<a href="javascript:alert(1)">Download now</a><a href="/donate">Download fast</a>', BASE), [])

    def test_copy_url_page(self):
        html = '<span class="whitespace-nowrap"><button>Copy</button><span class="bg-gray-200">https://files.example.test/book?token=a&amp;b=2</span></span>'
        self.assertEqual(ready_download_links(html, BASE), ["https://files.example.test/book?token=a&b=2"])

    def test_verification_detection(self):
        self.assertTrue(verification_required("DDoS-Guard", "Please wait"))
        self.assertTrue(verification_required("Just a moment...", ""))
        self.assertTrue(verification_required("Download", "Please verify that you are human"))
        self.assertFalse(verification_required("Book", "Slow downloads require browser verification"))

    def test_automatic_browser_check_does_not_require_manual_action(self):
        self.assertEqual(verification_kind("DDoS-Guard", "Checking your browser before accessing this site"), "automatic")
        self.assertEqual(verification_kind("Just a moment...", "Verifying you are human. This may take a few seconds."), "automatic")
        self.assertIsNone(verification_kind("Search", "Search results"))

    def test_explicit_human_prompt_takes_precedence(self):
        self.assertEqual(verification_kind("DDoS-Guard", "Please verify that you are human"), "interactive")
        self.assertEqual(verification_kind("Security verification", "I’m not a robot"), "interactive")
        self.assertEqual(verification_kind("网站安全检查", "请完成验证码"), "interactive")

    def test_windows_safe_filename(self):
        name = safe_filename('../a:b\\c*?"<>|', ID1, "pdf")
        for char in '<>:"/\\|?*':
            self.assertNotIn(char, name)
        self.assertTrue(safe_filename("CON", ID1, "epub").startswith("_CON"))
        self.assertLess(len(safe_filename("书" * 400, ID1, "pdf")), 110)

    def test_download_validation_and_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "book.epub"
            data = epub_bytes()
            md5 = hashlib.md5(data).hexdigest()
            book = Candidate(md5, "Test", "", "epub", "", BASE + "/md5/" + md5)
            path.write_bytes(data)
            validate_download(path, book)
            history = History(Path(directory) / "history.json")
            key = history.key(BookRequest("Test"), "epub", "", Path(directory))
            history.remember(key, book, path)
            self.assertEqual(History(history.path).lookup(key), path)
            # Detect same-length corruption, rather than trusting file existence/size.
            path.write_bytes(b"x" * len(data))
            self.assertIsNone(history.lookup(key))
            with self.assertRaises(ValueError):
                validate_download(path, book)
            path.write_bytes(b"<!doctype html><html>challenge</html>")
            with self.assertRaisesRegex(ValueError, "网页"):
                validate_download(path, book)

    def test_corrupt_history_is_backed_up(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            path.write_text("[]", encoding="utf-8")
            history = History(path)
            self.assertEqual(history.entries, {})
            self.assertEqual(json.loads(path.with_suffix(".broken.json").read_text()), [])


if __name__ == "__main__":
    unittest.main()
