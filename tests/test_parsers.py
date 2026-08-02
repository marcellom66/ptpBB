from beagleptp.parsers import Ptp4lLogParser, parse_pmc_dataset


def test_ptp4l_parser_tracks_state_master_and_sample() -> None:
    parser = Ptp4lLogParser()
    assert parser.parse("port 1: LISTENING to SLAVE on MASTER_CLOCK_SELECTED") is None
    assert parser.parse("selected best master clock 001122.fffe.334455") is None
    sample = parser.parse("master offset       -42 s2 freq  +12.5 path delay  18340", 123)
    assert sample is not None
    assert sample.timestamp_ns == 123
    assert sample.offset_ns == -42
    assert sample.frequency_ppb == 12.5
    assert sample.mean_path_delay_ns == 18340
    assert sample.port_state == "SLAVE"
    assert sample.master_clock_id == "001122.fffe.334455"


def test_pmc_parser_converts_numbers() -> None:
    payload = """
    sending: GET CURRENT_DATA_SET
        001122.fffe.334455-0 seq 0 RESPONSE MANAGEMENT CURRENT_DATA_SET
            stepsRemoved 2
            offsetFromMaster -127
            meanPathDelay 18340.5
            portState SLAVE
    """
    assert parse_pmc_dataset(payload) == {
        "stepsRemoved": 2,
        "offsetFromMaster": -127,
        "meanPathDelay": 18340.5,
        "portState": "SLAVE",
    }
