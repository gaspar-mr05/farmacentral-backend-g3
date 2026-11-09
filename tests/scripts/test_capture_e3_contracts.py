import hashlib
import json
from datetime import UTC, datetime

import httpx
import pytest
from defusedxml.common import DefusedXmlException

from scripts import capture_e3_contracts as module

WSDL = b"""<wsdl:definitions
 xmlns:wsdl="http://schemas.xmlsoap.org/wsdl/"
 xmlns:s="http://www.w3.org/2001/XMLSchema"
 targetNamespace="http://billing.tallerdeintegracion.cl">
 <wsdl:types><s:schema><s:element name="BillingDetails"><s:complexType>
 <s:sequence><s:element name="id" type="s:string"/>
 <s:element name="price" type="s:float"/></s:sequence>
 </s:complexType></s:element></s:schema></wsdl:types>
 <wsdl:portType><wsdl:operation name="emitInvoice"/>
 <wsdl:operation name="getInvoices"/></wsdl:portType>
</wsdl:definitions>"""


def test_wsdl_summary_reports_actual_fields_and_precision_without_inventing_link():
    summary = module.contract_summary(WSDL, "wsdl")
    assert summary["operations"] == ["emitInvoice", "getInvoices"]
    assert summary["invoice_fields"] == ["id", "price"]
    assert summary["money_types"] == {"price": "s:float"}


def test_wsdl_rejects_external_entities():
    xml = b'<!DOCTYPE x [<!ENTITY secret SYSTEM "file:///etc/passwd">]><x>&secret;</x>'
    with pytest.raises(DefusedXmlException):
        module.contract_summary(xml, "wsdl")


@pytest.mark.parametrize("content", [b"[]", b"null", b'{"paths":[]}'])
def test_openapi_summary_rejects_non_contract_json(content):
    with pytest.raises(ValueError, match="Expected OpenAPI paths"):
        module.contract_summary(content, "openapi")


@pytest.mark.anyio
async def test_capture_uses_only_public_gets_and_hashes_successful_snapshots(
    tmp_path, monkeypatch
):
    class Clock:
        @classmethod
        def now(cls, _):
            return datetime(2026, 11, 9, 16, tzinfo=UTC)

    monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(
        module,
        "SOURCES",
        {
            "billing.wsdl": ("/soap/billing?wsdl", "wsdl"),
            "trades.html": ("/trades/docs", "html"),
            "messaging.openapi.json": ("/messaging/openapi.json", "openapi"),
        },
    )

    def handle(request):
        assert request.method == "GET"
        assert "Authorization" not in request.headers
        if request.url.path == "/soap/billing":
            return httpx.Response(200, content=WSDL)
        if request.url.path == "/trades/docs":
            return httpx.Response(400, text="not available")
        return httpx.Response(200, json={"info": {"version": "1"}, "paths": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        manifest = await module.capture(client, tmp_path)

    assert json.loads((tmp_path / "manifest.json").read_text()) == manifest
    assert manifest["sources"]["trades.html"]["status"] == "pending"
    wsdl = manifest["sources"]["billing.wsdl"]
    assert (tmp_path / wsdl["file"]).read_bytes() == WSDL
    assert wsdl["sha256"] == hashlib.sha256(WSDL).hexdigest()
    assert not (tmp_path / "trades.html").exists()


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["redirect", "timeout", "invalid-contract"])
async def test_capture_records_failures_without_persisting_error_bodies(
    tmp_path, monkeypatch, mode
):
    monkeypatch.setattr(
        module, "SOURCES", {"billing.wsdl": ("/soap/billing?wsdl", "wsdl")}
    )

    def handle(request):
        if mode == "timeout":
            raise httpx.ReadTimeout("private-error", request=request)
        if mode == "redirect":
            return httpx.Response(302, text="private-error")
        return httpx.Response(200, content=b"<html>private-error</html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        manifest = await module.capture(client, tmp_path)

    assert manifest["sources"]["billing.wsdl"]["status"] == "failed"
    assert "private-error" not in json.dumps(manifest)
    assert list(tmp_path.iterdir()) == [tmp_path / "manifest.json"]
