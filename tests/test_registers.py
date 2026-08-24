"""Register map contract and Modbus codec tests."""
import pytest

from sim.regmap import (
    RangeError,
    RegisterMap,
    RegisterMapError,
    decode,
    encode,
    load_all,
    load_map,
)

MAPS = load_all()


@pytest.mark.parametrize("name", sorted(MAPS))
def test_modicon_offset_relationship(name):
    """Modicon 30001 is input-register offset 0; 40001 is holding offset 0.

    Off-by-one here is the single most common Modbus integration defect, so it
    is pinned rather than merely avoided.
    """
    m = MAPS[name]
    for p in m.points:
        assert p.offset == p.modicon - 30001, f"{name}.{p.key}"
    for p in m.setpoints:
        assert p.offset == p.modicon - 40001, f"{name}.{p.key}"


def test_return_air_temp_lands_at_offset_35():
    assert load_map("crv").by_key("return_air_temp").offset == 35


@pytest.mark.parametrize("name", sorted(MAPS))
def test_no_overlapping_registers(name):
    claimed = {}
    m = MAPS[name]
    for space in ("input", "holding"):
        for p in m.space(space):
            for reg in range(p.offset, p.offset + p.width):
                key = (space, reg)
                assert key not in claimed, f"{name}: {reg} claimed twice"
                claimed[key] = p.key


@pytest.mark.parametrize("name", sorted(MAPS))
def test_every_point_declares_provenance(name):
    """Published vs inferred must be explicit -- the project's honesty claim
    depends on being able to say which addresses came from public material."""
    for p in MAPS[name].all_points:
        assert p.source in {"published", "inferred"}, f"{name}.{p.key}"


def test_overlap_is_actually_rejected():
    """The validator must fail loudly, not just be satisfied by good input."""
    from sim.regmap import Point
    dup = [
        Point(key="a", modicon=30001, offset=0, type="uint16", space="input",
              poll_class="status", source="inferred", range=(0.0, 10.0)),
        Point(key="b", modicon=30001, offset=0, type="uint16", space="input",
              poll_class="status", source="inferred", range=(0.0, 10.0)),
    ]
    with pytest.raises(RegisterMapError, match="claimed by both"):
        RegisterMap(device_type="X", description="", points=dup)


def test_bad_offset_is_rejected():
    from sim.regmap import Point
    bad = [Point(key="a", modicon=30036, offset=36, type="int16", space="input",
                 poll_class="critical", source="inferred", range=(-20.0, 80.0))]
    with pytest.raises(RegisterMapError, match="does not"):
        RegisterMap(device_type="X", description="", points=bad)


# ---- codec ---------------------------------------------------------------

def test_scale_roundtrip():
    p = load_map("crv").by_key("return_air_temp")
    assert encode(p, 26.4) == [264]
    assert decode(p, [264]) == pytest.approx(26.4)


def test_signed_roundtrip():
    p = load_map("crv").by_key("supply_air_temp")
    raw = encode(p, -5.3)
    assert raw[0] > 32767, "negative must be two's complement"
    assert decode(p, raw) == pytest.approx(-5.3)


def test_uint32_spans_two_registers_high_word_first():
    p = load_map("crv").by_key("filter_hours")
    assert p.width == 2
    raw = encode(p, 73218)
    assert raw == [73218 >> 16, 73218 & 0xFFFF]
    assert decode(p, raw) == pytest.approx(73218)


def test_enum_roundtrip():
    p = load_map("crv").by_key("operating_state")
    assert decode(p, encode(p, "RUNNING")) == "RUNNING"


def test_bitfield_decode():
    p = load_map("cdu").by_key("alarm_word")
    raw = encode(p, ["LOW_FLOW", "PUMP_FAULT"])
    assert sorted(decode(p, raw)) == ["LOW_FLOW", "PUMP_FAULT"]


def test_out_of_range_is_rejected_not_silently_passed():
    p = load_map("crv").by_key("return_humidity")
    with pytest.raises(RangeError):
        decode(p, [2000])          # 200.0 %RH


def test_undefined_enum_is_rejected():
    p = load_map("crv").by_key("operating_state")
    with pytest.raises(RangeError):
        decode(p, [99])


def test_wrong_register_count_is_rejected():
    p = load_map("crv").by_key("filter_hours")
    with pytest.raises(ValueError, match="expected 2"):
        decode(p, [5])


def test_window_returns_only_wholly_contained_points():
    m = load_map("crv")
    # filter_hours occupies offsets 79-80; a window ending at 80 excludes it
    assert "filter_hours" not in [p.key for p in m.window("input", 79, 1)]
    assert "filter_hours" in [p.key for p in m.window("input", 79, 2)]


def test_map_load_is_deterministic():
    """Simulator and collector build from this same file; loading must be
    stable so the two cannot drift."""
    a = [(p.key, p.offset, p.type, p.scale) for p in load_map("cdu").all_points]
    b = [(p.key, p.offset, p.type, p.scale) for p in load_map("cdu").all_points]
    assert a == b


def test_enum_and_bitfield_labels_are_strings():
    """Regression: YAML 1.1 parses bare OFF/ON/YES/NO as booleans, which turned
    the CRV compressor mode label 'OFF' into False and broke encoding."""
    for name, m in MAPS.items():
        for p in m.all_points:
            for label in list(p.enum.values()) + list(p.bits.values()):
                assert isinstance(label, str), f"{name}.{p.key}: {label!r}"


def test_compressor_mode_off_survives_yaml_parsing():
    p = load_map("crv").by_key("compressor_control_mode")
    assert p.enum[0] == "OFF"
    assert decode(p, encode(p, "OFF")) == "OFF"
