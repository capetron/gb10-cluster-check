import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import gb10check as g  # noqa: E402


def fx(name):
    with open(os.path.join(HERE, "fixtures", name)) as f:
        return f.read()


def link(carrier=True, speed=200000):
    return {"carrier": carrier, "speed": speed}


class TestEthtool(unittest.TestCase):
    def test_up_200g(self):
        e = g.parse_ethtool(fx("ethtool_200g.txt"))
        self.assertEqual(e["speed"], 200000)
        self.assertTrue(e["link"])
        self.assertEqual(e["port"], "Direct Attach Copper")
        self.assertEqual(e["lanes"], 4)

    def test_down(self):
        e = g.parse_ethtool(fx("ethtool_down.txt"))
        self.assertIsNone(e["speed"])
        self.assertFalse(e["link"])


class TestIbdev2netdev(unittest.TestCase):
    def test_map(self):
        m = g.parse_ibdev2netdev(fx("ibdev2netdev.txt"))
        self.assertEqual(m["enp1s0f0np0"], ("rocep1s0f0", "Up"))
        self.assertEqual(m["enP2p1s0f1np1"], ("roceP2p1s0f1", "Down"))
        self.assertEqual(len(m), 4)


class TestModule(unittest.TestCase):
    def test_known_cmis_cable(self):
        mod = g.parse_module_eeprom(fx("ethtool_m_cmis_synthetic.txt"))
        self.assertEqual(mod["part"], "NJAAKR-0006")
        self.assertAlmostEqual(mod["length_m"], 0.5)
        self.assertTrue(mod["serial_present"])
        self.assertNotIn("EXAMPLE0000", json.dumps(mod))
        status, msg = g.classify_cable(mod, 200000)
        self.assertEqual(status, g.PASS)
        self.assertIn("Amphenol", msg)

    def test_long_100g_cable_warns(self):
        mod = g.parse_module_eeprom(fx("ethtool_m_sff8636_long_synthetic.txt"))
        self.assertEqual(mod["length_m"], 5.0)
        status, msg = g.classify_cable(mod, 100000)
        self.assertEqual(status, g.WARN)
        self.assertIn("not on the known-good list", msg)
        self.assertIn("100000", msg)

    def test_length_units(self):
        self.assertEqual(g.parse_length_m("0.001km"), 1.0)
        self.assertEqual(g.parse_length_m("1 m"), 1.0)
        self.assertIsNone(g.parse_length_m("n/a"))


class TestGids(unittest.TestCase):
    def test_v2_ipv4(self):
        entries = [
            (0, "fe80:0000:0000:0000:0000:0000:0000:0001", "IB/RoCE v1"),
            (1, "fe80:0000:0000:0000:0000:0000:0000:0001", "RoCE v2"),
            (2, "0000:0000:0000:0000:0000:ffff:c0a8:6401", "IB/RoCE v1"),
            (3, "0000:0000:0000:0000:0000:ffff:c0a8:6401", "RoCE v2"),
            (4, "0000:0000:0000:0000:0000:0000:0000:0000", None),
        ]
        self.assertEqual(g.parse_gids(entries), {"192.168.100.1": 3})


class TestBandwidthParsers(unittest.TestCase):
    def test_ib_write_bw(self):
        self.assertAlmostEqual(g.parse_ib_write_bw(fx("ib_write_bw.txt")), 111.86)

    def test_iperf3(self):
        doc = {"end": {"sum_received": {"bits_per_second": 110.6e9}}}
        self.assertAlmostEqual(g.parse_iperf3_json(json.dumps(doc)), 110.6)
        self.assertIsNone(g.parse_iperf3_json("not json"))

    def test_ping(self):
        out = ("3 packets transmitted, 3 received, 0% packet loss, time 402ms\n"
               "rtt min/avg/max/mdev = 0.700/0.899/1.100/0.160 ms\n")
        self.assertEqual(g.parse_ping(out), (3, 3, 0.899))
        self.assertEqual(g.parse_ping("3 packets transmitted, 0 received")[0], 0)


class TestIpAnalysis(unittest.TestCase):
    links = {"enp1s0f0np0": link(), "enP2p1s0f0np0": link(),
             "enp1s0f1np1": link(False, None), "enP2p1s0f1np1": link(False, None)}

    def addrs(self, a="192.168.100.1/24", b="192.168.200.1/24", extra=None):
        d = {"enp1s0f0np0": {"addrs": [a]}, "enP2p1s0f0np0": {"addrs": [b]}}
        d.update(extra or {})
        return d

    def statuses(self, res, check):
        return [s for s, c, _ in res if c == check]

    def test_clean(self):
        routes = [{"dst": "default", "dev": "eth0"},
                  {"dst": "192.168.100.0/24", "dev": "enp1s0f0np0", "flags": []},
                  {"dst": "172.17.0.0/16", "dev": "docker0", "flags": ["linkdown"]}]
        res = g.analyze_ip(self.links, self.addrs(), routes)
        self.assertNotIn(g.FAIL, [s for s, _, _ in res])
        self.assertNotIn(g.WARN, [s for s, _, _ in res])

    def test_default_route_on_fabric(self):
        res = g.analyze_ip(self.links, self.addrs(),
                           [{"dst": "default", "dev": "enp1s0f0np0"}])
        self.assertEqual(self.statuses(res, "ip.default"), [g.FAIL])

    def test_shared_subnet(self):
        res = g.analyze_ip(self.links, self.addrs(b="192.168.100.2/24"), [])
        self.assertEqual(self.statuses(res, "ip.subnets"), [g.WARN])

    def test_stale_bond_route(self):
        routes = [{"dst": "192.168.100.0/30", "dev": "bond0", "flags": ["linkdown"]}]
        res = g.analyze_ip(self.links, self.addrs(), routes)
        self.assertEqual(self.statuses(res, "ip.stale"), [g.FAIL])

    def test_down_port_holding_address(self):
        extra = {"enp1s0f1np1": {"addrs": ["192.168.101.1/30"]}}
        res = g.analyze_ip(self.links, self.addrs(extra=extra), [])
        self.assertEqual(self.statuses(res, "ip.enp1s0f1np1"), [g.WARN])


class TestNccl(unittest.TestCase):
    pairs = [("enp1s0f0np0", "rocep1s0f0"), ("enP2p1s0f0np0", "roceP2p1s0f0")]

    def test_unset_is_skip(self):
        res = g.nccl_advice({}, self.pairs, 3)
        self.assertEqual([s for s, _ in res], [g.SKIP, g.SKIP, g.SKIP])

    def test_single_hca_warns(self):
        env = {"NCCL_SOCKET_IFNAME": "enp1s0f0np0", "NCCL_IB_HCA": "rocep1s0f0",
               "NCCL_IB_GID_INDEX": "3"}
        res = g.nccl_advice(env, self.pairs, 3)
        self.assertEqual([s for s, _ in res], [g.PASS, g.WARN, g.PASS])

    def test_exact_match_prefix_and_wrong_gid(self):
        env = {"NCCL_SOCKET_IFNAME": "=eth9", "NCCL_IB_HCA": "=rocep1s0f0,roceP2p1s0f0",
               "NCCL_IB_GID_INDEX": "1"}
        res = g.nccl_advice(env, self.pairs, 3)
        self.assertEqual([s for s, _ in res], [g.WARN, g.PASS, g.WARN])


if __name__ == "__main__":
    unittest.main()
