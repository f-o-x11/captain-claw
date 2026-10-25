"""Pictures found while researching, kept for reports (captain_claw/research_images.py)."""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from PIL import Image

from captain_claw import research_images as ri
from captain_claw.tools.edit import EditTool
from captain_claw.tools.read import ReadTool
from captain_claw.tools.web_fetch import WebFetchBatchTool, WebFetchTool
from captain_claw.tools.write import WriteTool

PAGE = "https://acme.com/news/x300"

ARTICLE = """<html><head><title>X300</title>
<meta property="og:image" content="/img/x300-hero.jpg?w=1200"></head><body>
<header><img src="/static/logo.png" alt="Acme logo"></header>
<nav><img src="/img/menu.jpg" alt="A long menu picture of something"></nav>
<article><h1>The X300 battery</h1>
<figure><img src="data:image/gif;base64,R0lGOD" data-src="/img/x300-hero.jpg?w=800"
  srcset="/img/x300-hero.jpg?w=400 400w, /img/x300-hero.jpg?w=1200 1200w"
  alt="The X300 battery pack on a test bench"><figcaption>The X300 ships in Q3.</figcaption></figure>
<h2>Charging</h2><p>Charging takes 20 minutes.
  <img src="/img/chart.png" alt="Charging curve of the X300 versus rivals" width="900" height="500"></p>
<img src="/img/IMG_1234.jpg" alt="IMG_1234.jpg">
<img src="/img/no-alt.jpg">
<div class="related-posts"><a href="/other"><img src="/img/other.jpg"
  alt="Another great article about batteries"></a></div>
<div class="share-bar"><img src="/img/fb.png" alt="Share on Facebook today"></div>
<img src="/img/pixel.gif" width="1" height="1" alt="tracking pixel for analytics">
</article><footer><img src="/img/foot.jpg" alt="Footer photo of the team"></footer></body></html>"""

HERO = "https://acme.com/img/x300-hero.jpg?w=1200"
CHART = "https://acme.com/img/chart.png"


def _jpeg(w: int = 800, h: int = 500, color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "JPEG")
    return buf.getvalue()


def _png(w: int = 800, h: int = 500, alpha: bool = False) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA" if alpha else "RGB", (w, h), (10, 120, 200, 128) if alpha else (10, 120, 200)
              ).save(buf, "PNG")
    return buf.getvalue()


def _webp(w: int = 900, h: int = 600) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (30, 160, 60)).save(buf, "WEBP")
    return buf.getvalue()


class _Resp:
    def __init__(self, status: int, ctype: str, body: bytes):
        self.status_code = status
        self.headers = {"content-type": ctype}
        self._body = body

    async def aiter_bytes(self):
        yield self._body


