"""Parser tests.

Every fixture below is a verbatim byte string captured off 192.0.2.10 by
ghprobe.py -- not hand-written. That matters: the point of these tests is to pin
behaviour against what the device actually emits.
"""

import pytest

from greenheron import protocol as p

# One SWITCHUPDATE, exactly as it arrived (the recurring 33-byte segment).
UPDATE_33 = b"SWITCHUPDATE\x1fAS-84F-2\x1fOFF\x1f0\x1f-27\r\n"

# The recurring 109-byte segment: three records in one recv(), and note it
# begins mid-cycle with a LOCKS rather than an UPDATE.
TRIPLE_109 = (
    b"SWITCHLOCKS\x1fAS-84F-2\x1fOFF\x1fOFF\x1fOFF\x1fOFF\r\n"
    b"SWITCHUPDATE\x1fAS-84F-1\x1fOFF\x1f0\x1f-27\r\n"
    b"SWITCHLOCKS\x1fAS-84F-1\x1fOFF\x1fOFF\x1fOFF\x1fOFF\r\n"
)

ADD_195 = (
    b"SWITCHADD\x1f1\x1fAS-84F-1"
    b"\x1fBeam-10\x1d0\x1d0\x1dfalse"
    b"\x1fBeam-15\x1d0\x1d0\x1dfalse"
    b"\x1fBeam-20\x1d0\x1d0\x1dfalse"
    b"\x1fEFHW-40\x1d0\x1d0\x1dfalse"
    b"\x1fEFHW-80\x1d0\x1d0\x1dfalse"
    b"\x1fDipole-6\x1d0\x1d0\x1dfalse"
    b"\x1fVertical-NoFilters\x1d0\x1d0\x1dfalse"
    b"\x1fDummy Load\x1d0\x1d0\x1dfalse"
    b"\x1fOFF\x1d0\x1d0\x1dfalse\r\n"
)


def test_fixture_lengths_match_the_wire():
    # If these drift, the fixtures are no longer what the device sent.
    assert len(UPDATE_33) == 33
    assert len(TRIPLE_109) == 109
    assert len(ADD_195) == 195


# --------------------------------------------------------------------------
# Framing
# --------------------------------------------------------------------------

def test_one_segment_can_hold_several_records():
    records, rest = p.split_records(TRIPLE_109)
    assert len(records) == 3
    assert rest == b""


def test_partial_record_is_held_back_not_parsed():
    records, rest = p.split_records(b"SWITCHUPDATE\x1fAS-84F-2\x1fOFF\x1f0\x1f-2")
    assert records == []
    assert rest == b"SWITCHUPDATE\x1fAS-84F-2\x1fOFF\x1f0\x1f-2"


@pytest.mark.parametrize("cut", range(1, len(UPDATE_33)))
def test_record_split_at_every_possible_byte_still_parses(cut):
    """A record arriving in two pieces must survive, wherever TCP cut it --
    including between the CR and the LF."""
    first, second = UPDATE_33[:cut], UPDATE_33[cut:]

    records, buf = p.split_records(first)
    assert records == []
    records, buf = p.split_records(buf + second)

    assert len(records) == 1
    assert p.parse(records[0]) == p.parse(UPDATE_33[:-2])


def test_records_accumulate_across_reads():
    buf = b""
    seen = []
    for chunk in (ADD_195[:100], ADD_195[100:] + TRIPLE_109[:40], TRIPLE_109[40:]):
        records, buf = p.split_records(buf + chunk)
        seen.extend(records)
    assert buf == b""
    assert [p.parse(r).__class__.__name__ for r in seen] == [
        "SwitchAdd", "SwitchLocks", "SwitchUpdate", "SwitchLocks",
    ]


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def test_parse_switchupdate():
    rec = p.parse(UPDATE_33[:-2])
    assert isinstance(rec, p.SwitchUpdate)
    assert rec.switch == "AS-84F-2"
    assert rec.selected == "OFF"
    # Carried through verbatim, never interpreted.
    assert rec.unknown_c == "0"
    assert rec.wireless_signal == "-27"


def test_parse_switchlocks():
    records, _ = p.split_records(TRIPLE_109)
    rec = p.parse(records[0])
    assert isinstance(rec, p.SwitchLocks)
    assert rec.switch == "AS-84F-2"
    assert rec.locks == ("OFF", "OFF", "OFF", "OFF")


def test_parse_switchadd_roster():
    rec = p.parse(ADD_195[:-2])
    assert isinstance(rec, p.SwitchAdd)
    assert rec.switch == "AS-84F-1"
    assert rec.unknown_group == "1"
    assert rec.port_names == (
        "Beam-10", "Beam-15", "Beam-20", "EFHW-40", "EFHW-80",
        "Dipole-6", "Vertical-NoFilters", "Dummy Load", "OFF",
    )


def test_port_name_containing_a_space_survives():
    rec = p.parse(ADD_195[:-2])
    assert rec.ports[7].name == "Dummy Load"


def test_switchadd_unknown_subfields_are_carried_not_dropped():
    rec = p.parse(ADD_195[:-2])
    assert all(
        (q.unknown_a, q.unknown_b, q.unknown_flag) == ("0", "0", "false")
        for q in rec.ports
    )


def test_unrecognised_verb_is_surfaced_not_raised():
    """32 seconds of an idle device is not proof of the whole vocabulary."""
    rec = p.parse(b"SWITCHTHING\x1fAS-84F-1\x1fwhatever")
    assert isinstance(rec, p.Unknown)
    assert rec.verb == "SWITCHTHING"
    assert rec.fields == ("AS-84F-1", "whatever")


def test_garbage_does_not_raise():
    assert isinstance(p.parse(b"\x00\xff not a record"), p.Unknown)


# --------------------------------------------------------------------------
# Command model
# --------------------------------------------------------------------------

def test_observed_lengths_admit_exactly_one_verb_length_per_model():
    """The constraint that makes the field model checkable.

    29/29/32/25 with 8-character switch names is only explicable by a 10-char
    verb (model A) or an 8-char verb (model B). Any real capture must agree with
    one of these; if it agrees with neither, the field model is wrong.
    """
    viable = p.verbs_consistent_with()
    assert viable[False] == [10]
    assert viable[True] == [8]


def test_the_implied_port_sequence_is_a_coherent_operator_session():
    fmt = p.CommandFormat(verb="A" * 10)
    implied = [n - fmt.overhead for n in p.OBSERVED_COMMAND_LENGTHS]
    assert implied == [7, 7, 10, 3]
    # 7 = a Beam-/EFHW- antenna, 10 = "Dummy Load", 3 = "OFF"
    assert len("Dummy Load") == 10 and len("OFF") == 3
    assert {len(x) for x in ("Beam-20", "EFHW-40")} == {7}


def test_encode_round_trips_through_the_parser():
    """Whatever the verb turns out to be, encoding must produce a record the
    same framing code can take apart again."""
    fmt = p.CommandFormat(verb="SWITCHPORT")
    wire = fmt.encode("AS-84F-1", "Beam-20")
    records, rest = p.split_records(wire)
    assert rest == b""
    assert p.parse(records[0]).fields == ("AS-84F-1", "Beam-20")


@pytest.mark.parametrize("port", p.KNOWN_PORTS)
def test_predicted_length_matches_actual_encoding(port):
    for fmt in (p.CommandFormat("A" * 10), p.CommandFormat("B" * 8, include_group=True)):
        assert len(fmt.encode("AS-84F-1", port)) == fmt.predicted_length("AS-84F-1", port)
