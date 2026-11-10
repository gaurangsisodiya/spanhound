"""live_eval.py schedule and flag-file editing: no network, no docker."""

import importlib.util
import json
import shutil
from collections import Counter
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "eval" / "live_eval.py"
spec = importlib.util.spec_from_file_location("live_eval", SCRIPT)
live_eval = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live_eval)

# A copy of ~/opentelemetry-demo/src/flagd/demo.flagd.json, so the test needs no demo checkout.
FLAGS_SOURCE = Path(__file__).resolve().parent / "data" / "demo.flagd.json"


@pytest.fixture
def flags_file(tmp_path):
    path = tmp_path / "demo.flagd.json"
    shutil.copy(FLAGS_SOURCE, path)
    return path


def flags(path):
    return json.loads(path.read_text())["flags"]


def without(data: dict, name: str) -> dict:
    return {k: v for k, v in data.items() if k != name}


def test_same_seed_gives_same_schedule():
    assert live_eval.build_schedule(42) == live_eval.build_schedule(42)
    assert live_eval.build_schedule(42) != live_eval.build_schedule(7)


def test_schedule_has_every_run_once():
    schedule = live_eval.build_schedule(42)

    assert len(schedule) == len(set(schedule)) == 24
    counts = Counter(name for name, _ in schedule)
    assert counts == {**{flag: 3 for flag in live_eval.FLAGS}, "control": 6}


def test_strongest_variants(flags_file):
    data = flags(flags_file)
    strongest = {name: live_eval.strongest_variant(data[name]) for name in live_eval.FLAGS}

    assert strongest == {
        "paymentFailure": "100%", "cartFailure": "100%", "productCatalogFailure": "on",
        "adFailure": "on", "paymentUnreachable": "on", "intlShippingSlowdown": "10sec",
    }


def test_set_and_reset_default_variant_touches_one_flag(flags_file):
    before = flags(flags_file)

    live_eval.set_variant(flags_file, "paymentFailure", "100%")
    during = flags(flags_file)
    assert during["paymentFailure"]["defaultVariant"] == "100%"
    assert without(during, "paymentFailure") == without(before, "paymentFailure")

    live_eval.set_variant(flags_file, "paymentFailure", "off")
    assert flags(flags_file) == before
    assert not list(flags_file.parent.glob("*.tmp"))  # no temp file left behind


def test_targeting_flag_is_switched_in_its_rule(flags_file):
    before = flags(flags_file)

    live_eval.set_variant(flags_file, "productCatalogFailure", "on")
    flag = flags(flags_file)["productCatalogFailure"]
    assert flag["targeting"]["if"][1] == "on"
    assert flag["defaultVariant"] == "off"  # flagd would ignore it: the rule always decides
    assert live_eval.flags_not_off(flags_file) == ["productCatalogFailure"]

    live_eval.set_variant(flags_file, "productCatalogFailure", "off")
    assert flags(flags_file) == before


def test_all_off_resets_every_eval_flag(flags_file):
    for name in live_eval.FLAGS:
        live_eval.set_variant(flags_file, name, live_eval.strongest_variant(flags(flags_file)[name]))
    assert len(live_eval.flags_not_off(flags_file)) == len(live_eval.FLAGS)

    live_eval.all_off(flags_file)

    assert live_eval.flags_not_off(flags_file) == []


def test_unknown_variant_is_rejected(flags_file):
    with pytest.raises(ValueError):
        live_eval.set_variant(flags_file, "adFailure", "100%")


def test_intl_order_payloads_match_the_shop_api():
    cart, checkout = live_eval.intl_order_payloads("user-1")

    # src/frontend/pages/api/cart.ts reads {userId, item}
    assert cart == {"userId": "user-1", "item": {"productId": live_eval.INTL_PRODUCT, "quantity": 1}}
    # src/frontend/pages/api/checkout.ts passes the body on as a PlaceOrderRequest
    assert set(checkout) == {"userId", "userCurrency", "address", "email", "creditCard"}
    assert checkout["userId"] == "user-1"
    assert checkout["address"]["country"] not in ("US", "USA", "United States")
    assert set(checkout["address"]) == {"streetAddress", "zipCode", "city", "state", "country"}


def test_intl_payloads_do_not_share_state():
    _, first = live_eval.intl_order_payloads("a")
    first["address"]["country"] = "changed"
    _, second = live_eval.intl_order_payloads("b")

    assert second["address"]["country"] == "Canada"
    assert second["userId"] == "b"


def test_intl_product_is_not_the_catalog_failure_product():
    assert live_eval.INTL_PRODUCT != "OLJCESPC7Z"


def test_order_offsets_spread_across_window():
    offsets = live_eval.order_offsets(180)

    assert len(offsets) == 3
    assert offsets == sorted(offsets)
    assert 0 < offsets[0] and offsets[-1] < 180 - 15  # last order has time to finish (10 s delay)
    gaps = [b - a for a, b in zip(offsets, offsets[1:])]
    assert max(gaps) - min(gaps) < 1e-9


@pytest.mark.parametrize(
    "flag, needle",
    [
        ("paymentFailure", "payment"),
        ("cartFailure", "cart"),
        ("productCatalogFailure", "product-catalog"),
        ("adFailure", "ad"),
    ],
)
def test_error_fault_queries(flag, needle):
    sql, params = live_eval.fault_query(flag, "2026-11-10 15:00:00", "2026-11-10 15:03:00")

    assert "StatusCode = 'Error'" in sql
    assert params == {"service": needle, "start": "2026-11-10 15:00:00", "end": "2026-11-10 15:03:00"}


def test_special_fault_queries_and_control():
    unreachable, _ = live_eval.fault_query("paymentUnreachable", "s", "e")
    slow, _ = live_eval.fault_query("intlShippingSlowdown", "s", "e")

    assert "SpanKind = 'Client'" in unreachable and "%PaymentService%" in unreachable
    assert "ServiceName = 'shipping'" in slow and "Duration > 5000000000" in slow
    assert live_eval.fault_query("control", "s", "e") is None
    assert set(live_eval.FAULT_CHECKS) == set(live_eval.FLAGS)


def live_row(incident, run, correct, observed, expected="payment", predicted="payment"):
    return {
        "incident": incident, "run": run, "start": "s", "end": "e", "expected": expected,
        "predicted": predicted, "correct": correct, "fault_observed": observed,
    }


def test_accuracy_over_observed_faults_and_unobserved_list():
    rows = [
        live_row("paymentFailure", 1, True, True),
        live_row("paymentFailure", 2, False, True, predicted="checkout"),
        live_row("intlShippingSlowdown", 1, False, False, expected="shipping", predicted="none"),
        live_row("control", 1, True, True, expected="none", predicted="none"),
    ]

    assert live_eval.observed_accuracy_row(rows) == ("Fault accuracy, fault observed", "1/2 (50%)")
    section = live_eval.unobserved_section(rows)
    assert "intlShippingSlowdown run 1" in section and "predicted none" in section
    assert "paymentFailure" not in section and "control" not in section