class _Stream:
    def __init__(self, resp: _Resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


class FakeImageClient:
    """Stands in for the public-only client: url → (status, content-type, body)."""

    def __init__(self, routes: dict[str, tuple[int, str, bytes]]):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    def stream(self, method: str, url: str, headers=None):
        self.calls.append((url, dict(headers or {})))
        return _Stream(_Resp(*self.routes.get(url, (404, "text/html", b""))))

    async def aclose(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _no_run_folder(monkeypatch):
    for var in ("CLAW_VFS_PROJECT", "CLAW_VATRA_WORKER", "CLAW_BASNA_WORKER",
                "CLAW_CTX_GOVERNOR", "CLAW_SOURCE_CORPUS", "CLAW_WRITE_DIRECT",
                "CLAW_CODE_AGENT"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def fake_client(monkeypatch):
    client = FakeImageClient({HERO: (200, "image/jpeg", _jpeg(1200, 675)),
                              CHART: (200, "image/png", _png(900, 500))})
    monkeypatch.setattr(ri, "make_client", lambda: client)
    return client


def _kw(tmp_path: Path, session: str = "s1") -> dict:
    return {"_runtime_base_path": tmp_path, "_saved_base_path": tmp_path / "saved",
            "_session_id": session}


def _store(tmp_path: Path, session: str = "s1") -> ri.ImageStore:
    return ri.ImageStore.session_store(tmp_path / "saved", tmp_path, session)


# ── finding ──────────────────────────────────────────────────────────


def test_only_described_content_pictures_are_candidates():
    found = ri.find_page_images(BeautifulSoup(ARTICLE, "html.parser"), PAGE)
    assert [c.url for c in found] == [HERO, CHART]
    hero, chart = found
    assert hero.caption == "The X300 ships in Q3."
    assert hero.alt == "The X300 battery pack on a test bench"
    assert hero.section == "The X300 battery"
    assert chart.section == "Charging"
    assert "Charging takes 20 minutes" in chart.context


def test_srcset_largest_within_cap_wins():
    assert ri._best_from_srcset("a.jpg 400w, b.jpg 1200w, c.jpg 3000w") == "b.jpg"
    assert ri._best_from_srcset("a.jpg 1x, b.jpg 2x") == "b.jpg"
    assert ri._best_from_srcset("only.jpg") == "only.jpg"


def test_a_page_without_article_needs_a_strong_description():
    html = ("<body><img src='/a.jpg' alt='Solar farm'>"
            "<img src='/b.jpg' alt='Aerial view of the new solar farm near Split'></body>")
    found = ri.find_page_images(BeautifulSoup(html, "html.parser"), "https://x.org/")
    assert [c.url for c in found] == ["https://x.org/b.jpg"]


def test_an_error_page_keeps_no_pictures():
    html = ("<title>Page not found | Ars</title><article><h1>Page not found</h1>"
            "<figure><img src='/shark.png' alt='Moon shark eating the link'>"
            "<figcaption>Alas, the Moonshark has eaten your link.</figcaption></figure></article>")
    assert ri.find_page_images(BeautifulSoup(html, "html.parser"), "https://x.org/") == []


def test_one_picture_in_two_crops_is_kept_once():
    html = ("<article><figure><img src='/cloud-1536x836.jpg' alt='Image of cloud on Mars'>"
            "<figcaption>The cloud forms near Arsia Mons.</figcaption></figure><p>Text</p>"
            "<figure><img src='/cloud-1536x864.jpg' alt='Image of cloud on Mars'>"
            "<figcaption>The cloud forms near Arsia Mons.</figcaption></figure></article>")
    found = ri.find_page_images(BeautifulSoup(html, "html.parser"), "https://x.org/")
    assert [c.url for c in found] == ["https://x.org/cloud-1536x836.jpg"]


def test_wrapper_classes_around_the_article_do_not_hide_its_pictures():
    html = ("<div class='layout with-sidebar menu-open'><article><p>Text</p>"
            "<img src='/fig.jpg' alt='Diagram of the cooling loop design'></article></div>")
    found = ri.find_page_images(BeautifulSoup(html, "html.parser"), "https://x.org/")
    assert [c.url for c in found] == ["https://x.org/fig.jpg"]


# ── normalising ──────────────────────────────────────────────────────


def test_normalize_keeps_small_jpeg_bytes_and_converts_webp():
    jpeg = _jpeg()
    assert ri.normalize_image(jpeg) == (jpeg, "image/jpeg", 800, 500)
    body, mime, w, h = ri.normalize_image(_webp())
    assert mime == "image/jpeg" and (w, h) == (900, 600)
    assert ri.sniff_mime(body[:16]) == "image/jpeg"


def test_normalize_refuses_icons_banners_and_junk():
    assert ri.normalize_image(_png(64, 64)) is None
    assert ri.normalize_image(_png(1600, 200)) is None   # banner-shaped
    assert ri.normalize_image(b"<svg xmlns='http://www.w3.org/2000/svg'/>") is None
    assert ri.normalize_image(_png(64, 64), check_size=False) is not None


def test_normalize_downscales_and_keeps_transparency():
    body, mime, w, h = ri.normalize_image(_png(3200, 2000, alpha=True))
    assert mime == "image/png" and max(w, h) == 1600


# ── the store ────────────────────────────────────────────────────────


def test_store_save_find_index_and_dedupe(tmp_path):
    store = _store(tmp_path)
    meta = ri.PageImage(url=HERO, alt="The X300 battery pack", caption="Ships in Q3")
    data = _jpeg()
    entry = store.save(data, "image/jpeg", 800, 500, url=HERO, page_url=PAGE, meta=meta)
    assert entry.name.startswith("ships-in-q3-") and entry.name.endswith(".jpg")
    assert entry.shown == f"saved/media/s1/web-images/{entry.name}"
    assert Path(entry.path).read_bytes() == data
    sidecar = json.loads((store.directory / f"{entry.name}.json").read_text())
    assert sidecar["url"] == HERO and "path" not in sidecar
    again = store.save(data, "image/jpeg", 800, 500, url="https://mirror/x.jpg")
    assert again.name == entry.name
    assert len(store.entries()) == 1
    assert store.find(url=HERO).name == entry.name
    assert store.for_page(PAGE)[0].name == entry.name
    index = (store.directory / ri.INDEX_NAME).read_text()
    assert entry.shown in index and HERO in index


async def test_save_page_images_keeps_the_good_ones_with_referer(tmp_path, fake_client):
    found = ri.find_page_images(BeautifulSoup(ARTICLE, "html.parser"), PAGE)
    kept = await ri.save_page_images(found, PAGE, _store(tmp_path), limit=4, budget_s=5)
    assert [e.url for e in kept] == [HERO, CHART]
    assert all(h.get("Referer") == PAGE for _, h in fake_client.calls)
    block = ri.format_saved(kept)
    assert block.startswith("[Images — 2 relevant-looking picture(s)")
    assert kept[0].shown in block and f"original {HERO}" in block


async def test_a_failing_candidate_lets_the_next_one_in(tmp_path, fake_client):
    fake_client.routes[HERO] = (403, "text/html", b"denied")
    found = ri.find_page_images(BeautifulSoup(ARTICLE, "html.parser"), PAGE)
    kept = await ri.save_page_images(found, PAGE, _store(tmp_path), limit=1, budget_s=5)
    assert [e.url for e in kept] == [CHART]


# ── placeholders ─────────────────────────────────────────────────────


def test_collapse_and_restore_round_trip():
    b64 = base64.b64encode(_jpeg()).decode()
    html = f'<p>a</p><img src="data:image/jpeg;base64,{b64}" alt="x"><p>b</p>'
    collapsed, mapping = ri.collapse_inline_images(html)
    assert b64 not in collapsed and "[inline image " in collapsed
    assert len(collapsed) < 200
    restored, n, missing = ri.restore_inline_images(collapsed, mapping)
    assert restored == html and n == 1 and missing == []


def test_restore_finds_a_saved_picture_by_its_id(tmp_path):
    store = _store(tmp_path)
    entry = store.save(_jpeg(), "image/jpeg", 800, 500, url=HERO)
    text = f'<img src="[inline image {entry.sha8} · 40 KB]">'
    restored, n, _ = ri.restore_inline_images(text, {}, [store])
    assert n == 1 and 'src="data:image/jpeg;base64,' in restored


# ── reports ──────────────────────────────────────────────────────────


async def test_embed_html_saved_local_and_remote(tmp_path, fake_client):
    store = _store(tmp_path)
    saved = store.save(_jpeg(), "image/jpeg", 800, 500, url=HERO)
    chart = tmp_path / "saved" / "media" / "s1" / "chart.png"
    chart.write_bytes(_png())
    html = (f'<img src="{saved.shown}" srcset="{HERO} 2x" alt="hero">'
            '<img src="../media/s1/chart.png">'
            f'<img src="{CHART}" alt="remote chart">')
    report_path = tmp_path / "saved" / "showcase" / "report.html"
    report_path.parent.mkdir(parents=True)
    out, rep = await ri.embed_html_images(html, _kw(tmp_path), file_path=report_path)
    assert rep.embedded == 3 and not rep.failed
    assert out.count('src="data:image/') == 3
    assert "srcset" not in out and "http" not in out
    assert store.find(url=CHART) is not None  # the remote one is kept for next time


async def test_embed_html_refuses_non_images_and_outside_files(tmp_path, monkeypatch):
    monkeypatch.setattr(ri, "make_client", lambda: FakeImageClient({}))
    (tmp_path / "secret.txt").write_text("hunter2")
    outside = tmp_path.parent / "elsewhere.png"
    outside.write_bytes(_png())
    html = f'<img src="{tmp_path / "secret.txt"}"><img src="{outside}"><img src="missing.png">'
    out, rep = await ri.embed_html_images(html, _kw(tmp_path))
    assert out == html and rep.embedded == 0 and len(rep.failed) == 3
    assert "could not be embedded" in rep.note()


def test_markdown_links_saved_pictures_by_original_url(tmp_path):
    store = _store(tmp_path)
    saved = store.save(_jpeg(), "image/jpeg", 800, 500, url=HERO)
    md = f"![hero]({saved.shown})\n![chart](charts/own.png)\n![web]({CHART})"
    out, rep = ri.link_markdown_images(md, _kw(tmp_path))
    assert out == f"![hero]({HERO})\n![chart](charts/own.png)\n![web]({CHART})"
    assert rep.linked == 1


# ── the tools ────────────────────────────────────────────────────────


async def test_write_embeds_html_and_links_markdown(tmp_path):
    saved = _store(tmp_path).save(_jpeg(), "image/jpeg", 800, 500, url=HERO)
    tool = WriteTool()
    res = await tool.execute(path="showcase/report.html",
                             content=f'<figure><img src="{saved.shown}"></figure>', **_kw(tmp_path))
    assert res.success, res.error
    written = (tmp_path / "saved" / "showcase" / "s1" / "report.html").read_text()
    assert 'src="data:image/jpeg;base64,' in written
    assert "Embedded 1 picture(s)" in res.content
    res = await tool.execute(path="showcase/report.md", content=f"![hero]({saved.shown})",
                             **_kw(tmp_path))
    assert (tmp_path / "saved" / "showcase" / "s1" / "report.md").read_text() == f"![hero]({HERO})"


async def test_write_in_code_mode_leaves_img_references(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAW_WRITE_DIRECT", "1")
    (tmp_path / "logo.png").write_bytes(_png())
    res = await WriteTool().execute(path="index.html", content='<img src="logo.png">',
                                    **_kw(tmp_path))
    assert res.success
    assert (tmp_path / "index.html").read_text() == '<img src="logo.png">'


async def test_rewriting_a_report_from_its_placeholders_keeps_pictures(tmp_path):
    saved = _store(tmp_path).save(_jpeg(), "image/jpeg", 800, 500, url=HERO)
    tool = WriteTool()
    body = "<p>" + "Findings. " * 150 + "</p>"
    await tool.execute(path="showcase/r.html", content=f'{body}<img src="{saved.shown}">',
                       **_kw(tmp_path))
    target = tmp_path / "saved" / "showcase" / "s1" / "r.html"
    read = await ReadTool().execute(path=str(target))
    assert "[inline image " in read.content and "placeholders" in read.content
    view = read.content.split("\n", 1)[1]
    # A full rewrite from the read view is not a "shrink" and keeps the picture.
    res = await tool.execute(path="showcase/r.html", content=view.replace("Findings", "Results"),
                             **_kw(tmp_path))
    assert res.success, res.content
    text = target.read_text()
    assert "Results" in text and 'src="data:image/jpeg;base64,' in text
    assert "[inline image " not in text


async def test_edit_works_on_placeholders_and_embeds_new_pictures(tmp_path, fake_client):
    saved = _store(tmp_path).save(_jpeg(), "image/jpeg", 800, 500, url=HERO)
    target = tmp_path / "saved" / "showcase" / "s1" / "r.html"
    target.parent.mkdir(parents=True)
    b64 = base64.b64encode(Path(saved.path).read_bytes()).decode()
    target.write_text(f'<h1>Old</h1><img src="data:image/jpeg;base64,{b64}">')
    tool = EditTool()
    res = await tool.execute(path=str(target), edits=[
        {"old_string": "<h1>Old</h1>", "new_string": f'<h1>New</h1><img src="{CHART}">'},
    ], **_kw(tmp_path))
    assert res.success, res.error
    text = target.read_text()
    assert text.startswith("<h1>New</h1>")
    assert b64 in text and text.count('src="data:image/') == 2
    assert "Embedded 1 picture(s)" in res.content


async def test_edit_size_limit_ignores_inline_pictures(tmp_path):
    from captain_claw.config import get_config

    limit = get_config().tools.edit.max_file_bytes
    big = base64.b64encode(b"\xff\xd8\xff" + b"\x00" * (limit + 10)).decode()
    target = tmp_path / "big.html"
    target.write_text(f'<p>Hi</p><img src="data:image/jpeg;base64,{big}">')
    res = await EditTool().execute(path=str(target), old_string="<p>Hi</p>",
                                   new_string="<p>Hello</p>", **_kw(tmp_path))
    assert res.success, res.error
    assert target.read_text() == f'<p>Hello</p><img src="data:image/jpeg;base64,{big}">'


class _Page:
    def __init__(self, text: str):
        self.text = text
        self.status_code = 200
        self.headers = {"content-type": "text/html"}

    def raise_for_status(self):
        return None


class _PageClient:
    def __init__(self, pages: dict[str, str]):
        self.pages = pages

    async def get(self, url, **_):
        return _Page(self.pages[url])

    async def aclose(self):
        return None


async def test_web_fetch_lists_the_saved_pictures(tmp_path, fake_client):
    tool = WebFetchTool()
    tool.client = _PageClient({PAGE: ARTICLE})
    res = await tool.execute(url=PAGE, deep_fetch=False, **_kw(tmp_path))
    assert res.success, res.error
    assert "[Images — 2 relevant-looking picture(s) saved from this page." in res.content
    assert "saved/media/s1/web-images/" in res.content
    assert len(_store(tmp_path).entries()) == 2


async def test_web_fetch_with_pictures_off_is_unchanged(tmp_path, fake_client, monkeypatch):
    from captain_claw.config import get_config

    monkeypatch.setattr(get_config().tools.web_fetch, "save_images", False)
    tool = WebFetchTool()
    tool.client = _PageClient({PAGE: ARTICLE})
    res = await tool.execute(url=PAGE, deep_fetch=False, **_kw(tmp_path))
    assert "[Images" not in res.content and fake_client.calls == []


async def test_web_fetch_on_an_image_url_saves_it(tmp_path, fake_client):
    url = "https://acme.com/img/photo.webp"
    fake_client.routes[url] = (200, "image/webp", _webp(400, 300))
    res = await WebFetchTool().execute(url=url, **_kw(tmp_path))
    assert res.success, res.error
    assert "[Image saved: saved/media/s1/web-images/" in res.content
    assert _store(tmp_path).find(url=url).mime == "image/jpeg"


async def test_web_fetch_batch_appends_each_pages_pictures(tmp_path, fake_client):
    other = "https://acme.com/plain"
    tool = WebFetchBatchTool()
    tool.client = _PageClient({PAGE: ARTICLE, other: "<p>" + "plain text " * 80 + "</p>"})
    res = await tool.execute(urls=[PAGE, other], **_kw(tmp_path))
    assert res.success, res.error
    first, second = res.content.split("\n\n---\n\n")
    assert "[Images — 2 relevant-looking picture(s)" in first
    assert "[Images" not in second
    assert fake_client.closed


def test_member_gets_original_urls_only():
    found = ri.find_page_images(BeautifulSoup(ARTICLE, "html.parser"), PAGE)
    block = ri.format_unsaved(found, 4)
    assert HERO in block and "saved/" not in block
