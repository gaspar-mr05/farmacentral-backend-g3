"""Measure deterministic interpretation/resolution, not live LLM accuracy."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.clients.llm_gateway import LLMGatewayResult
from app.models import (
    BusinessPeriod,
    MessagingDecision,
    Order,
    Product,
    ProductCategory,
)
from app.schemas.llm_gateway import LLMOrderProposal
from app.services.messaging import (
    InquiryResolutionService,
    MessageInterpretationService,
)
from tests.services.test_messaging_inquiry import add_message
from tests.support.farma_central import catalog_product, make_settings
from tests.support.orders import make_purchase_order

FIXTURES = Path(__file__).parents[1] / "fixtures" / "messaging"
CASES = []
for split in ("development", "evaluation"):
    dataset = json.loads((FIXTURES / f"{split}.json").read_text())
    CASES.extend(
        (split, {**case, "history": dataset["history"]}) for case in dataset["cases"]
    )


@pytest.mark.anyio
@pytest.mark.parametrize("split,case", CASES, ids=[case["id"] for _, case in CASES])
async def test_corpus(db_session, request, split, case):
    for code, start, end in (
        (
            "current",
            datetime(2026, 11, 10, tzinfo=UTC),
            datetime(2026, 11, 20, tzinfo=UTC),
        ),
        (
            "previous",
            datetime(2026, 11, 1, tzinfo=UTC),
            datetime(2026, 11, 9, tzinfo=UTC),
        ),
    ):
        db_session.add(
            BusinessPeriod(
                code=code, group_id=3, environment="dev", starts_at=start, ends_at=end
            )
        )
    skus = ("KIT-CURACION", "KIT-DIABETES")
    db_session.add_all(
        [
            Product(sku=sku, name=sku, category=ProductCategory.KIT, batch_size=1)
            for sku in skus
        ]
    )
    db_session.flush()
    config = make_settings(
        app_env="dev", farma_central_base_url="https://dev.example.test/"
    )
    for index, prior in enumerate(case["history"]):
        add_message(
            db_session,
            message_id=f"history-{index}",
            phone=prior["phone"],
            text="Pedido histórico sintético",
            occurred_at=datetime.fromisoformat(prior["at"]),
            decision=MessagingDecision.ORDER,
            order_id=prior["id"],
            sku=prior["sku"],
            quantity=prior["quantity"],
            settings=config.model_copy(
                update={"farma_central_group": prior.get("group", 3)}
            ),
        )
    message = case["message"]
    received = datetime.fromisoformat(message["receivedAt"])
    incoming = add_message(
        db_session,
        message_id=message["messageId"],
        phone=message["sender"]["phone"],
        text=message["text"],
        occurred_at=received,
        decision=None,
        settings=config,
    )
    gateway, catalog, orders = AsyncMock(), AsyncMock(), AsyncMock()
    proposal = LLMOrderProposal.model_validate_json(json.dumps(case["mock_proposal"]))
    gateway.propose_order.return_value = LLMGatewayResult(proposal, {"mock": True})
    catalog.get_available_products.return_value = [
        catalog_product(sku, sku, sellable=True) for sku in skus
    ]
    await MessageInterpretationService(
        gateway, db_session, catalog_client=catalog
    ).interpret(incoming, now=received + timedelta(seconds=1))
    if incoming.decision == MessagingDecision.INQUIRY:
        orders.get_order.side_effect = lambda identity: make_purchase_order(
            id=identity, proveedor="3"
        )
        await InquiryResolutionService(orders, db_session, group_id=3).resolve(
            incoming, now=received
        )
    actual = {
        "intent": incoming.decision.value,
        "selected_order": incoming.consulted_external_order_id,
        "sku": incoming.interpreted_sku,
        "quantity": incoming.interpreted_quantity,
        "delivery_date": incoming.interpreted_delivery_date.isoformat().replace(
            "+00:00", "Z"
        )
        if incoming.interpreted_delivery_date
        else None,
    }
    checks = {
        field: actual[field] == expected for field, expected in case["expected"].items()
    }
    if not hasattr(request.config, "e3_corpus_results"):
        request.config.e3_corpus_results = []
    request.config.e3_corpus_results.append(
        {"split": split, "category": case["category"], "checks": checks}
    )
    assert all(checks.values()), {
        "case": case["id"],
        "expected": case["expected"],
        "actual": actual,
    }
    gateway.propose_order.assert_awaited_once()
    assert db_session.scalar(select(func.count()).select_from(Order)) == 0
    if case["expected"].get("selected_order") is None:
        orders.get_order.assert_not_awaited()


def test_development_and_evaluation_are_distinct_and_scored_separately():
    dev = [case for split, case in CASES if split == "development"]
    evaluation = [case for split, case in CASES if split == "evaluation"]
    assert {c["id"] for c in dev}.isdisjoint(c["id"] for c in evaluation)
    assert {c["message"]["text"] for c in dev}.isdisjoint(
        c["message"]["text"] for c in evaluation
    )
    assert {c["category"] for c in dev} == {c["category"] for c in evaluation}
