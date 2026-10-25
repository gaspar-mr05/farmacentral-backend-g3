import pytest

from app.clients.farma_central import FarmaCentralInvalidResponseError
from app.services.messaging.catalog import kit_catalog
from tests.support.farma_central import catalog_product


def test_official_components_include_names_from_all_recipe_levels_once():
    payload = [
        catalog_product(
            "KIT-ANALGESIA",
            "Kit de analgesia",
            sellable=True,
            components=("AMP-KETOPRO-100", "FRA-SUERO-FISIO"),
        ),
        catalog_product(
            "AMP-KETOPRO-100",
            "Ketoprofeno inyectable",
            components=("API-KETOPRO-100", "FRA-SUERO-FISIO"),
        ),
        catalog_product("API-KETOPRO-100", "Ketoprofeno"),
        catalog_product("FRA-SUERO-FISIO", "Suero fisiológico"),
        catalog_product("KIT-REMOVED", "Kit retirado", sellable=False),
        catalog_product("KIT-UNKNOWN", "Kit fuera del catálogo local", sellable=True),
    ]

    result = kit_catalog(
        payload,
        {"KIT-ANALGESIA", "KIT-REMOVED", "KIT-OLD"},
        message_text="Necesito el kit con ketoprofeno inyectable y suero fisiológico",
    )

    assert [item.sku for item in result] == ["KIT-ANALGESIA"]
    assert result[0].components == (
        "Ketoprofeno",
        "Ketoprofeno inyectable",
        "Suero fisiológico",
    )


def test_recipe_cycle_does_not_repeat_the_kit_or_loop():
    payload = [
        catalog_product("KIT-A", "Kit", sellable=True, components=("COMPONENT",)),
        catalog_product("COMPONENT", "Componente", components=("KIT-A",)),
    ]
    assert kit_catalog(payload, {"KIT-A"}, message_text="kit con componente")[
        0
    ].components == ("Componente",)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        [{"sku": "KIT-A"}],
        [catalog_product("KIT-A", "Kit", sellable=True, components=("MISSING",))],
        [catalog_product("KIT-A", "Uno"), catalog_product("KIT-A", "Otro")],
    ],
)
def test_invalid_catalog_is_rejected_instead_of_sending_incomplete_context(payload):
    with pytest.raises(FarmaCentralInvalidResponseError):
        kit_catalog(payload, {"KIT-A"}, message_text="Necesito un kit")


def test_all_kits_remain_visible_and_shared_ingredients_do_not_hide_ambiguity():
    payload = [
        catalog_product(
            "KIT-A", "Kit adulto", sellable=True, components=("AMOX-500", "PACKAGING")
        ),
        catalog_product(
            "KIT-B", "Kit pediátrico", sellable=True, components=("AMOX-250",)
        ),
        catalog_product("KIT-C", "Otro kit", sellable=True),
        catalog_product("AMOX-500", "Amoxicilina 500 mg"),
        catalog_product("AMOX-250", "Amoxicilina 250 mg"),
        catalog_product("PACKAGING", "Lámina de plástico"),
    ]

    result = kit_catalog(
        payload, {"KIT-A", "KIT-B", "KIT-C"}, message_text="Necesito amoxicilina"
    )

    assert [item.sku for item in result] == ["KIT-A", "KIT-B", "KIT-C"]
    assert result[0].components == ("Amoxicilina 500 mg",)
    assert result[1].components == ("Amoxicilina 250 mg",)
    assert result[2].components == ()


def test_component_matching_ignores_accents_and_simple_plurals():
    payload = [
        catalog_product(
            "KIT-A", "Kit", sellable=True, components=("SYRINGE", "SALINE", "PAPER")
        ),
        catalog_product("SYRINGE", "Jeringa de 10 ml"),
        catalog_product("SALINE", "Solución fisiológica"),
        catalog_product("PAPER", "Papel estéril"),
    ]
    result = kit_catalog(
        payload, {"KIT-A"}, message_text="Necesito jeringas y solucion fisiologica"
    )
    assert result[0].components == ("Jeringa de 10 ml", "Solución fisiológica")
