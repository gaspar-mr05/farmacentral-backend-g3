from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring

from app.clients.billing import soap

WSDL = (
    Path(__file__).resolve().parents[2]
    / "docs/e3/contracts/2026-11-09/20261109T231057798063Z-billing.wsdl"
).read_bytes()
ORDER_ID = "111111111111111111111111"
INVOICE_ID = "222222222222222222222222"


def invoice_values(**overrides):
    return {
        "id": INVOICE_ID,
        "client": "3",
        "supplier": "1000",
        "channel": "fabric",
        "description": "Official invoice",
        "status": "pending",
        "price": "100.123456789",
        "interest": "0.000000001",
        "totalPrice": "100.123456790",
        "createdAt": "2026-11-09T14:00:00.000Z",
        "updatedAt": "2026-11-09T14:00:00.000Z",
    } | overrides


def response(operation, values, *, qualified=True):
    root = Element(f"{{{soap.SOAP}}}Envelope")
    body = SubElement(root, f"{{{soap.SOAP}}}Body")
    prefix = f"{{{soap.BILLING}}}" if qualified else ""
    wrapper = SubElement(body, prefix + operation + "Response")
    name = "BankStatement" if operation == "getBankStatement" else "BillingDetails"
    for item in values:
        record = SubElement(wrapper, prefix + name)
        for key, value in item.items():
            SubElement(record, prefix + key).text = value
    return tostring(root, encoding="utf-8")


def fault(code="Server", message="private-server-message"):
    root = Element(f"{{{soap.SOAP}}}Envelope")
    node = SubElement(SubElement(root, f"{{{soap.SOAP}}}Body"), f"{{{soap.SOAP}}}Fault")
    SubElement(node, "faultcode").text = "soap:" + code
    SubElement(node, "faultstring").text = message
    return tostring(root, encoding="utf-8")
