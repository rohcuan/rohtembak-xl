# Decoy package management
import json
import os

from app.client.engsel import get_family, get_package
from app.type_dict import PaymentItem

DECOY_DATA_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "decoy_data"
)


def decoy_json_path(payment_type: str) -> str:
    return os.path.join(DECOY_DATA_DIR, f"decoy-default-{payment_type}.json")


def load_decoy_config(payment_type: str) -> dict | None:
    path = decoy_json_path(payment_type)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def resolve_decoy_package(api_key: str, tokens: dict, config: dict) -> dict | None:
    family_data = get_family(
        api_key,
        tokens,
        config["family_code"],
        config.get("is_enterprise"),
        config.get("migration_type"),
    )
    if not family_data:
        return None

    option_code = None
    for variant in family_data["package_variants"]:
        if variant["package_variant_code"] != config["variant_code"]:
            continue
        for option in variant["package_options"]:
            if option["order"] == config.get("order"):
                option_code = option["package_option_code"]
                break
        break

    if option_code is None:
        return None

    return get_package(api_key, tokens, option_code, config["family_code"], config["variant_code"])


def build_decoy_item(api_key: str, tokens: dict, payment_type: str = "balance") -> PaymentItem | None:
    config = load_decoy_config(payment_type)
    if not config:
        return None

    package_detail = resolve_decoy_package(api_key, tokens, config)
    if not package_detail:
        return None

    option = package_detail.get("package_option", {})
    return PaymentItem(
        item_code=option.get("package_option_code", ""),
        product_type="",
        item_price=option.get("price", config.get("price", 0)),
        item_name=option.get("name", ""),
        tax=0,
        token_confirmation=package_detail.get("token_confirmation", ""),
    )
