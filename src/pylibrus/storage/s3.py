from __future__ import annotations

import hashlib
import mimetypes
import re
from typing import TYPE_CHECKING
from urllib.parse import quote

import boto3

if TYPE_CHECKING:
    from pylibrus.pylibrus import Attachment
    from pylibrus.settings import WebhookNotify

LINK_EXPIRE_DURATION = 604800  # 7 days is maximum possible for S3 pre-signed URLs


def parse_s3_source(webhook_attachments_source: str) -> tuple[str | None, str]:
    if not webhook_attachments_source or not webhook_attachments_source.startswith("s3://"):
        return None, ""
    source_without_scheme = webhook_attachments_source[len("s3://"):]
    if not source_without_scheme:
        return None, ""
    bucket_name, _, raw_prefix = source_without_scheme.partition("/")
    if not bucket_name:
        return None, ""
    return bucket_name, raw_prefix.strip("/")


def is_s3_webhook_source(webhook_attachments_source: str) -> bool:
    return bool(webhook_attachments_source and webhook_attachments_source.startswith("s3://"))


def sanitize_s3_segment(segment: str, fallback: str = "item") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (segment or "").strip())
    return cleaned or fallback


def build_download_content_disposition(file_name: str) -> str:
    safe_ascii_name = file_name.encode("ascii", "ignore").decode() or "attachment"
    encoded_file_name = quote(file_name, safe="")
    return f'attachment; filename="{safe_ascii_name}"; filename*=UTF-8\'\'{encoded_file_name}'


class S3AttachmentStorage:
    def __init__(self, webhook_notify: WebhookNotify):
        bucket_name, key_prefix = parse_s3_source(webhook_notify.webhook_attachments_source)
        if not bucket_name:
            raise ValueError("Missing bucket name in webhook_attachments_source")
        self._bucket_name = bucket_name
        self._key_prefix = key_prefix
        self._client = boto3.client(
            "s3",
            region_name=webhook_notify.s3_region,
            aws_access_key_id=webhook_notify.s3_access_key_id,
            aws_secret_access_key=webhook_notify.s3_secret_access_key,
            aws_session_token=webhook_notify.s3_session_token or None,
            endpoint_url=webhook_notify.s3_endpoint_url or None,
        )

    @staticmethod
    def _hash_msg_path(msg_path: str) -> str:
        return hashlib.sha1(msg_path.encode("utf-8")).hexdigest()[:12]

    def build_object_key(self, librus_user_name: str, msg_path: str, attachment: Attachment) -> str:
        user_segment = sanitize_s3_segment(librus_user_name, fallback="user")
        msg_segment = self._hash_msg_path(msg_path)
        attachment_segment = sanitize_s3_segment(attachment.link_id, fallback="attachment")
        name_segment = sanitize_s3_segment(attachment.name, fallback="file")
        parts = [p for p in (self._key_prefix, user_segment, msg_segment, f"{attachment_segment}_{name_segment}") if p]
        return "/".join(parts)

    def upload_attachment(self, librus_user_name: str, msg_path: str, attachment: Attachment) -> tuple[str, str]:
        object_key = attachment.s3_key or self.build_object_key(librus_user_name, msg_path, attachment)
        content_type = mimetypes.guess_type(attachment.name)[0] or "application/octet-stream"
        content_disposition = build_download_content_disposition(attachment.name)
        response = self._client.put_object(
            Bucket=self._bucket_name,
            Key=object_key,
            Body=attachment.data,
            ContentType=content_type,
            ContentDisposition=content_disposition,
        )
        return object_key, str(response.get("ETag", "")).strip('"')

    def generate_download_link(self, attachment: Attachment) -> str:
        if not attachment.s3_key:
            raise ValueError("Missing s3_key for pre-signed URL generation")
        return self._client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self._bucket_name,
                "Key": attachment.s3_key,
                "ResponseContentDisposition": build_download_content_disposition(attachment.name),
            },
            ExpiresIn=LINK_EXPIRE_DURATION,
        )
