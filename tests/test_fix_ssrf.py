from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import replace

import httpx
import pytest

from app import main
from app.db import init_db
from app.service import create_device


class FakeResponse:
    def __init__(self, status_code=200, headers=None, chunks=None):
        self.status_code = status_code
        self.headers = httpx.Headers(headers or {})
        self._chunks = list(chunks or [])
        self.closed = False

    @property
    def is_redirect(self) -> bool:
        return self.status_code in (301, 302, 303, 307, 308) and "location" in self.headers

    async def aclose(self) -> None:
        self.closed = True

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            req = httpx.Request("GET", "http://test")
            raise httpx.HTTPStatusError(
                str(self.status_code),
                request=req,
                response=httpx.Response(self.status_code, request=req),
            )

    async def aiter_bytes(self, chunk_size=None):
        for chunk in self._chunks:
            yield chunk


class FakeAsyncClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.sent: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def build_request(self, method, url, headers=None):
        return httpx.Request(method, url, headers=headers)

    async def send(self, request, stream=False):
        self.sent.append(str(request.url))
        return self._responses.pop(0)


def patch_client(monkeypatch, responses):
    client = FakeAsyncClient(responses)
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda *a, **k: client)
    return client


def image_response(payload: bytes, content_type="image/png"):
    return FakeResponse(
        status_code=200,
        headers={"content-type": content_type},
        chunks=[payload],
    )


def test_scheme_must_be_http_or_https(monkeypatch):
    client = patch_client(monkeypatch, [image_response(b"\x89PNG")])
    for url in ["file:///etc/passwd", "ftp://example.com/a.png", "data:image/png;base64,AAAA", "gopher://x/1"]:
        with pytest.raises(ValueError, match="scheme"):
            asyncio.run(main.download_image_url(url))
    # No outbound request should have been issued for a rejected scheme.
    assert client.sent == []


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x.png",
        "http://10.0.0.5/x.png",
        "http://192.168.1.1/x.png",
        "http://169.254.169.254/latest/meta-data/",
        "http://0.0.0.0/x.png",
        "http://[::1]/x.png",
        "http://[::ffff:127.0.0.1]/x.png",
        "http://224.0.0.1/x.png",
    ],
)
def test_internal_and_mapped_addresses_rejected(monkeypatch, url):
    client = patch_client(monkeypatch, [image_response(b"\x89PNG")])
    with pytest.raises(ValueError):
        asyncio.run(main.download_image_url(url))
    assert client.sent == []


def test_all_resolved_addresses_are_validated(monkeypatch):
    # A hostname resolving to both a public and a private address must be rejected.
    monkeypatch.setattr(main, "resolve_host_ips", lambda host: ["93.184.216.34", "10.0.0.1"])
    with pytest.raises(ValueError, match="10.0.0.1"):
        main.validate_image_url("http://example.com/x.png")


def test_public_literal_ip_is_allowed(monkeypatch):
    client = patch_client(monkeypatch, [image_response(b"\x89PNG\x00data")])
    data, content_type = asyncio.run(main.download_image_url("http://93.184.216.34/x.png"))
    assert data == b"\x89PNG\x00data"
    assert content_type == "image/png"
    assert client.sent == ["http://93.184.216.34/x.png"]


def test_redirect_is_revalidated_each_hop(monkeypatch):
    redirect = FakeResponse(status_code=302, headers={"location": "http://127.0.0.1/steal.png"})
    client = patch_client(monkeypatch, [redirect])
    with pytest.raises(ValueError):
        asyncio.run(main.download_image_url("http://93.184.216.34/x.png"))
    # Only the first hop was fetched; the private redirect target was never requested.
    assert client.sent == ["http://93.184.216.34/x.png"]
    assert redirect.closed is True


def test_redirect_chain_within_limit_follows_public_hops(monkeypatch):
    redirect = FakeResponse(status_code=301, headers={"location": "http://93.184.216.34/final.png"})
    client = patch_client(monkeypatch, [redirect, image_response(b"ok")])
    data, _ = asyncio.run(main.download_image_url("http://93.184.216.34/x.png"))
    assert data == b"ok"
    assert client.sent == ["http://93.184.216.34/x.png", "http://93.184.216.34/final.png"]


def test_download_stops_when_size_limit_exceeded(monkeypatch):
    monkeypatch.setattr(main, "MAX_GENERATED_IMAGE_BYTES", 10)
    response = FakeResponse(
        status_code=200,
        headers={"content-type": "image/png"},
        chunks=[b"aaaa", b"bbbb", b"cccc"],
    )
    patch_client(monkeypatch, [response])
    with pytest.raises(ValueError, match="too large"):
        asyncio.run(main.download_image_url("http://93.184.216.34/x.png"))


def test_non_image_content_type_rejected(monkeypatch):
    patch_client(monkeypatch, [image_response(b"<html>", content_type="text/html")])
    with pytest.raises(ValueError, match="did not return an image"):
        asyncio.run(main.download_image_url("http://93.184.216.34/x.png"))


def test_atomic_write_leaves_no_temp_files(tmp_path):
    target = tmp_path / "day" / "image.png"
    main.write_bytes_atomic(target, b"\x89PNGdata")
    assert target.read_bytes() == b"\x89PNGdata"
    leftovers = [p.name for p in target.parent.iterdir() if p.name.startswith(".tmp-")]
    assert leftovers == []


def test_persist_annotates_failed_download_and_logs(monkeypatch, tmp_path, caplog):
    db = tmp_path / "test.sqlite3"
    init_db(db)
    create_device(db, "BTF:SSRF", 30)
    monkeypatch.setattr(
        main,
        "settings",
        replace(
            main.settings,
            database_path=db,
            generated_images_dir=tmp_path / "generated-images",
            public_base_url="http://testserver",
        ),
    )
    payload = {"data": [{"url": "http://127.0.0.1/secret.png"}]}
    with caplog.at_level("WARNING", logger="btf_device_gateway"):
        result = json.loads(
            asyncio.run(
                main.persist_image_generation_response(
                    json.dumps(payload).encode(),
                    "application/json",
                    "BTF:SSRF",
                    None,
                    "month_1",
                    "btf-image",
                    "[b2]gpt-image-2",
                    json.dumps({"prompt": "x"}).encode(),
                    None,
                )
            )
        )
    assert "btf_image_error" in result["data"][0]
    assert "url" in result["data"][0]  # original url left untouched
    assert any("persistence failed" in record.message for record in caplog.records)


def test_persist_still_persists_inline_b64(monkeypatch, tmp_path):
    db = tmp_path / "test.sqlite3"
    image_dir = tmp_path / "generated-images"
    init_db(db)
    create_device(db, "BTF:INLINE", 30)
    monkeypatch.setattr(
        main,
        "settings",
        replace(main.settings, database_path=db, generated_images_dir=image_dir, public_base_url="http://testserver"),
    )
    payload = {
        "data": [{"b64_json": base64.b64encode(b"\x89PNG\r\n\x1a\ninline").decode()}],
    }
    result = json.loads(
        asyncio.run(
            main.persist_image_generation_response(
                json.dumps(payload).encode(),
                "application/json",
                "BTF:INLINE",
                None,
                "month_1",
                "btf-image",
                "[b2]gpt-image-2",
                json.dumps({"prompt": "x"}).encode(),
                None,
            )
        )
    )
    relative_path = result["data"][0]["btf_local_path"]
    assert (image_dir / relative_path).is_file()
