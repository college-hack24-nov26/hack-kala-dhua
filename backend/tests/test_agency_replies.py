"""The agency reply set: every reply formats, and the draw is reproducible."""

from app.ops import agency_replies as ar


def test_every_reply_formats():
    for kind, spec in ar.REPLY_SET.items():
        assert spec["texts"], kind
        for t in spec["texts"]:
            out = t.format(qty=3, given=1, rest=2, wait=20, kind="ambulances", base="Red Cross base")
            assert "{" not in out, (kind, t)


def test_draw_is_reproducible_and_varied():
    weights = {k: v["weight"] for k, v in ar.REPLY_SET.items()}
    a = [ar._pick(ar._rng(f"req-{i}", 0), weights) for i in range(200)]
    b = [ar._pick(ar._rng(f"req-{i}", 0), weights) for i in range(200)]
    assert a == b
    assert set(a) == set(ar.REPLY_SET)  # every kind actually happens


def test_after_delay_never_asks_again():
    assert "need_info" not in ar._AFTER_DELAY and "delayed" not in ar._AFTER_DELAY


def test_region_from_ward():
    assert ar._region("w-gzb-3") == "ncr"
    assert ar._region("w-12") == "pune"
    assert ar._region(None) == "pune"
