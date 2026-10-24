import asyncio
import os
from types import SimpleNamespace

import asyncssh
import httpx
import pytest
from sqlalchemy import select

from app.clients.purchase_orders import PurchaseOrdersClient
from app.clients.sftp import SFTPOrdersClient, SFTPOrdersError
from app.models import Order
from app.services.orders import SFTPOrderIngestionService
from tests.support.farma_central import make_settings
from tests.support.orders import EXTERNAL_ORDER_ID, make_purchase_order


@pytest.fixture
async def sftp_server(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    closed = asyncio.Event()
    stalled = asyncio.Event()
    connections = []

    class Server(asyncssh.SSHServer):
        def connection_made(self, connection):
            connections.append(connection)

        def connection_lost(self, exc):
            closed.set()

        def password_auth_supported(self):
            return True

        def validate_password(self, username, password):
            return username == "grupo3_desarrollo" and password == "test-password"

    class FileServer(asyncssh.SFTPServer):
        def open(self, path, pflags, attrs):
            if path == b"/unreadable.xml":
                raise asyncssh.SFTPPermissionDenied("File is temporarily unavailable")
            return super().open(path, pflags, attrs)

        async def read(self, file_obj, offset, size):
            if os.fsdecode(file_obj.name).endswith("/slow.xml"):
                await stalled.wait()
            return super().read(file_obj, offset, size)

        async def realpath(self, path):
            if path == b"slow":
                await stalled.wait()
            return super().realpath(path)

    host_key = asyncssh.generate_private_key("ssh-ed25519")
    async with asyncssh.listen(
        "127.0.0.1",
        0,
        server_factory=Server,
        server_host_keys=[host_key],
        sftp_factory=lambda channel: FileServer(channel, chroot=str(inbox)),
    ) as listener:
        known_hosts = tmp_path / "known_hosts"
        known_hosts.write_text(
            f"[127.0.0.1]:{listener.get_port()} "
            + host_key.export_public_key().decode()
        )
        settings = make_settings(
            sftp_host="127.0.0.1",
            sftp_port=listener.get_port(),
            sftp_username="grupo3_desarrollo",
            sftp_password="test-password",
            sftp_known_hosts_file=known_hosts,
        )
        try:
            yield SimpleNamespace(
                inbox=inbox,
                settings=settings,
                closed=closed,
                connections=connections,
                known_hosts=known_hosts,
            )
        finally:
            stalled.set()
            for connection in connections:
                connection.close()
                await connection.wait_closed()


@pytest.mark.anyio
async def test_reads_only_new_regular_xml_files_and_closes_sessions(sftp_server):
    server = sftp_server
    (server.inbox / "new.xml").write_bytes(b"<order/>")
    (server.inbox / "known.xml").write_bytes(b"already received")
    (server.inbox / "unreadable.xml").write_bytes(b"unavailable")
    (server.inbox / "ignore.txt").write_bytes(b"not XML")
    (server.inbox / "directory.xml").mkdir()
    (server.inbox / "symlink.xml").symlink_to(server.inbox / "new.xml")

    files = await SFTPOrdersClient(server.settings).fetch_files(
        skip_paths={"/known.xml"}
    )

    assert [(file.path, file.content) for file in files] == [("/new.xml", b"<order/>")]
    assert (server.inbox / "new.xml").read_bytes() == b"<order/>"
    await asyncio.wait_for(server.closed.wait(), timeout=2)
    assert all(connection.is_closed() for connection in server.connections)


@pytest.mark.anyio
async def test_closes_sessions_when_listing_fails(sftp_server):
    settings = sftp_server.settings.model_copy(update={"sftp_directory": "missing"})
    with pytest.raises(SFTPOrdersError, match="Could not read"):
        await SFTPOrdersClient(settings).fetch_files(skip_paths=set())
    await asyncio.wait_for(sftp_server.closed.wait(), timeout=2)


@pytest.mark.anyio
async def test_bounds_remote_file_reads(sftp_server):
    (sftp_server.inbox / "large.xml").write_bytes(b"x" * 1000)
    settings = sftp_server.settings.model_copy(update={"sftp_max_file_bytes": 20})
    files = await SFTPOrdersClient(settings).fetch_files(skip_paths=set())
    assert len(files[0].content) == 21


@pytest.mark.anyio
async def test_limits_the_number_of_new_files_per_poll(sftp_server):
    for index in range(3):
        (sftp_server.inbox / f"{index}.xml").write_bytes(b"<order/>")
    settings = sftp_server.settings.model_copy(update={"sftp_batch_size": 2})
    client = SFTPOrdersClient(settings)
    first = await client.fetch_files(skip_paths=set())
    second = await client.fetch_files(skip_paths={file.path for file in first})
    assert len(first) == 2
    assert len(second) == 1


@pytest.mark.anyio
async def test_rejected_credentials_do_not_expose_the_password(sftp_server, caplog):
    settings = make_settings(
        **{
            **sftp_server.settings.model_dump(),
            "sftp_password": "invalid-secret",
        }
    )
    with pytest.raises(SFTPOrdersError, match="credentials were rejected") as error:
        await SFTPOrdersClient(settings).fetch_files(skip_paths=set())
    assert "invalid-secret" not in str(error.value) + caplog.text


@pytest.mark.anyio
async def test_rejects_a_different_host_key(sftp_server):
    key = asyncssh.generate_private_key("ssh-ed25519")
    sftp_server.known_hosts.write_text(
        f"[127.0.0.1]:{sftp_server.settings.sftp_port} "
        + key.export_public_key().decode()
    )
    with pytest.raises(SFTPOrdersError, match="host key is not trusted"):
        await SFTPOrdersClient(sftp_server.settings).fetch_files(skip_paths=set())


@pytest.mark.anyio
async def test_directory_timeout_closes_the_connection(sftp_server):
    settings = sftp_server.settings.model_copy(
        update={"sftp_directory": "slow", "sftp_timeout_seconds": 0.1}
    )
    with pytest.raises(SFTPOrdersError, match="Could not read"):
        await asyncio.wait_for(
            SFTPOrdersClient(settings).fetch_files(skip_paths=set()), timeout=3
        )
    await asyncio.wait_for(sftp_server.closed.wait(), timeout=2)


@pytest.mark.anyio
async def test_stalled_read_does_not_hold_the_session_open(sftp_server):
    (sftp_server.inbox / "slow.xml").write_bytes(b"stalled read")
    settings = sftp_server.settings.model_copy(update={"sftp_timeout_seconds": 0.1})
    with pytest.raises(SFTPOrdersError, match="Could not read"):
        await asyncio.wait_for(
            SFTPOrdersClient(settings).fetch_files(skip_paths=set()), timeout=3
        )
    await asyncio.wait_for(sftp_server.closed.wait(), timeout=2)


@pytest.mark.anyio
async def test_local_sftp_to_official_http_contract_creates_one_order(
    sftp_server, db_session
):
    (sftp_server.inbox / "order.xml").write_text(
        f"<order><id>{EXTERNAL_ORDER_ID}</id><sku>FAKE</sku><qty>999</qty></order>"
    )
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/autenticar"):
            return httpx.Response(200, json={"token": "test-token"})
        if request.url.path.endswith(f"/ordenes/{EXTERNAL_ORDER_ID}/estado"):
            assert request.method == "POST"
            assert request.headers["Authorization"] == "Bearer test-token"
            assert request.content == b'{"estado":"rechazada"}'
            return httpx.Response(204)
        assert request.url.path.endswith(f"/ordenes/{EXTERNAL_ORDER_ID}")
        assert request.headers["Authorization"] == "Bearer test-token"
        return httpx.Response(
            200, json=make_purchase_order().model_dump(mode="json", by_alias=True)
        )

    async with (
        httpx.AsyncClient(
            base_url="https://orders.test/", transport=httpx.MockTransport(handler)
        ) as http_client,
        PurchaseOrdersClient(
            settings=sftp_server.settings, http_client=http_client
        ) as orders,
    ):
        service = SFTPOrderIngestionService(
            SFTPOrdersClient(sftp_server.settings),
            orders,
            db_session,
            settings=sftp_server.settings,
        )
        assert (await service.poll()).processed == 1
        assert (await service.poll()).processed == 0

    assert len(requests) == 3
    order = db_session.scalars(select(Order)).one()
    assert order.external_order_id == EXTERNAL_ORDER_ID
    assert order.items[0].sku == "KIT-ANALGESIA"
    assert order.items[0].quantity == 2
    assert order.status.value == "rejected"
    assert order.decision_at is not None
    assert len(order.inbound_events) == 1
