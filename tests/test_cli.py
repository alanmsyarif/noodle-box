"""The CLI contract: every advertised name resolves, bad input exits 2.

The preset list is advertised in the README and printed by `--realtime`, so it
is an interface. An earlier version listed `default` in the help text but had no
such entry in the table, so `--realtime default` raised KeyError after building
the whole node graph. Deriving the list from the tables is what makes that
class of drift impossible; these tests hold the derivation in place.
"""
import pytest

PRESETS = ["default", "quality", "balanced", "fast", "metric", "metric_fast"]


def test_preset_names_are_derived_from_the_tables(noodle):
    assert set(noodle.preset_names()) == set(noodle.REALTIME_PRESETS) | set(
        noodle.UNIT_PRESETS)


@pytest.mark.parametrize("name", PRESETS)
def test_every_advertised_preset_resolves(noodle, name):
    overrides, max_count = noodle.lookup_preset(name)
    assert isinstance(overrides, dict)
    assert max_count >= 1


@pytest.mark.parametrize("name", PRESETS)
def test_every_preset_only_names_real_sockets(noodle, name):
    """A typo in a preset table would otherwise be a silent no-op."""
    overrides, _ = noodle.lookup_preset(name)
    known = {p[0] for p in noodle.PARAMS}
    assert set(overrides) <= known, f"{name} sets unknown sockets"


def test_default_preset_overrides_nothing(noodle):
    """'default' is the reference scene in PARAMS, not a second copy of it."""
    overrides, _ = noodle.lookup_preset("default")
    assert overrides == {}


def test_unknown_preset_is_a_cli_error(noodle):
    with pytest.raises(noodle.CliError) as excinfo:
        noodle.lookup_preset("bogus")
    assert "unknown preset" in str(excinfo.value)


def test_realtime_presets_keep_the_default_fall_speed_budget(noodle):
    """Each speed preset trades detail for speed without changing the fall.

    The budget is 2 * radius * VELOCITY_SAFETY * substeps * fps, held constant
    across the table - that is the whole point of the radius/substep pairing,
    and it is the arithmetic the README's table has to agree with.
    """
    budget = {}
    for name in ("quality", "balanced", "fast"):
        overrides, _ = noodle.lookup_preset(name)
        budget[name] = overrides["Noodle Radius"] * overrides["Substeps"]
    fastest = max(budget.values())
    for name, value in budget.items():
        assert value > 0.75 * fastest, (
            f"{name} spends {value:.1f} against a budget of {fastest:.1f}")


def test_parse_mode_args_rejects_malformed_bench(noodle):
    with pytest.raises(noodle.CliError):
        noodle.parse_mode_args(["--bench", "default", "120", "60", "extra"])
    with pytest.raises(noodle.CliError):
        noodle.parse_mode_args(["--bench", "default", "0"])


def test_parse_mode_args_defaults(noodle):
    assert noodle.parse_mode_args([]) == ("build",)
    assert noodle.parse_mode_args(["--check"]) == ("check",)
    assert noodle.parse_mode_args(["--bench"]) == ("bench", "default", 120, 60)
    assert noodle.parse_mode_args(["--realtime"]) == ("realtime", "balanced")


def test_every_socket_has_a_description(noodle):
    """A socket with no tooltip is a socket nobody can use.

    Nine of the twenty-four shipped without one, including Gravity and Friction,
    which are the two most likely to be changed.
    """
    missing = [p[0] for p in noodle.PARAMS if p[0] not in noodle.DESCRIPTIONS]
    assert missing == [], f"sockets with no description: {missing}"


def test_descriptions_only_name_real_sockets(noodle):
    known = {p[0] for p in noodle.PARAMS}
    assert set(noodle.DESCRIPTIONS) <= known
