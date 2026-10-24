import asyncio
from dataclasses import dataclass
from functools import partial

from google.api_core.exceptions import (
    AlreadyExists,
    DeadlineExceeded,
    GoogleAPICallError,
)
from google.auth.exceptions import GoogleAuthError
from google.cloud import pubsub_v1

from app.core.config import Settings, get_settings


class PubSubOrdersError(Exception):
    pass


@dataclass(frozen=True)
class PubSubOrderMessage:
    message_id: str
    ack_id: str
    data: bytes


class PubSubOrdersClient:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        subscriber: pubsub_v1.SubscriberClient | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._owns_subscriber = subscriber is None
        try:
            if subscriber is not None:
                self._subscriber = subscriber
            elif self._settings.pubsub_credentials_file is not None:
                self._subscriber = pubsub_v1.SubscriberClient.from_service_account_file(
                    str(self._settings.pubsub_credentials_file.expanduser())
                )
            else:
                self._subscriber = pubsub_v1.SubscriberClient()
        except (GoogleAuthError, OSError, ValueError) as exc:
            raise PubSubOrdersError("Could not load Pub/Sub credentials") from exc

        project = self._settings.pubsub_project_id
        self.topic = self._subscriber.topic_path(
            project, self._settings.effective_pubsub_topic_id
        )
        self.subscription = self._subscriber.subscription_path(
            project, self._settings.effective_pubsub_subscription_id
        )

    async def __aenter__(self) -> "PubSubOrdersClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_subscriber:
            await asyncio.to_thread(self._subscriber.close)

    async def ensure_subscription(self) -> None:
        call = partial(
            self._subscriber.create_subscription,
            name=self.subscription,
            topic=self.topic,
            timeout=self._settings.pubsub_timeout_seconds,
        )
        try:
            await asyncio.to_thread(call)
        except AlreadyExists:
            return
        except GoogleAPICallError as exc:
            raise PubSubOrdersError(
                "Could not create the Pub/Sub subscription"
            ) from exc

    async def pull(self) -> list[PubSubOrderMessage]:
        call = partial(
            self._subscriber.pull,
            subscription=self.subscription,
            max_messages=self._settings.pubsub_batch_size,
            retry=None,
            timeout=self._settings.pubsub_timeout_seconds,
        )
        try:
            response = await asyncio.to_thread(call)
        except DeadlineExceeded:
            return []
        except GoogleAPICallError as exc:
            raise PubSubOrdersError("Could not pull Pub/Sub messages") from exc
        return [
            PubSubOrderMessage(
                message_id=received.message.message_id,
                ack_id=received.ack_id,
                data=received.message.data,
            )
            for received in response.received_messages
        ]

    async def acknowledge(self, ack_ids: list[str]) -> None:
        if not ack_ids:
            return
        call = partial(
            self._subscriber.acknowledge,
            subscription=self.subscription,
            ack_ids=ack_ids,
            retry=None,
            timeout=self._settings.pubsub_timeout_seconds,
        )
        try:
            await asyncio.to_thread(call)
        except GoogleAPICallError as exc:
            raise PubSubOrdersError("Could not acknowledge Pub/Sub messages") from exc
