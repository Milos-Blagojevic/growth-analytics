from pathlib import Path

import pytest

from prospect_data import CONFIG_TOML, SETTINGS
from prospects import config


def load(tmp_path, text):
    path = tmp_path / "prospects.toml"
    path.write_text(text)
    return config.load(path)


def test_load_fills_in_the_defaults(tmp_path):
    assert load(tmp_path, CONFIG_TOML) == SETTINGS


def test_optional_keys_have_the_documented_defaults(tmp_path):
    text = CONFIG_TOML
    for line in ("revenue_per_employee = [50000, 200000]\n", "basis_share_of_revenue = 0.5\n", "min_fit = 50\n"):
        text = text.replace(line, "")

    settings = load(tmp_path, text)

    assert settings["pricing"]["revenue_per_employee"] is None  # no estimate from headcount
    assert (settings["pricing"]["basis_share_of_revenue"], settings["routing"]["min_fit"]) == (1.0, 50)


def test_the_shipped_template_loads_once_its_blanks_are_filled_in(tmp_path):
    template = (Path(__file__).parent.parent / "prospects.example.toml").read_text()
    assert template.count('= ""') == 4  # product, ideal customer, what to look for, pricing basis

    settings = load(tmp_path, template.replace('= ""', '= "filled in"'))

    assert [tier["name"] for tier in settings["routing"]["tiers"]] == ["A", "B", "C"]
    assert settings["pricing"]["take_rate"] == 0.01


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('description = "Subscription billing for mobile apps."', 'description = " "', r"\[product\]\.description is required"),
        ('look_for = "whether it sells in-app subscriptions"', "", r"\[icp\]\.look_for is required"),
        ("take_rate = 0.01", "take_rate = 0", r"\[pricing\]\.take_rate must be a number above 0"),
        ("take_rate = 0.01", "take_rate = 1.5", r"\[pricing\]\.take_rate must be a number above 0"),
        ("take_rate = 0.01", 'take_rate = "1%"', r"\[pricing\]\.take_rate must be a number above 0"),
        ("take_rate = 0.01", "take_rate = true", r"\[pricing\]\.take_rate must be a number above 0"),  # not 100%
        ("take_rate = 0.01", "", r"\[pricing\]\.take_rate must be a number above 0"),
        ("basis_share_of_revenue = 0.5", "basis_share_of_revenue = 50", r"\[pricing\]\.basis_share_of_revenue must be"),
        # a misspelt optional key would silently fall back to its default and double every estimate here
        ("basis_share_of_revenue = 0.5", "basis_share_of_revenu = 0.5", r"\[pricing\]\.basis_share_of_revenu is not a key"),
        ("[routing]\n", "[routin]\n", r"\[routin\] is not a section"),
        ("[50000, 200000]", "[200000, 50000]", r"revenue_per_employee must be \[low, high\]"),
        ("[50000, 200000]", "[50000, inf]", r"revenue_per_employee must be \[low, high\]"),
        ("[50000, 200000]", "50000", r"revenue_per_employee must be \[low, high\]"),
        ("min_fit = 50", "min_fit = 150", r"\[routing\]\.min_fit must be a number from 0 to 100"),
        ('["Agency that builds apps for clients"]', '[""]', r"\[icp\]\.disqualifiers must be a list"),
        ("min_value = 20000", "min_value = 5000", "from the highest min_value to the lowest"),
        ("min_value = 20000", "min_value = 1000", "from the highest min_value to the lowest"),
        ("min_value = 20000", "min_value = nan", "each tier needs name, route and min_value"),
        ('route = "sales sequence"', "", "each tier needs name, route and min_value"),
        ('route = "sales sequence"', 'route = "sales sequence"\nowner = "me"', "each tier needs name, route and min_value"),
        ("min_value = 5000", "min_value = -1", "each tier needs name, route and min_value"),
        ('name = "A"', 'name = "out"', "'out' and 'unknown' are the tiers of dropped and unvalued prospects"),
        ('name = "B"', 'name = "unknown"', "'out' and 'unknown' are the tiers of dropped and unvalued prospects"),
    ],
)
def test_load_names_the_key_that_is_missing_invalid_or_unknown(tmp_path, old, new, message):
    assert old in CONFIG_TOML

    with pytest.raises(ValueError, match=message):
        load(tmp_path, CONFIG_TOML.replace(old, new))


def test_load_needs_at_least_one_tier(tmp_path):
    with pytest.raises(ValueError, match="needs at least one tier"):
        load(tmp_path, CONFIG_TOML.split("[[routing.tiers]]")[0])


def test_load_rejects_a_section_that_is_not_a_table(tmp_path):
    with pytest.raises(ValueError, match=r"\[product\] must be a table"):
        load(tmp_path, "product = 1\n[icp]" + CONFIG_TOML.split("[icp]", 1)[1])
