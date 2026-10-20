from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup


FORMATS = ("epub", "pdf", "mobi", "azw3", "djvu", "txt", "fb2", "cbz", "cbr", "docx", "rtf")
MD5_PATH = re.compile(r"^/md5/([a-f0-9]{32})/?$", re.I)


def normalize(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())


@dataclass(frozen=True)
class BookRequest:
    title: str
    author: str = ""

    @property
    def query(self) -> str:
        return " ".join(filter(None, (self.title, self.author)))

    @property
    def key(self) -> str:
        return normalize(self.title) + "|" + normalize(self.author)


def parse_requests(text: str) -> list[BookRequest]:
    books, seen = [], set()
    for line in text.splitlines():
        line = line.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        parts = line.split("|", 1)
        title = parts[0].strip()
        if not title:
            continue
        book = BookRequest(title, parts[1].strip() if len(parts) == 2 else "")
        if book.key not in seen:
            seen.add(book.key)
            books.append(book)
    return books


@dataclass(frozen=True)
class Candidate:
    md5: str
    title: str
    author: str
    extension: str
    metadata: str
    url: str
    has_issues: bool = False


@dataclass(frozen=True)
class CandidateGroup:
    key: str
    title: str
    candidates: tuple[Candidate, ...]


_VOLUME_MARKER = re.compile(
    r"第\s*(?P<number>[0-9零〇一二三四五六七八九十百千万两]+)\s*(?P<unit>[集卷册部])"
    r"|(?P<position>[上中下])\s*(?P<position_unit>[集卷册部])"
    r"|(?<![a-z0-9])(?P<english_unit>vol(?:ume)?\.?|part)\s*[:.#]?\s*(?P<english_number>\d+|[ivxlcdm]+)(?![a-z0-9])",
    re.I,
)


def _volume_identity(marker: re.Match) -> str:
    unit = marker["unit"] or marker["position_unit"]
    number = marker["number"] or marker["position"]
    if marker["english_unit"]:
        unit = "部" if marker["english_unit"].casefold() == "part" else "卷"
        number = marker["english_number"].casefold()
    # Normalize leading zeroes, while retaining the catalogue's Chinese and
    # Roman numbering rather than guessing whether two labels are equivalent.
    if number.isdecimal():
        number = str(int(number))
    return unit + ":" + number


def _metadata_volumes(book: Candidate) -> list[tuple[str, str]]:
    metadata = unicodedata.normalize("NFKC", book.metadata)
    # Search metadata separately from the title and author: their names may
    # themselves contain words such as "Part 1" or "上册".
    for value in (book.title, book.author):
        value = unicodedata.normalize("NFKC", value).strip()
        if value:
            metadata = re.sub(re.escape(value), " ", metadata, count=1, flags=re.I)
    title_markers = {
        _volume_identity(marker)
        for marker in _VOLUME_MARKER.finditer(unicodedata.normalize("NFKC", book.title))
    }
    volumes: dict[str, str] = {}
    # A whole delimited field is evidence of a volume. A number in a year,
    # author name, publisher or descriptive sentence is not enough.
    for field in re.split(r"[\r\n|·•,，;；()\[\]{}《》【】]+", metadata):
        marker = _VOLUME_MARKER.fullmatch(field.strip())
        if marker:
            identity = _volume_identity(marker)
            if identity not in title_markers:
                volumes.setdefault(identity, marker[0])
    return list(volumes.items())


def group_candidates(ranked: list[Candidate]) -> list[CandidateGroup]:
    """Keep one selectable group per full title/explicit volume, in rank order."""
    grouped: dict[str, tuple[str, list[Candidate]]] = {}
    for book in ranked:
        volumes = _metadata_volumes(book)
        key = normalize(book.title)
        if volumes:
            key += "|" + "|".join(sorted(identity for identity, _ in volumes))
        if key not in grouped:
            title = book.title
            if volumes:
                title += "（" + "、".join(label for _, label in volumes) + "）"
            grouped[key] = (title, [])
        grouped[key][1].append(book)
    return [CandidateGroup(key, title, tuple(books)) for key, (title, books) in grouped.items()]


def web_url(url: str, base: str) -> str | None:
    result = urljoin(base, url)
    parsed = urlsplit(result)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        return None
    return result


