"""Validation tests: a broken SLO file must fail with a message someone can act on."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from sloctl.config import load_config_dir, load_config_file, load_configs, parse_objective, parse_slo
from sloctl.errors import ConfigError

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "examples" / "slo"

VALID = """
service: checkout
description: Checkout API availability
sli:
  name: availability
  unit: requests
  good_query: sum(rate(http_requests_total{job="checkout",status!~"5.."}[5m]))
  total_query: sum(rate(http_requests_total{job="checkout"}[5m]))
objective: 0.999
window: 30d
labels:
  team: platform
"""


def write(tmp_path: Path, text: str, name: str = "slo.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def expect_error(text: str, match: str) -> ConfigError:
    with pytest.raises(ConfigError, match=match) as error:
        parse_slo(yaml.safe_load(text), "slo.yaml")
    return error.value


def test_examples_load_with_the_documented_objectives():
    slos = {slo.service: slo for slo in load_config_dir(CONFIG_DIR)}
    assert sorted(slos) == ["checkout", "payments", "search"]
    assert slos["checkout"].objective == Decimal("0.999")
    assert slos["payments"].objective == Decimal("0.9995")
    assert slos["search"].objective == Decimal("0.995")
    for slo in slos.values():
        assert slo.window == "30d"
        assert slo.window_seconds == 2592000
        assert slo.sli.name == "availability"
        assert slo.labels["team"] in {"platform", "payments", "search"}
    assert slos["checkout"].budget_seconds == Decimal(2592)
    assert slos["payments"].budget_seconds == Decimal(1296)
    assert slos["search"].budget_seconds == Decimal(12960)


def test_valid_document_round_trips_through_a_file(tmp_path):
    path = write(tmp_path, VALID)
    slo = load_config_file(path)
    assert slo.service == "checkout"
    assert slo.labels == {"team": "platform"}
    assert slo.source == str(path)
    assert load_configs(path)[0].service == "checkout"


def test_percentage_string_is_accepted():
    assert parse_objective("99.95%", "slo.yaml") == Decimal("0.9995")
    assert parse_objective("99.999%", "slo.yaml") == Decimal("0.99999")


def test_float_objective_is_parsed_without_binary_noise():
    assert parse_objective(0.999, "slo.yaml") == Decimal("0.999")


def test_invalid_yaml_syntax_names_the_file(tmp_path):
    path = write(tmp_path, "service: checkout\nobjective: [0.999\n")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config_file(path)


def test_empty_file_is_rejected(tmp_path):
    path = write(tmp_path, "\n")
    with pytest.raises(ConfigError, match="file is empty"):
        load_config_file(path)


def test_missing_file_is_rejected_with_its_path(tmp_path):
    missing = tmp_path / "nope.yaml"
    with pytest.raises(ConfigError) as error:
        load_config_file(missing)
    assert str(missing) in str(error.value)


@pytest.mark.parametrize("field", ["service", "sli", "objective", "window"])
def test_every_required_field_is_enforced(field):
    document = yaml.safe_load(VALID)
    document.pop(field)
    error = expect_error(yaml.safe_dump(document), "missing required key")
    assert field in str(error)


def test_unknown_top_level_key_is_rejected():
    error = expect_error(VALID + "budget: 42\n", "unknown key")
    assert "budget" in str(error)


def test_unknown_sli_key_is_rejected():
    error = expect_error(VALID.replace("  unit: requests", "  unit: requests\n  window: 30d"), "sli has unknown key")
    assert "window" in str(error)


def test_service_name_must_be_prometheus_safe():
    error = expect_error(VALID.replace("service: checkout", 'service: "Checkout API"'), "invalid")
    assert "service" in str(error)


@pytest.mark.parametrize("objective", ["1.0", "0", "1.5", "0%", "100%"])
def test_objective_must_leave_room_for_a_budget(objective):
    with pytest.raises(ConfigError, match="objective"):
        parse_objective(objective, "slo.yaml")


@pytest.mark.parametrize("objective", ["ninety nine", "abc", None, True, [0.99]])
def test_objective_must_be_a_number(objective):
    with pytest.raises(ConfigError, match="objective"):
        parse_objective(objective, "slo.yaml")


@pytest.mark.parametrize("window", ["30", "thirty days", "30x", "0d"])
def test_window_must_be_a_duration(window):
    error = expect_error(VALID.replace("window: 30d", "window: %s" % window), "window")
    assert "30d" in str(error)


def test_queries_must_carry_a_range_selector():
    broken = VALID.replace("[5m]))", "))")
    error = expect_error(broken, "range selector")
    assert "good_query" in str(error) or "total_query" in str(error)


def test_queries_must_be_present():
    without_good = VALID.replace('  good_query: sum(rate(http_requests_total{job="checkout",status!~"5.."}[5m]))\n', "")
    error = expect_error(without_good, "missing required key")
    assert "good_query" in str(error)


def test_labels_must_be_strings():
    error = expect_error(VALID + "  tier: 1\n", "label")
    assert "tier" in str(error)


def test_service_label_is_reserved():
    error = expect_error(VALID + "  service: checkout\n", "reserved")
    assert "service" in str(error)


def test_labels_must_be_a_mapping():
    error = expect_error(
        VALID.replace("labels:\n  team: platform\n", "labels:\n  - team\n"), "labels must be a mapping"
    )
    assert "labels" in str(error)


def test_top_level_must_be_a_mapping():
    error = expect_error("- checkout\n- payments\n", "top level must be a mapping")
    assert "mapping" in str(error)


def test_duplicate_service_across_files_is_rejected(tmp_path):
    write(tmp_path, VALID, "checkout.yaml")
    write(tmp_path, VALID, "checkout-copy.yaml")
    with pytest.raises(ConfigError, match="defined by both"):
        load_config_dir(tmp_path)


def test_directory_without_yaml_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match=r"no \*\.yaml files"):
        load_config_dir(tmp_path)


def test_missing_directory_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="does not exist"):
        load_config_dir(tmp_path / "nope")


def test_directory_of_valid_files_loads_sorted(tmp_path):
    write(tmp_path, VALID, "b.yaml")
    write(tmp_path, VALID.replace("service: checkout", "service: alerts"), "a.yaml")
    assert [slo.service for slo in load_config_dir(tmp_path)] == ["alerts", "checkout"]
