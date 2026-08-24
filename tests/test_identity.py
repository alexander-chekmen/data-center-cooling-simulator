"""Device identity: determinism, uniqueness, and asset-vs-device separation."""
import pytest

from sim.identity import (
    FIRMWARE_POOL,
    component_serial,
    device_identity,
    rack_asset_tag,
)
from sim.regmap import decode_string, encode_string, load_map
from sim.topology import default_topology

TOPO = default_topology()


def test_serials_are_deterministic_across_calls():
    """If serials regenerated, the device-substitution detector would fire on
    every restart and Phase 2 history would stop matching the live fleet."""
    a = device_identity("CRV-001", "CRV")
    b = device_identity("CRV-001", "CRV")
    assert a == b


def test_every_device_in_the_fleet_has_a_unique_serial():
    serials = [device_identity(r.id, "PDU").serial for r in TOPO.racks]
    serials += [device_identity(c.id, "CRV").serial for c in TOPO.crvs]
    serials += [device_identity(c.id, "CDU").serial for c in TOPO.cdus]
    assert len(serials) == 30
    assert len(set(serials)) == 30


def test_serial_encodes_device_type():
    assert device_identity("CRV-001", "CRV").serial.startswith("TE-CRV-")
    assert device_identity("CDU-001", "CDU").serial.startswith("TE-CDU-")


def test_rack_asset_tag_is_not_the_pdu_serial():
    """A rack is an asset with a tag; the PDU inside it is a device with a
    serial. Swapping the PDU must not change the rack's identity."""
    assert rack_asset_tag("RACK-A03") != device_identity("RACK-A03", "PDU").serial


def test_asset_tags_are_unique_and_stable():
    tags = [rack_asset_tag(r.id) for r in TOPO.racks]
    assert len(set(tags)) == len(tags)
    assert rack_asset_tag("RACK-A03") == rack_asset_tag("RACK-A03")


def test_component_serials_differ_per_pump_and_per_device():
    a1 = component_serial("CDU-001", "pump_a")
    b1 = component_serial("CDU-001", "pump_b")
    a2 = component_serial("CDU-002", "pump_a")
    assert len({a1, b1, a2}) == 3


def test_component_serial_differs_from_its_parent_device():
    assert component_serial("CDU-001", "pump_a") != device_identity("CDU-001", "CDU").serial


def test_firmware_is_not_uniform_across_the_fleet():
    """A real fleet is never uniformly patched; drift is a thing to detect."""
    versions = {device_identity(r.id, "PDU").firmware for r in TOPO.racks}
    assert len(versions) > 1
    assert versions <= set(FIRMWARE_POOL)


# ---- string register codec ----------------------------------------------

@pytest.mark.parametrize("text,chars", [
    ("TE-CRV-936A-0B4447", 20), ("AB", 8), ("ODD", 5), ("", 8),
    ("EXACTLYSIXTEEN!!", 16),
])
def test_string_round_trip(text, chars):
    assert decode_string(encode_string(text, chars)) == text


def test_string_packs_two_chars_per_register_high_byte_first():
    """Character order within a word is a classic Modbus integration bug."""
    regs = encode_string("AB", 2)
    assert regs == [(ord("A") << 8) | ord("B")]


def test_oversized_string_is_truncated_not_overflowed():
    regs = encode_string("THIS-IS-FAR-TOO-LONG", 8)
    assert len(regs) == 4
    assert decode_string(regs) == "THIS-IS-"


def test_identity_points_declare_correct_widths():
    for name in ("crv", "cdu", "pdu"):
        m = load_map(name)
        p = m.by_key("serial_number")
        assert p.type == "string" and p.chars == 20 and p.width == 10


def test_string_point_without_chars_is_rejected():
    from sim.regmap import Point, RegisterMap, RegisterMapError
    bad = [Point(key="s", modicon=30001, offset=0, type="string", space="input",
                 poll_class="config", source="inferred", chars=0)]
    with pytest.raises(RegisterMapError, match="needs `chars`"):
        RegisterMap(device_type="X", description="", points=bad)


def test_no_identity_string_is_truncated_by_its_register_field():
    """Regression: pump serials are 18 characters but the register field was
    sized at 16, so every pump serial came back silently clipped. Silent
    truncation of an identifier is worse than a hard failure -- it produces a
    value that looks plausible and matches nothing."""
    from sim.engine.state import build_state
    from sim.points import cdu_values, crv_values, pdu_values
    from sim.regmap import decode, encode, load_map

    state = build_state()
    cases = [
        ("crv", crv_values(state, "CRV-001")),
        ("cdu", cdu_values(state, "CDU-001")),
        ("pdu", pdu_values(state, "RACK-A01")),
    ]
    for device_type, values in cases:
        m = load_map(device_type)
        for point in m.all_points:
            if point.type != "string":
                continue
            original = values[point.key]
            assert decode(point, encode(point, original)) == original, (
                f"{device_type}.{point.key}: {original!r} does not fit "
                f"{point.chars} chars"
            )