def parse_candidates(html: str, base_url: str) -> list[Candidate]:
    """Read the compact layout, plus the current standard and older list layouts."""
    soup = BeautifulSoup(html, "html.parser")
    candidates: dict[str, Candidate] = {}
    for anchor in soup.select('a[href*="/md5/"]'):
        raw_url = anchor.get("href", "")
        url = web_url(raw_url, base_url)
        if not url or urlsplit(url).netloc != urlsplit(base_url).netloc:
            continue
        match = MD5_PATH.fullmatch(urlsplit(url).path)
        if not match:
            continue
        heading = anchor.select_one("h3, h2")
        classes = " ".join(anchor.get("class", []))
        if heading:
            title = heading.get_text(" ", strip=True)
            container = anchor
        elif "js-vim-focus" in classes and anchor.get_text(strip=True):
            title = anchor.get_text(" ", strip=True)
            container = anchor.parent.parent
        else:
            continue
        metadata = container.get_text(" ", strip=True)
        extension_match = re.search(r"(?<![a-z0-9])(" + "|".join(FORMATS) + r")(?![a-z0-9])", metadata, re.I)
        if not extension_match:
            continue
        author_node = container.select_one('.italic, a[href^="/search?q="]')
        author = author_node.get_text(" ", strip=True) if author_node else ""
        md5 = match[1].lower()
        has_issues = "opacity-40" in classes or "opacity-40" in " ".join(container.get("class", []))
        candidates.setdefault(md5, Candidate(md5, title, author, extension_match[1].lower(), metadata, url, has_issues))
    return list(candidates.values())


def title_similarity(query: str, title: str) -> float:
    wanted, actual = normalize(query), normalize(title)
    if not wanted or not actual:
        return 0
    if wanted == actual:
        return 1
    requested_volumes = {
        _volume_identity(marker)
        for marker in _VOLUME_MARKER.finditer(unicodedata.normalize("NFKC", query))
    }
    actual_volumes = {
        _volume_identity(marker)
        for marker in _VOLUME_MARKER.finditer(unicodedata.normalize("NFKC", title))
    }
    if requested_volumes and not requested_volumes <= actual_volumes:
        return 0
    # An explicit suffix is meaningful even for a short title, unlike a bare
    # number or an arbitrary trailing word. This lets a series search reach its
    # numbered volumes before the user chooses which ones to download.
    if len(wanted) >= 2 and actual.startswith(wanted):
        if _VOLUME_MARKER.fullmatch(actual[len(wanted):]):
            return 0.95
    # A subtitle/edition suffix is acceptable; a short word appearing anywhere is not.
    for base in re.split(r"[:：\(（\[【]", title)[0:1]:
        if normalize(base) == wanted:
            return 0.98
    # Chinese catalogues commonly separate a series name and volume title with
    # spaces or a full stop: “科学帮帮忙  万能的磁铁”. Match that complete
    # leading label, while retaining the short-English and substring guards.
    if len(wanted) >= 3 and re.fullmatch(r"[\u3400-\u4dbf\u4e00-\u9fff\U00020000-\U0002fa1f]+", wanted):
        segments = re.split(r"[\s.。·•—–\-]+", unicodedata.normalize("NFKC", title).strip(), maxsplit=1)
        if len(segments) == 2 and normalize(segments[0]) == wanted and segments[1].strip():
            return 0.95
    if min(len(wanted), len(actual)) < 8:
        return 0
    return SequenceMatcher(None, wanted, actual).ratio()


def rank_candidates(books: list[Candidate], request: BookRequest, preferred: str, language: str) -> list[Candidate]:
    ranked = []
    isbn = re.sub(r"[-\s]", "", request.title)
    is_isbn = bool(re.fullmatch(r"(?:\d{9}[\dXx]|\d{13})", isbn))
    for index, book in enumerate(books):
        score = 1.0 if is_isbn else title_similarity(request.title, book.title)
        if score < 0.90:
            continue
        if language:
            lang_pattern = r"(?<![a-z])" + re.escape(language) + r"(?:[_-][a-z]+)?(?![a-z])"
            aliases = {"zh": ("chinese", "中文", "汉语"), "en": ("english", "英语")}
            if not re.search(lang_pattern, book.metadata, re.I) and not any(a in book.metadata.lower() for a in aliases.get(language, ())):
                continue
        if request.author:
            desired, actual = normalize(request.author), normalize(book.author)
            tokens = set(re.findall(r"\w+", request.author.casefold()))
            author_tokens = set(re.findall(r"\w+", book.author.casefold()))
            if not actual or not (desired in actual or (tokens and tokens <= author_tokens)):
                continue
        score += 0.04 if book.extension == preferred else 0
        score -= 0.08 if book.has_issues else 0
        ranked.append((score, -index, book))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in ranked]


