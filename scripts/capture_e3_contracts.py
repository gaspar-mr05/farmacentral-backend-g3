"""Archive public E3 documentation without authentication or business operations."""

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

BASE_URL = "https://dev.proyecto.2026-2.tallerdeintegracion.cl"
SOURCES = {
    "billing.wsdl": ("/soap/billing?wsdl", "wsdl"),
    "billing.html": ("/billing/docs", "html"),
    "trades.html": ("/trades/docs", "html"),
    "messaging.html": ("/messaging/docs", "html"),
    "farma-central.openapi.json": ("/farma-central/openapi.json", "openapi"),
    "purchase-orders.openapi.json": ("/ordenes-compra/openapi.json", "openapi"),
    "messaging.openapi.json": ("/messaging/openapi.json", "openapi"),
}
NS = {
    "wsdl": "http://schemas.xmlsoap.org/wsdl/",
    "xsd": "http://www.w3.org/2001/XMLSchema",
    "soap": "http://schemas.xmlsoap.org/wsdl/soap/",
}


def contract_summary(content: bytes, kind: str) -> dict:
    if kind == "wsdl":
        root = ElementTree.fromstring(content)
        if root.tag != f"{{{NS['wsdl']}}}definitions":
            raise ValueError("Expected WSDL definitions")
        fields = root.findall(
            ".//xsd:element[@name='BillingDetails']/xsd:complexType/"
            "xsd:sequence/xsd:element",
            NS,
        )
        return {
            "namespace": root.get("targetNamespace"),
            "operations": sorted(
                node.attrib["name"]
                for node in root.findall("wsdl:portType/wsdl:operation", NS)
            ),
            "invoice_fields": [node.get("name") for node in fields],
            "money_types": {
                node.attrib["name"]: node.get("type")
                for node in fields
                if node.get("name") in {"price", "interest", "totalPrice"}
            },
        }
    if kind == "openapi":
        schema = json.loads(content)
        if not isinstance(schema, dict) or not isinstance(schema.get("paths"), dict):
            raise ValueError("Expected OpenAPI paths")
        return {
            "version": schema.get("info", {}).get("version"),
            "paths": sorted(schema["paths"]),
        }
    if b"<html" not in content.lower():
        raise ValueError("Expected HTML documentation")
    return {}


async def capture(client: httpx.AsyncClient, directory: Path) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    observed_at = datetime.now(UTC)
    entries = {}
    # Sequential, bounded reads avoid a burst against the shared teaching service.
    for filename, (path, kind) in SOURCES.items():
        entry = {"url": BASE_URL + path}
        try:
            response = await client.get(BASE_URL + path)
            entry["http_status"] = response.status_code
            if response.status_code != 200:
                scheduled = (
                    filename == "trades.html"
                    and response.status_code in {400, 404, 503}
                    and observed_at.astimezone(ZoneInfo("America/Santiago")).date()
                    < date(2026, 11, 11)
                )
                entry["status"] = "pending" if scheduled else "failed"
                if scheduled:
                    entry["announced_available_on"] = "2026-11-11"
            else:
                content = response.content
                if len(content) > 2_000_000:
                    raise ValueError("Documentation exceeds the snapshot limit")
                entry.update(contract_summary(content, kind))
                snapshot_name = f"{observed_at:%Y%m%dT%H%M%S%fZ}-{filename}"
                (directory / snapshot_name).write_bytes(content)
                entry.update(
                    status="passed",
                    file=snapshot_name,
                    sha256=hashlib.sha256(content).hexdigest(),
                    bytes=len(content),
                )
        except (
            httpx.HTTPError,
            ValueError,
            ElementTree.ParseError,
            DefusedXmlException,
        ) as exc:
            entry.update(status="failed", error_type=type(exc).__name__)
        entries[filename] = entry
    manifest = {
        "captured_at": observed_at.isoformat(),
        "scope": "public_documentation_only",
        "llm_calls": 0,
        "sources": entries,
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


async def run(directory: Path) -> dict:
    async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
        return await capture(client, directory)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    manifest = asyncio.run(run(args.output_dir))
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return int(any(item["status"] == "failed" for item in manifest["sources"].values()))


if __name__ == "__main__":
    raise SystemExit(main())
