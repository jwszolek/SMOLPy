from smolpy import Network


def test_network_creation() -> None:
    net = Network("test-net")
    assert net.name == "test-net"


def test_adapter_registration() -> None:
    net = Network("test-net")
    a = net.adapter("host-A", ip="10.0.0.1")
    assert a.name == "host-A"
    assert a.ip == "10.0.0.1"
    assert "host-A" in net._nodes


def test_link_connects_nodes() -> None:
    net = Network("test-net")
    a = net.adapter("host-A", ip="10.0.0.1")
    sw = net.switch("sw1", ports=8)
    lnk = net.link(a, sw, speed=1000, length=5)
    assert lnk.speed == 1000
    assert lnk.speed_bps == 1_000_000_000
    assert lnk in a.links
    assert lnk in sw.links


def test_duplicate_node_raises() -> None:
    import pytest
    net = Network("test-net")
    net.adapter("host-A", ip="10.0.0.1")
    with pytest.raises(ValueError, match="already exists"):
        net.adapter("host-A", ip="10.0.0.2")


def test_simulate_returns_result() -> None:
    net = Network("test-net")
    a = net.adapter("host-A", ip="10.0.0.1")
    b = net.adapter("host-B", ip="10.0.0.2")
    sw = net.switch("sw1", ports=4)
    net.link(a, sw, speed=1000, length=5)
    net.link(b, sw, speed=1000, length=5)
    a.sends(to=b, rate=100, size=512, pattern="constant")
    result = net.simulate(duration=1_000)
    assert result is not None
