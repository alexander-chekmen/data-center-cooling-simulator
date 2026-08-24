"""Read-planning tests."""
from collector.batching import plan_reads
from sim.regmap import load_all, load_map

MAPS = load_all()


def test_batching_is_not_one_request_per_point():
    """The whole point: a naive collector issues one request per point and
    collapses at 30 devices."""
    points = load_map("crv").points
    blocks = plan_reads(points)
    assert len(points) > 20
    assert len(blocks) <= 4, f"{len(points)} points should not need {len(blocks)} requests"


def test_every_point_lands_in_exactly_one_block():
    for name, m in MAPS.items():
        covered = [p.key for b in plan_reads(m.all_points) for p in b.points]
        assert sorted(covered) == sorted(p.key for p in m.all_points), name
        assert len(covered) == len(set(covered)), f"{name}: duplicated point"


def test_blocks_never_exceed_the_protocol_limit():
    for m in MAPS.values():
        for b in plan_reads(m.all_points):
            assert 1 <= b.count <= 125


def test_blocks_cover_their_points():
    for m in MAPS.values():
        for b in plan_reads(m.all_points):
            for p in b.points:
                assert b.address <= p.offset and p.last_offset <= b.end


def test_input_and_holding_are_never_mixed():
    for b in plan_reads(load_map("cdu").all_points):
        assert len({p.space for p in b.points}) == 1


def test_large_gaps_force_a_new_block():
    m = load_map("crv")
    pts = [m.by_key("operating_state"), m.by_key("alarm_word")]   # offsets 0 and 89
    assert len(plan_reads(pts, max_gap=8)) == 2


def test_small_gaps_are_bridged():
    m = load_map("crv")
    pts = [m.by_key("remote_temp_1"), m.by_key("remote_temp_5")]  # offsets 47, 51
    assert len(plan_reads(pts, max_gap=8)) == 1
