from settings.merge import DEFAULTS, load


def test_override_nested_value():
    assert load({"server": {"port": 9000}})["server"] == {"host": "localhost", "port": 9000}


def test_loading_does_not_change_the_defaults():
    load({"server": {"port": 9000}, "debug": True})
    assert load({}) == {"server": {"host": "localhost", "port": 8000}, "debug": False}
    assert DEFAULTS["server"]["port"] == 8000
