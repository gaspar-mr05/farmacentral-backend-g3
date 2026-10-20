from xml.etree.ElementTree import ParseError

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring
from pydantic import TypeAdapter, ValidationError

from app.schemas.purchase_orders import PurchaseOrderId

ORDER_ID_ADAPTER = TypeAdapter(PurchaseOrderId)


class InvalidSFTPOrderError(Exception):
    pass


def extract_order_id(content: bytes, *, max_bytes: int = 65536) -> str:
    if len(content) > max_bytes:
        raise InvalidSFTPOrderError("XML exceeds the configured file size limit")
    try:
        root = fromstring(content, forbid_dtd=True)
    except (ParseError, DefusedXmlException, ValueError) as exc:
        raise InvalidSFTPOrderError("Invalid or unsafe order XML") from exc
    ids = root.findall("id")
    if root.tag != "order" or len(ids) != 1 or list(ids[0]):
        raise InvalidSFTPOrderError("Expected one order/id element")
    try:
        return ORDER_ID_ADAPTER.validate_python((ids[0].text or "").strip()).lower()
    except ValidationError as exc:
        raise InvalidSFTPOrderError("XML contains an invalid order ID") from exc
