"""AA-708 — Drive photo sync: labels, tour/destination matching, folder walk, WebP sizes, upsert args."""
import io

import pytest

from services.photos import match as M
from services.photos import sync as S
from services.photos.drive import FOLDER_MIME, DriveFile, walk


def test_place_label_strips_extension_and_counters():
    assert M.place_label("Olkhon Island1.jpeg") == "Olkhon Island"
    assert M.place_label("Bukchon_Hanok-Village (2).JPG") == "Bukchon Hanok Village"
    assert M.place_label("Gyeongbokgung Palace copy.png") == "Gyeongbokgung Palace"


def test_match_tour_exact_fuzzy_and_ambiguous():
    tours = [("t1", "THE DRUK PATH"), ("t2", "LAYA-GASA TREK"), ("t3", "Laya Gasa Trek Short")]
    assert M.match_tour("The Druk Path", tours) == "t1"
    assert M.match_tour("Laya-Gasa Trek ", tours) == "t2"
    assert M.match_tour("Paro", tours) is None
    assert M.match_tour(None, tours) is None


def test_tour_title_strips_real_folder_suffixes():
    assert M.tour_title("Best of Bhutan - 9 days") == "Best of Bhutan"
    assert M.tour_title("Dabajianshan Trek 3 Day / 2 Night (Guided)") == "Dabajianshan Trek"
    assert M.tour_title("GB-01 MANILA and SUBURBS - 4 hours") == "MANILA and SUBURBS"
    assert M.tour_title("Beijing, Xi'an and Shanghai — Nine Days") == "Beijing, Xi'an and Shanghai"


def test_match_tour_uses_rewritten_name_and_word_share():
    tours = [("k1", "Guided Seoul to Busan Bike Tour", "Seoul to Busan: Eight Days by Bicycle"),
             ("b1", "BEST OF BHUTAN", None)]
    assert M.match_tour("Seoul to Busan: Eight Days by Bicycle - 8 DAYS", tours) == "k1"
    assert M.match_tour("Best of Bhutan - 9 days", tours) == "b1"


def test_match_destination_longest_whole_word_unique():
    dests = [("d1", "Olkhon Island"), ("d2", "Olkhon"), ("d3", "Lake Baikal")]
    assert M.match_destination("Olkhon Island sunset", dests) == "d1"
    assert M.match_destination("Lake Baikal", dests) == "d3"
    assert M.match_destination("Ulaanbaatar", dests) is None
    assert M.match_destination("Paro", [("a", "Paro"), ("b", "paro")]) == "a"   # exact wins first


class _FakeDrive:
    def __init__(self, tree, blobs=None):
        self.tree, self.blobs = tree, blobs or {}

    async def list_children(self, folder_id):
        return self.tree.get(folder_id, [])

    async def download(self, file_id, thumbnail_link=None):
        return self.blobs[file_id]


def _img(fid, name, mod="2026-09-01T00:00:00Z"):
    return DriveFile(fid, name, "image/jpeg", mod, 1000)


@pytest.mark.asyncio
async def test_walk_keeps_folder_path_and_skips_sheets():
    tree = {"root": [DriveFile("f1", "THE DRUK PATH", FOLDER_MIME, None, None), _img("i0", "Paro.jpg"),
                     DriveFile("s1", "list", "application/vnd.google-apps.spreadsheet", None, None)],
            "f1": [_img("i1", "Tango Monastery1.jpg")]}
    found = await walk(_FakeDrive(tree), "root")
    assert sorted((p, f.id) for p, f in found) == [([], "i0"), (["THE DRUK PATH"], "i1")]


def _jpeg(w=2400, h=1600) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 120, 40)).save(buf, "JPEG")
    return buf.getvalue()


