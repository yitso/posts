"""Check the generated Pages site without third-party dependencies."""

import json
import re
import sys
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

BASE = "/posts/"
ORIGIN = "https://yitso.github.io"
SOURCE = Path(__file__).resolve().parent.parent


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key in {"href", "src"} and value:
                self.urls.append(value)


def check(site):
    site = site.resolve()
    ids = []
    for article in sorted((SOURCE / "content").rglob("*.md")):
        text = article.read_text(encoding="utf-8")
        front = text.split("---", 2)
        assert len(front) == 3 and not front[0].strip(), f"Missing front matter: {article.name}"
        match = re.search(r"^id: ([a-z0-9][a-z0-9-]*)$", front[1], re.MULTILINE)
        assert match, f"Invalid or missing id: {article.name}"
        ids.append(match[1])
    assert ids and len(ids) == len(set(ids)), "Article IDs must be unique"

    manifest = json.loads((site / "build-manifest.json").read_text())
    assert manifest["site_base"] == BASE, "Incorrect deployment base"
    assert manifest["article_count"] == len(ids), "Missing articles in build"
    assert not manifest["warnings"], "Build reported warnings"
    required = ["index.html", "posts/index.html", "tags/index.html", "404.html", ".nojekyll"]
    required += [f"posts/{article_id}/index.html" for article_id in ids]
    for relative in required:
        assert (site / relative).is_file(), f"Missing output: {relative}"

    def local_target(url, current):
        parsed = urlsplit(urljoin(ORIGIN + BASE + current, url))
        if parsed.scheme not in {"http", "https"} or parsed.netloc != "yitso.github.io":
            return
        assert parsed.path.startswith(BASE), f"Link escapes deployment base: {url} in {current}"
        relative = unquote(parsed.path[len(BASE):])
        target = (site / relative).resolve()
        assert target.is_relative_to(site), f"Link escapes output directory: {url}"
        if target.is_dir():
            target /= "index.html"
        assert target.is_file(), f"Broken local link: {url} in {current}"

    pages = sorted(site.rglob("*.html"))
    for page in pages:
        html = page.read_text(encoding="utf-8")
        assert "/Users/" not in html and "localhost:" not in html, f"Private path in {page.name}"
        parser = Links()
        parser.feed(html)
        for url in parser.urls:
            local_target(url, page.relative_to(site).as_posix())

    sitemap = ET.parse(site / "sitemap.xml")
    for loc in sitemap.iter("{http://www.sitemaps.org/schemas/sitemap/0.9}loc"):
        assert loc.text.startswith(ORIGIN + BASE), f"Incorrect sitemap URL: {loc.text}"
        local_target(loc.text, "index.html")
    assert f"Sitemap: {ORIGIN}{BASE}sitemap.xml" in (site / "robots.txt").read_text()
    print(f"Verified {len(ids)} articles, {len(pages)} HTML pages, local links, assets, and sitemap under {BASE}")


if __name__ == "__main__":
    check(Path(sys.argv[1] if len(sys.argv) > 1 else "dist"))
