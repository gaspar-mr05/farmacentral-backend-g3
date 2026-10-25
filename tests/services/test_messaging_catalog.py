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

    result = kit_catalog(payload, {"KIT-ANALGESIA", "KIT-REMOVED", "KIT-OLD"})

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
    assert kit_catalog(payload, {"KIT-A"})[0].components == ("Componente",)


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
        kit_catalog(payload, {"KIT-A"})
