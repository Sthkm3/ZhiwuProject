import pytest

from app import admin, config, db
from app.titles import uid_tier

from .conftest import login


@pytest.mark.parametrize("uid,tier", [
    (0, "admin"), (1, None), (114514, None), (99999999, None),
])
def test_uid_tier(uid, tier):
    assert uid_tier(uid) == tier


def test_reset_progress(client, sent, capsys):
    login(client, sent)
    client.patch("/api/me", json={"display_name": "Tester"})
    first = client.post("/api/checkin").json()
    assert client.get(first["card_url"]).status_code == 200

    # 沒有 --yes 不做任何事
    admin.main(["reset-progress"])
    assert client.get("/api/checkin/today").json()["total_days"] == 1

    admin.main(["reset-progress", "--yes"])
    today = client.get("/api/checkin/today").json()
    assert today["total_days"] == 0 and today["checked_in"] is False
    me = client.get("/api/me").json()  # 帳號、名稱、session 都還在
    assert me["display_name"] == "Tester" and me["uid_str"] == "00000001"
    assert list(config.DATA_DIR.glob("zhiwu.db.bak-*"))
    assert not any((config.DATA_DIR / "cards").iterdir())
    # 同一天可以重新簽到
    again = client.post("/api/checkin").json()
    assert again["already"] is False and again["total_days"] == 1
    assert db.connect().execute("SELECT value FROM meta WHERE key = 'next_uid'").fetchone()[0] == 2


def test_assets_versioned_and_not_cached_by_proxies(client):
    html = client.get("/").text
    assert "__ASSET_VERSION__" not in html
    assert '/assets/app.js?v=' in html and '/assets/style.css?v=' in html
    r = client.get("/assets/app.js")
    assert r.headers["cache-control"] == "no-cache"
    etag = r.headers["etag"]
    assert client.get("/assets/app.js", headers={"If-None-Match": etag}).status_code == 304
