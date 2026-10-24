from types import SimpleNamespace

import pytest
from google.api_core.exceptions import AlreadyExists, DeadlineExceeded

from app.clients.pubsub import PubSubOrdersClient
from tests.support.farma_central import make_settings


class FakeSubscriber:
    def __init__(self):
        self.created = []
        self.pulls = []
        self.acknowledgements = []
        self.subscription_exists = False
        self.pull_times_out = False

    @staticmethod
    def topic_path(project, topic):
        return f"projects/{project}/topics/{topic}"

    @staticmethod
    def subscription_path(project, subscription):
        return f"projects/{project}/subscriptions/{subscription}"

    def create_subscription(self, **kwargs):
        self.created.append(kwargs)
        if self.subscription_exists:
            raise AlreadyExists("already exists")

    def pull(self, **kwargs):
        self.pulls.append(kwargs)
        if self.pull_times_out:
            raise DeadlineExceeded("no messages before deadline")
        message = SimpleNamespace(message_id="message-1", data=b"order-id")
        return SimpleNamespace(
            received_messages=[SimpleNamespace(ack_id="ack-1", message=message)]
        )

    def acknowledge(self, **kwargs):
        self.acknowledgements.append(kwargs)


@pytest.mark.anyio
async def test_creates_pulls_and_acknowledges_with_derived_resource_names():
    subscriber = FakeSubscriber()
    settings = make_settings(
        farma_central_base_url=(
            "https://dev.proyecto.2026-2.tallerdeintegracion.cl/farma-central/"
        )
    )
    client = PubSubOrdersClient(settings, subscriber=subscriber)

    await client.ensure_subscription()
    messages = await client.pull()
    await client.acknowledge([messages[0].ack_id])

    topic = "projects/taller-integracion-310700/topics/dev.proyecto.2026-2.grupo3"
    subscription = "projects/taller-integracion-310700/subscriptions/grupo3-dev"
    assert subscriber.created[0]["topic"] == topic
    assert subscriber.created[0]["name"] == subscription
    assert subscriber.pulls[0]["subscription"] == subscription
    assert subscriber.pulls[0]["max_messages"] == 100
    assert subscriber.acknowledgements[0]["ack_ids"] == ["ack-1"]
    assert messages[0].message_id == "message-1"
    assert messages[0].data == b"order-id"


@pytest.mark.anyio
async def test_existing_subscription_is_not_an_error():
    subscriber = FakeSubscriber()
    subscriber.subscription_exists = True
    settings = make_settings(
        pubsub_topic_id="topic", pubsub_subscription_id="subscription"
    )
    await PubSubOrdersClient(settings, subscriber=subscriber).ensure_subscription()
    assert len(subscriber.created) == 1


@pytest.mark.anyio
async def test_pull_deadline_without_messages_returns_an_empty_batch():
    subscriber = FakeSubscriber()
    subscriber.pull_times_out = True
    settings = make_settings(
        pubsub_topic_id="topic", pubsub_subscription_id="subscription"
    )
    messages = await PubSubOrdersClient(settings, subscriber=subscriber).pull()
    assert messages == []