def test_make_sizes_webp_widths():
    from PIL import Image
    sizes, w, h = S.make_sizes(_jpeg())
    assert (w, h) == (2400, 1600)
    assert Image.open(io.BytesIO(sizes["large"])).size[0] == 1600
    assert Image.open(io.BytesIO(sizes["small"])).size[0] == 600
    assert S.s3_key("South Korea", "abc", "small") == "photos/south-korea/abc-600.webp"


class _Conn:
    def __init__(self, existing=()):
        self.existing, self.writes = list(existing), []

    async def fetch(self, sql, *a):
        if "tripplanner" in sql:
            return [{"tour_id": "11111111-1111-1111-1111-111111111111",
                     "id": "22222222-2222-2222-2222-222222222222", "name": "Tango Monastery"}]
        if "raw_tours" in sql:
            return [{"tour_id": "11111111-1111-1111-1111-111111111111", "src_name": "THE DRUK PATH", "aa_name": None}]
        if "shared.destinations" in sql:
            return [{"id": "22222222-2222-2222-2222-222222222222", "name": "Tango Monastery"}]
        if "place_photo" in sql:
            return self.existing
        return []

    async def execute(self, sql, *a):
        self.writes.append(a)


class _S3:
    def __init__(self):
        self.keys = []

    def put_object(self, **kw):
        self.keys.append(kw["Key"])


@pytest.mark.asyncio
async def test_sync_matches_tour_only_by_default_and_uploads_two_sizes():
    tree = {"root": [DriveFile("sup", "Wangchuk Tour&Trek", FOLDER_MIME, None, None)],
            "sup": [DriveFile("f1", "The Druk Path - 8 days", FOLDER_MIME, None, None)],
            "f1": [_img("i1", "Tango Monastery1.jpg")]}
    conn, s3 = _Conn(), _S3()
    res = await S.sync_folder(conn, _FakeDrive(tree, {"i1": _jpeg()}), s3, country="Bhutan", root_id="root")
    assert res["matched_tour"] == 1 and res["matched_destination"] == 0 and res["downloaded"] == 1
    assert sorted(s3.keys) == ["photos/bhutan/i1-1600.webp", "photos/bhutan/i1-600.webp"]
    a = conn.writes[0]
    assert a[4] == "The Druk Path - 8 days" and a[2] == "Wangchuk Tour&Trek › The Druk Path - 8 days"
    assert a[7] == "11111111-1111-1111-1111-111111111111" and a[8] is None and a[9] == "matched"
    assert a[-1] is False                                   # match_destinations off


@pytest.mark.asyncio
async def test_sync_matches_destination_when_asked_and_skips_unchanged_download():
    from datetime import datetime, timezone
    tree = {"root": [DriveFile("f1", "THE DRUK PATH", FOLDER_MIME, None, None)],
            "f1": [_img("i1", "Tango Monastery1.jpg")]}
    existing = [{"drive_file_id": "i1", "drive_modified_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
                 "s3_key_small": "photos/bhutan/i1-600.webp", "match_source": "auto", "status": "matched"}]
    conn, s3 = _Conn(existing), _S3()
    res = await S.sync_folder(conn, _FakeDrive(tree, {}), s3, country="Bhutan", root_id="root",
                              match_destinations=True)
    assert res["unchanged"] == 1 and res["downloaded"] == 0 and s3.keys == []
    assert conn.writes[0][8] == "22222222-2222-2222-2222-222222222222"


@pytest.mark.asyncio
async def test_sync_records_error_for_bad_image():
    tree = {"root": [_img("i9", "broken.jpg")]}
    conn = _Conn()
    res = await S.sync_folder(conn, _FakeDrive(tree, {"i9": b"not an image"}), _S3(), country="Bhutan",
                              root_id="root")
    assert res["errors"] == 1
    assert conn.writes[0][17]                                # error text stored


