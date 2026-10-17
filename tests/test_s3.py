"""Unit tests for the pure S3 helpers in `pylibrus.storage.s3`: source parsing, object keys and
download headers. `boto3.client` is faked, so no AWS call happens."""

import boto3
import pytest

from pylibrus.db.schema import Attachment
from pylibrus.settings import WebhookNotify
from pylibrus.storage.s3 import (
    S3AttachmentStorage,
    build_download_content_disposition,
    is_s3_webhook_source,
    parse_s3_source,
    sanitize_s3_segment,
)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("s3://bucket/some/prefix/", ("bucket", "some/prefix")),
        ("s3://bucket", ("bucket", "")),
        ("s3://bucket/", ("bucket", "")),
        ("s3://", (None, "")),
        ("s3:///prefix", (None, "")),
        ("librus_link", (None, "")),
        ("", (None, "")),
        (None, (None, "")),
    ],
)
def test_parse_s3_source(source, expected):
    assert parse_s3_source(source) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [("s3://bucket", True), ("librus_link", False), ("", False), (None, False)],
)
def test_is_s3_webhook_source(source, expected):
    assert is_s3_webhook_source(source) is expected


@pytest.mark.parametrize(
    ("segment", "expected"),
    [
        ("Ania Kowalska", "Ania_Kowalska"),
        ("1002/77", "1002_77"),
        ("  zgoda.v2-final.pdf  ", "zgoda.v2-final.pdf"),
        ("Zgoda Żółć.pdf", "Zgoda_.pdf"),  # a run of disallowed characters becomes one underscore
        ("", "item"),
        (None, "item"),
    ],
)
def test_sanitize_s3_segment(segment, expected):
    assert sanitize_s3_segment(segment) == expected


def test_content_disposition_keeps_an_ascii_fallback_and_the_utf8_name():
    assert build_download_content_disposition("zgoda żółć.pdf") == (
        "attachment; filename=\"zgoda .pdf\"; filename*=UTF-8''zgoda%20%C5%BC%C3%B3%C5%82%C4%87.pdf"
    )


def test_content_disposition_falls_back_when_nothing_is_ascii():
    assert build_download_content_disposition("żółć").startswith('attachment; filename="attachment";')


@pytest.fixture
def storage(monkeypatch):
    def make(source):
        monkeypatch.setattr(boto3, "client", lambda service, **kwargs: object())
        notify = WebhookNotify(
            webhook="https://hooks.example.com",
            webhook_attachments_source=source,
            s3_region="eu-central-1",
            s3_access_key_id="AKIA",
            s3_secret_access_key="shh",
        )
        return S3AttachmentStorage(notify)

    return make


def test_object_key_is_prefix_user_message_hash_and_attachment(storage):
    attachment = Attachment(link_id="1002/77", name="zgoda rodzica.pdf")

    key = storage("s3://bucket/librus/").build_object_key("Ania Kowalska", "/wiadomosci/1/5/1002/f0", attachment)

    # The message segment is sha1(msg_path)[:12], so the same message always maps to the same key.
    assert key == "librus/Ania_Kowalska/36ee9d5938a8/1002_77_zgoda_rodzica.pdf"


def test_object_key_without_prefix_has_no_leading_segment(storage):
    attachment = Attachment(link_id="1002/77", name="zgoda.pdf")

    key = storage("s3://bucket").build_object_key("Ania", "/wiadomosci/1/5/1002/f0", attachment)

    assert key == "Ania/36ee9d5938a8/1002_77_zgoda.pdf"