def free_download_links(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.select('a[href*="/slow_download/"]'):
        url = web_url(a.get("href", ""), base_url)
        if url and urlsplit(url).netloc == urlsplit(base_url).netloc and url not in links:
            links.append(url)
    return links


def ready_download_links(html: str, base_url: str) -> list[str]:
    """Only use links already exposed by the ordinary download page after its wait."""
    soup = BeautifulSoup(html, "html.parser")
    if soup.select_one(".js-partner-countdown"):
        return []
    links = []
    for a in soup.select("a[href]"):
        label = a.get_text(" ", strip=True).casefold()
        if not re.match(r"^(?:⬇\s*)?(?:download\b|立即下载|下载文件|下载)", label):
            continue
        if any(word in label for word in ("short filename", "manager", "fast", "membership", "viewer", "donat", "快速", "会员")):
            continue
        url = web_url(a.get("href", ""), base_url)
        if url and not re.match(r"^/(?:md5|search|donate|faq|login|fast_download|slow_download|view)(?:/|$)", urlsplit(url).path):
            links.append(url)
    # Anna's Archive sometimes displays a copy/paste URL instead of an anchor.
    for span in soup.select("span.whitespace-nowrap > span, code"):
        text = span.get_text("", strip=True)
        if re.fullmatch(r"https?://[^\s<>]+", text):
            url = web_url(text, base_url)
            if url:
                links.append(url)
    return list(dict.fromkeys(links))


def verification_kind(title: str, body: str) -> str | None:
    """Classify visible instructions; never interact with a verification challenge."""
    snippet = body[:4000].casefold().replace("’", "'")
    if any(word in snippet for word in (
        "verify you are human", "verify that you are human", "i'm not a robot",
        "i am not a robot", "complete the captcha", "solve the captcha",
        "请验证您是真人", "验证您是真人", "我不是机器人", "请完成验证码",
    )):
        return "interactive"
    title = title.casefold()
    if any(word in title for word in ("ddos-guard", "just a moment", "security verification", "browser verification")):
        return "automatic"
    if any(word in snippet for word in (
        "checking your browser before", "checking if the site connection is secure",
        "verifying you are human", "performing security verification", "正在检查您的浏览器",
    )):
        return "automatic"
    return None


def verification_required(title: str, body: str) -> bool:
    return verification_kind(title, body) is not None


def safe_filename(title: str, md5: str, extension: str) -> str:
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", unicodedata.normalize("NFKC", title)).strip(" .")
    title = title[:85].rstrip(" .") or "book"
    if re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", title, re.I):
        title = "_" + title
    return f"{title} - {md5[:8]}.{extension}"


def file_md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_download(path: Path, candidate: Candidate) -> None:
    if path.stat().st_size == 0:
        raise ValueError("服务器返回了空文件")
    with path.open("rb") as handle:
        header = handle.read(1024)
    if header.lstrip().lower().startswith((b"<!doctype html", b"<html", b"<head", b"<body")):
        raise ValueError("服务器返回的是网页，未保存为图书")
    if candidate.extension == "pdf" and b"%PDF-" not in header:
        raise ValueError("文件不是有效的 PDF")
    if candidate.extension in ("epub", "docx", "cbz") and not zipfile.is_zipfile(path):
        raise ValueError("电子书压缩结构不完整")
    if candidate.extension == "epub":
        with zipfile.ZipFile(path) as archive:
            if archive.read("mimetype").strip() != b"application/epub+zip":
                raise ValueError("文件不是有效的 EPUB")
    if file_md5(path) != candidate.md5:
        raise ValueError("文件 MD5 与书籍记录不符，可能下载不完整")


class History:
    def __init__(self, path: Path):
        self.path = path
        self.entries = {}
        if path.exists():
            try:
                self.entries = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(self.entries, dict):
                    raise ValueError("下载记录格式不正确")
            except (ValueError, OSError):
                # Preserve the original for diagnosis before starting a new history.
                backup = path.with_suffix(".broken.json")
                if not backup.exists():
                    backup.write_bytes(path.read_bytes())
                self.entries = {}

    @staticmethod
    def key(request: BookRequest, preferred: str, language: str, output: Path) -> str:
        return "|".join((request.key, preferred, language, str(output.resolve())))

    def lookup(self, key: str) -> Path | None:
        item = self.entries.get(key)
        if not isinstance(item, dict):
            return None
        path = Path(item.get("path", ""))
        try:
            if path.is_file() and path.stat().st_size == item.get("size") and file_md5(path) == item.get("md5"):
                return path
        except OSError:
            pass
        return None

    def remember(self, key: str, candidate: Candidate, path: Path) -> None:
        self.entries[key] = {"path": str(path.resolve()), "size": path.stat().st_size, "md5": candidate.md5, "title": candidate.title, "source": candidate.url}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.entries, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)