def test_migration_198_and_job_registered():
    from pathlib import Path
    sql = (Path(__file__).resolve().parents[2] / "api/migrations/198_place_photo.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS shared.place_photo" in sql and "drive_file_id     TEXT NOT NULL UNIQUE" in sql
    import services.jobs  # noqa: F401
    from shared.jobs.registry import _KINDS
    assert "photo_sync" in _KINDS


@pytest.mark.asyncio
async def test_destination_only_from_the_tours_own_itinerary():
    """A place on another tour's itinerary is not a candidate (S207: narrow by itinerary)."""
    tree = {"root": [DriveFile("f1", "Unknown Folder", FOLDER_MIME, None, None)],
            "f1": [_img("i1", "Tango Monastery1.jpg")]}
    conn = _Conn()
    res = await S.sync_folder(conn, _FakeDrive(tree, {"i1": _jpeg()}), _S3(), country="Bhutan", root_id="root",
                              match_destinations=True)
    assert res["matched_tour"] == 0 and res["matched_destination"] == 0      # no tour -> no place


def test_migration_200_read_views():
    from pathlib import Path
    sql = (Path(__file__).resolve().parents[2] / "api/migrations/200_photo_read_views.sql").read_text()
    assert "CREATE OR REPLACE VIEW shared.v_tour_photos" in sql
    assert "CREATE OR REPLACE VIEW shared.v_destination_photos" in sql
    assert "p.status = 'matched'" in sql


@pytest.mark.asyncio
async def test_api_key_goes_in_header_never_in_url_or_error():
    import httpx
    from services.photos import drive as Dr
    seen = {}

    def handler(request):
        seen["url"], seen["key"] = str(request.url), request.headers.get("X-Goog-Api-Key")
        return httpx.Response(403, json={"error": {"message": "nope key=SECRET123",
                                                   "errors": [{"reason": "forbidden"}]}})
    c = Dr.DriveClient({"api_key": "SECRET123"}, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(Dr.DriveError) as ei:
        await c.download("f1")
    assert seen["key"] == "SECRET123" and "SECRET123" not in seen["url"]
    assert "SECRET123" not in str(ei.value) and "forbidden" in str(ei.value)


@pytest.mark.asyncio
async def test_rate_limit_is_retried(monkeypatch):
    import httpx
    from services.photos import drive as Dr
    monkeypatch.setattr(Dr, "RETRY_DELAYS", (0, 0))
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(403, json={"error": {"errors": [{"reason": "userRateLimitExceeded"}]}})
        return httpx.Response(200, content=b"img")
    c = Dr.DriveClient({"api_key": "k"}, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    c._last_download = -100
    monkeypatch.setattr(Dr, "MIN_DOWNLOAD_INTERVAL", 0)
    assert await c.download("f1") == b"img" and calls["n"] == 3


@pytest.mark.asyncio
async def test_sync_stops_after_consecutive_failures():
    tree = {"root": [_img(f"i{n}", f"p{n}.jpg") for n in range(12)]}
    with pytest.raises(S.DriveQuotaStop):
        await S.sync_folder(_Conn(), _FakeDrive(tree, {}), _S3(), country="Bhutan", root_id="root")


@pytest.mark.asyncio
async def test_download_prefers_thumbnail_link_then_media(monkeypatch):
    import httpx
    from services.photos import drive as Dr
    monkeypatch.setattr(Dr, "MIN_DOWNLOAD_INTERVAL", 0)
    hits = []

    def handler(request):
        hits.append(request.url.host)
        if request.url.host == "lh3.googleusercontent.com":
            assert str(request.url).endswith("=s2000")
            return httpx.Response(200, content=b"thumb", headers={"content-type": "image/jpeg"})
        return httpx.Response(200, content=b"media")
    c = Dr.DriveClient({"api_key": "k"}, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await c.download("f1", "https://lh3.googleusercontent.com/drive-storage/abc=s220") == b"thumb"
    assert await c.download("f2") == b"media"
    assert hits == ["lh3.googleusercontent.com", "www.googleapis.com"]
