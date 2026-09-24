from __future__ import annotations

import io
import zipfile

import pytest

from life_helper.knowledge.store import KnowledgePathError, KnowledgeStore, convert_upload, normalize_filename

from .conftest import sign_in


@pytest.fixture
def store(tmp_path, settings):
    s = KnowledgeStore(tmp_path / "kb", settings.seed_dir)
    s.ensure_seeded()
    return s


def test_seed_creates_structure(store):
    for d in ("profile", "memories", "notes", "plans", "money", "docs"):
        assert (store.root / d).is_dir()
    assert (store.root / "INDEX.md").exists()
    assert (store.root / "profile" / "about-me.md").exists()


def test_seed_does_not_overwrite(store):
    (store.root / "INDEX.md").write_text("custom", encoding="utf-8")
    store.ensure_seeded()
    assert (store.root / "INDEX.md").read_text(encoding="utf-8") == "custom"


@pytest.mark.parametrize(
    "bad", ["../x.md", "/etc/passwd", "C:/Windows/x.md", "notes/../../x.md", "notes\\..\\..\\x", ""]
)
def test_resolve_rejects_escapes(store, bad):
    with pytest.raises(KnowledgePathError):
        store.resolve(bad)


def test_write_only_in_user_areas(store):
    assert store.write_text("notes/a.md", "hello") == "notes/a.md"
    assert store.write_text("INDEX.md", "# idx") == "INDEX.md"
    for rel in ("money/portfolio.yaml", "notes/a.py", "secret.md", "memories/x.exe"):
        with pytest.raises(KnowledgePathError):
            store.write_text(rel, "x")


def test_list_hides_price_cache(store):
    (store.root / "money" / "prices" / "2026-01-01.json").write_text("{}", encoding="utf-8")
    paths = [e.path for e in store.list_files()]
    assert "INDEX.md" in paths
    assert not any(p.startswith("money/prices/") for p in paths)


def test_normalize_filename():
    assert normalize_filename("../../etc/pass wd.TXT") == "pass_wd.txt"
    assert normalize_filename("家計簿 2026.md") == "家計簿_2026.md"


def test_convert_upload_rejects_other_types_and_fake_pdf():
    with pytest.raises(KnowledgePathError):
        convert_upload("x.exe", b"MZ")
    with pytest.raises(KnowledgePathError):
        convert_upload("x.pdf", b"not a pdf")


def test_convert_text_adds_header():
    md = convert_upload("memo.txt", "本文".encode())
    assert md.startswith("# memo") and "本文" in md


def test_api_requires_auth(client):
    assert client.get("/api/files").status_code == 401


def test_api_crud_upload_export(client, ctx):
    csrf = sign_in(client, ctx)
    h = {"x-csrf-token": csrf}
    assert any(f["path"] == "INDEX.md" for f in client.get("/api/files").json())
    assert (
        client.put("/api/files/content", json={"path": "notes/n.md", "content": "メモ"}, headers=h).status_code == 200
    )
    assert client.get("/api/files/content", params={"path": "notes/n.md"}).json()["content"] == "メモ"
    assert client.put("/api/files/content", json={"path": "../x.md", "content": "x"}, headers=h).status_code == 400

    # Sensitive data is never stored in knowledge files from the editor.
    resp = client.put("/api/files/content", json={"path": "notes/s.md", "content": "口座番号: 1234567"}, headers=h)
    assert resp.status_code == 422 and resp.json()["detail"]["code"] == "sensitive_data"

    up = client.post("/api/files/upload", files={"file": ("家計.txt", "食費 3 万円".encode(), "text/plain")}, headers=h)
    assert up.json()["path"] == "docs/家計.md" and up.json()["redacted"] == []
    up2 = client.post("/api/files/upload", files={"file": ("家計.txt", b"x", "text/plain")}, headers=h)
    assert up2.json()["path"] == "docs/家計-2.md"
    stmt = client.post(
        "/api/files/upload",
        files={"file": ("明細.txt", "口座番号: 1234567 残高 10 万円".encode(), "text/plain")},
        headers=h,
    ).json()
    assert stmt["redacted"] == ["口座番号"]
    saved = client.get("/api/files/content", params={"path": stmt["path"]}).json()["content"]
    assert "1234567" not in saved and "残高 10 万円" in saved
    assert (
        client.post(
            "/api/files/upload", files={"file": ("a.exe", b"x", "application/octet-stream")}, headers=h
        ).status_code
        == 400
    )

    zf = zipfile.ZipFile(io.BytesIO(client.get("/api/export.zip").content))
    assert "notes/n.md" in zf.namelist() and "docs/家計.md" in zf.namelist()

    assert client.delete("/api/files", params={"path": "notes/n.md"}, headers=h).status_code == 200
    assert client.delete("/api/files", params={"path": "money/portfolio.yaml"}, headers=h).status_code == 400


def test_upload_size_limit(client, ctx, settings):
    csrf = sign_in(client, ctx)
    big = b"a" * (settings.upload_max_bytes + 1)
    resp = client.post(
        "/api/files/upload", files={"file": ("big.txt", big, "text/plain")}, headers={"x-csrf-token": csrf}
    )
    assert resp.status_code == 413
