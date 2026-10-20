import pytest

from app.schemas.sftp_orders import InvalidSFTPOrderError, extract_order_id

ORDER_ID = "64abc123def456789012abcd"


def test_extracts_only_the_reference_not_untrusted_order_details():
    content = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f"<order><id> {ORDER_ID.upper()} </id><sku>FAKE</sku><qty>invalid</qty></order>"
    ).encode()
    assert extract_order_id(content) == ORDER_ID


@pytest.mark.parametrize(
    "content",
    [
        b"not XML",
        b"<order>",
        b"<order/>",
        b"<order><id/></order>",
        b"<order><id>../../other-service</id></order>",
        f"<other><id>{ORDER_ID}</id></other>".encode(),
        f"<order><id>{ORDER_ID}</id><id>{ORDER_ID}</id></order>".encode(),
        f"<order><id>{ORDER_ID}<nested/></id></order>".encode(),
        b'<!DOCTYPE order [<!ENTITY id "secret">]><order><id>&id;</id></order>',
        b'<!DOCTYPE order SYSTEM "file:///etc/passwd"><order><id>bad</id></order>',
    ],
)
def test_rejects_invalid_or_unsafe_order_xml(content):
    with pytest.raises(InvalidSFTPOrderError):
        extract_order_id(content)


def test_limits_the_size_of_the_xml():
    content = f"<order><id>{ORDER_ID}</id></order>".encode()
    with pytest.raises(InvalidSFTPOrderError, match="size limit"):
        extract_order_id(content, max_bytes=10)
