"""SOAP 1.1 document/literal envelope and exact, bounded XML decoding."""

from xml.etree.ElementTree import Element, SubElement, tostring

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
BILLING = "http://billing.tallerdeintegracion.cl"
WSDL = "http://schemas.xmlsoap.org/wsdl/"
BINDING = "http://schemas.xmlsoap.org/wsdl/soap/"
WSSE = (
    "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
)
PASSWORD_TEXT = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordText"
OPERATIONS = {"emitInvoice", "payInvoice", "getInvoices", "getBankStatement"}


def xml_root(content: bytes) -> Element:
    if len(content) > 4_000_000:
        raise ValueError("Billing XML exceeds the response limit")
    try:
        return ElementTree.fromstring(content)
    except (ElementTree.ParseError, DefusedXmlException):
        raise ValueError("Invalid billing XML") from None


def wsdl_actions(content: bytes) -> dict[str, str]:
    root = xml_root(content)
    if root.tag != f"{{{WSDL}}}definitions" or root.get("targetNamespace") != BILLING:
        raise ValueError("Unexpected billing WSDL namespace")
    actions = {}
    for binding in root.findall(f"{{{WSDL}}}binding"):
        if binding.find(f"{{{BINDING}}}binding") is None:
            continue
        for operation in binding.findall(f"{{{WSDL}}}operation"):
            action = operation.find(f"{{{BINDING}}}operation")
            if operation.get("name") in OPERATIONS and action is not None:
                value = action.get("soapAction")
                if value is None or any(char in value for char in '\r\n"'):
                    raise ValueError("Invalid billing SOAPAction")
                actions[operation.attrib["name"]] = value
    if set(actions) != OPERATIONS:
        raise ValueError("Billing WSDL is missing required SOAP 1.1 operations")
    return actions


def envelope(
    operation: str, values: dict[str, str], username: str, password: str
) -> bytes:
    root = Element(f"{{{SOAP}}}Envelope")
    security = SubElement(SubElement(root, f"{{{SOAP}}}Header"), f"{{{WSSE}}}Security")
    token = SubElement(security, f"{{{WSSE}}}UsernameToken")
    SubElement(token, f"{{{WSSE}}}Username").text = username
    SubElement(token, f"{{{WSSE}}}Password", Type=PASSWORD_TEXT).text = password
    body = SubElement(root, f"{{{SOAP}}}Body")
    request = SubElement(body, f"{{{BILLING}}}{operation}Request")
    for name, value in values.items():
        SubElement(request, f"{{{BILLING}}}{name}").text = value
    return tostring(root, encoding="utf-8", xml_declaration=True)


def body_element(content: bytes) -> Element:
    root = xml_root(content)
    if root.tag != f"{{{SOAP}}}Envelope":
        raise ValueError("Expected SOAP 1.1 envelope")
    bodies = root.findall(f"{{{SOAP}}}Body")
    if len(bodies) != 1 or len(bodies[0]) != 1:
        raise ValueError("Expected one billing SOAP response")
    return bodies[0][0]


def fault_code(root: Element) -> str | None:
    if root.tag != f"{{{SOAP}}}Fault":
        return None
    code = root.find("faultcode")
    if code is None:
        code = root.find(f"{{{SOAP}}}faultcode")
    value = (code.text or "").split(":")[-1] if code is not None else ""
    return (
        value
        if value in {"Client", "Server", "VersionMismatch", "MustUnderstand"}
        else "Unknown"
    )


def _is_element(root: Element, name: str) -> bool:
    return root.tag in {name, f"{{{BILLING}}}{name}"}


def records(root: Element, operation: str, name: str) -> list[dict[str, str]]:
    if _is_element(root, operation + "Response"):
        children = list(root)
    elif _is_element(root, name) and operation != "getInvoices":
        children = [root]
    else:
        raise ValueError("Unexpected billing response operation")
    if operation != "getInvoices" and len(children) != 1:
        raise ValueError("Expected one billing result")
    values = []
    for child in children:
        if not _is_element(child, name):
            raise ValueError("Unexpected billing result element")
        fields = {}
        for field in child:
            key = field.tag.rsplit("}", 1)[-1]
            if not _is_element(field, key) or key in fields or len(field):
                raise ValueError("Invalid or duplicate billing field")
            # Decimal is constructed later from this XML text, never from float.
            fields[key] = field.text or ""
        values.append(fields)
    return values
