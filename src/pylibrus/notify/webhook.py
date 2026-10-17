import datetime
import json
import logging

import requests

from pylibrus.db.schema import Attachment
from pylibrus.db.store import MessageStore
from pylibrus.notify.render import build_webhook_payload
from pylibrus.scraper.urls import attachment_download_link
from pylibrus.settings import LibrusUser
from pylibrus.storage.s3 import S3AttachmentStorage, is_s3_webhook_source

logger = logging.getLogger(__name__)


class WebhookNotifier:
    """Posts items to a Slack-style webhook, linking attachments on Librus or (re-uploaded) on S3."""

    def __init__(self, librus_user: LibrusUser, store: MessageStore):
        self._librus_user = librus_user
        self._store = store

    def notify(self, msg_from_db, display_name: str | None = None):
        logger.info(f"Sending '{msg_from_db.subject}' to webhook from {msg_from_db.sender} ({msg_from_db.date})")
        self.send_via_webhook(msg_from_db, display_name)

    def _get_attachments(self, msg_from_db) -> list[Attachment]:
        return self._store.attachments_for(msg_from_db.url)


    @staticmethod
    def _librus_attachment_links(attachments: list[Attachment]) -> list[tuple[str, str]]:
        return [(a.name, attachment_download_link(a.link_id)) for a in attachments]


    def _s3_attachment_links(self, msg_from_db, attachments: list[Attachment]) -> list[tuple[str, str]]:
        links: list[tuple[str, str]] = []
        storage = S3AttachmentStorage(self._librus_user.notify)
        for attach in attachments:
            fallback_link = attachment_download_link(attach.link_id)
            if attach.data is None:
                logger.warning(f"Attachment '{attach.name}' has no data in DB, fallback to Librus link")
                links.append((attach.name, fallback_link))
                continue
            try:
                if not attach.s3_key:
                    s3_key, s3_etag = storage.upload_attachment(
                        librus_user_name=self._librus_user.name,
                        msg_path=msg_from_db.url,
                        attachment=attach,
                    )
                    attach.s3_key = s3_key
                    attach.s3_etag = s3_etag
                    attach.s3_upload_date = datetime.datetime.now()
                    logger.info(f"Uploaded attachment '{attach.name}' to S3 key '{attach.s3_key}'")
                else:
                    logger.info(f"Reusing uploaded S3 key for attachment '{attach.name}': {attach.s3_key}")
                links.append((attach.name, storage.generate_download_link(attach)))
            except Exception as ex:
                logger.warning(f"Failed to upload/presign attachment '{attach.name}' ({ex}), fallback to Librus link")
                links.append((attach.name, fallback_link))
        return links


    def _build_webhook_attachment_links(self, msg_from_db) -> list[tuple[str, str]]:
        attachments = self._get_attachments(msg_from_db)
        if not attachments:
            return []
        if not is_s3_webhook_source(self._librus_user.notify.webhook_attachments_source):
            return self._librus_attachment_links(attachments)
        try:
            return self._s3_attachment_links(msg_from_db, attachments)
        except Exception as ex:
            logger.warning(f"Failed to initialize S3 attachment storage ({ex}), fallback to Librus links")
            return self._librus_attachment_links(attachments)


    def send_via_webhook(self, msg_from_db, display_name: str | None = None):
        attachment_links = self._build_webhook_attachment_links(msg_from_db)
        name = display_name or self._librus_user.name

        message = build_webhook_payload(msg_from_db, name, attachment_links)

        response = requests.post(
            self._librus_user.notify.webhook,
            data=json.dumps(message),
            headers={"Content-Type": "application/json"},
        )

        if response.status_code != 200:
            logger.warning(f"Failed to send message. Status code: {response.status_code}")
