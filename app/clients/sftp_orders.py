import asyncio
import logging
import posixpath
from dataclasses import dataclass

import asyncssh

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)


class SFTPOrdersError(Exception):
    pass


@dataclass(frozen=True)
class SFTPOrderFile:
    path: str
    content: bytes


class SFTPOrdersClient:
    """Read-only inbox access; every poll closes both the SFTP and SSH sessions."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    async def fetch_files(self, *, skip_paths: set[str]) -> list[SFTPOrderFile]:
        settings = self._settings
        options = {}
        if settings.sftp_known_hosts_file is not None:
            options["known_hosts"] = str(settings.sftp_known_hosts_file.expanduser())
        try:
            async with asyncssh.connect(
                settings.sftp_host,
                settings.sftp_port,
                username=settings.effective_sftp_username,
                password=settings.effective_sftp_password.get_secret_value(),
                client_keys=[],
                agent_path=None,
                config=None,
                connect_timeout=settings.sftp_timeout_seconds,
                login_timeout=settings.sftp_timeout_seconds,
                **options,
            ) as connection:
                async with asyncio.timeout(settings.sftp_timeout_seconds):
                    sftp = await connection.start_sftp_client()
                try:
                    return await self._read_inbox(sftp, skip_paths)
                finally:
                    # A stalled server must not retain a session during shutdown.
                    sftp.exit()
                    connection.close()
                    await sftp.wait_closed()
        except asyncssh.HostKeyNotVerifiable as exc:
            raise SFTPOrdersError(
                "The sFTP host key is not trusted; configure SFTP_KNOWN_HOSTS_FILE"
            ) from exc
        except asyncssh.PermissionDenied as exc:
            raise SFTPOrdersError("The sFTP credentials were rejected") from exc
        except (OSError, asyncssh.Error, TimeoutError) as exc:
            raise SFTPOrdersError("Could not read the sFTP inbox") from exc

    async def _read_inbox(
        self, sftp: asyncssh.SFTPClient, skip_paths: set[str]
    ) -> list[SFTPOrderFile]:
        settings = self._settings
        async with asyncio.timeout(settings.sftp_timeout_seconds):
            directory = await sftp.realpath(settings.sftp_directory)
            entries = await sftp.readdir(directory)
        files = []
        for entry in sorted(entries, key=lambda item: item.filename):
            name = entry.filename
            if (
                not name.lower().endswith(".xml")
                or posixpath.basename(name) != name
                or entry.attrs.type != asyncssh.FILEXFER_TYPE_REGULAR
            ):
                continue
            path = posixpath.join(directory, name)
            if path in skip_paths:
                continue
            try:
                async with asyncio.timeout(settings.sftp_timeout_seconds):
                    remote_file = await sftp.open(path, "rb")
                    content = await remote_file.read(settings.sftp_max_file_bytes + 1)
                    await remote_file.close()
            except TimeoutError:
                raise
            except (OSError, asyncssh.SFTPError):
                # Do not record an unreadable file as received: try it next poll.
                logger.warning("Could not read sFTP file: path=%s", path)
                continue
            files.append(SFTPOrderFile(path=path, content=content))
            if len(files) >= settings.sftp_batch_size:
                break
        return files
