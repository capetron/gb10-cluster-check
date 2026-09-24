# gb10-cluster-check

A read-only health check for the ConnectX-7 cluster fabric on NVIDIA GB10
workstations: the DGX Spark and the OEM GB10 systems (ASUS Ascent GX10, Dell Pro Max
with GB10, HP ZGX Nano, Lenovo ThinkStation PGX, MSI EdgeXpert, Acer Veriton GN100,
Gigabyte AI TOP ATOM).

## The problem it solves

A GB10 cluster link can report "200000Mb/s, Link detected: yes" and still move data at
12 Gb/s. Most of the faults people hit when clustering these boxes are invisible in the
usual status output: one PCIe half of a port configured and the other forgotten, an MTU
that is 9000 on one node and 1500 on the next, a default route that slipped onto a fabric
interface, a stale bond still owning a route, the wrong RoCE GID index in the NCCL
environment, or a switch path that is quietly CPU-forwarded. Each one usually surfaces
later as a slow or hung NCCL job, where it looks like a model or container problem.

`gb10-cluster-check` runs on one node and checks all of that in a few seconds, with a
PASS / WARN / FAIL / SKIP line per check, a summary, a JSON mode, and an exit code you can
use in scripts. It changes nothing: every command it runs is a query.

## What it checks

| Check | How | Fails or warns when |
|---|---|---|
| Platform | `nvidia-smi` | not a GB10 (warning only) |
| Link state and speed, per port, both PCIe halves | `/sys/class/net`, `ethtool` | one half up and the other down, speed below 200G, repeated link drops since boot |
| RDMA mapping | `ibdev2netdev` (or sysfs) | an up interface has no active RDMA device |
| RoCE error counters | `/sys/class/infiniband/*/ports/1/hw_counters` | more than 1,000 `packet_seq_err` / `out_of_sequence` since boot |
| Cable (module EEPROM) | `ethtool -m`, root only | not a QSFP module, longer than 3 m, below 200G, or a part not on the known-good list (informational) |
| MTU | `ip -j addr` | MTU differs across fabric interfaces (fail), or is below 9000 (warn) |
| IPv4 addressing | `ip -j addr`, `ip -j route` | no address, several addresses, both halves in one subnet, a default route via a fabric interface, a route into an interface with no carrier |
| Peer reachability | `ping`, then a do-not-fragment ping at MTU minus 28 bytes | peer unreachable, or small pings pass and jumbo pings fail |
| RoCE GIDs | `/sys/class/infiniband/*/ports/1/gids` | no RoCE v2 GID for the interface's IPv4 address |
| NCCL environment | `NCCL_SOCKET_IFNAME`, `NCCL_IB_HCA`, `NCCL_IB_GID_INDEX` | interface not up, only one PCIe half listed, GID index does not match |
| Bandwidth (opt-in, `--bw`) | `ib_write_bw` and `iperf3` clients | RDMA below about 95 Gb/s for one half |

`mlxlink` is not required. `ethtool -m` needs root; without it the cable check reports
SKIP and never prompts for a password (it only tries `sudo -n`).

## Install

Python 3.6 or newer and the standard tools that already ship on DGX OS (`ip`, `ethtool`,
`ping`, `ibdev2netdev`). No pip packages. `iperf3` and `perftest` (`ib_write_bw`) are only
needed for `--bw`.

```
git clone https://github.com/capetron/gb10-cluster-check.git
cd gb10-cluster-check
./gb10-cluster-check
```

Keep `gb10check.py` next to the `gb10-cluster-check` script, or run
`python3 gb10check.py` directly.

## Usage

```
./gb10-cluster-check                                  # local checks only
./gb10-cluster-check --peer 192.168.100.2 --peer 192.168.200.2
./gb10-cluster-check --discover                       # also test neighbours seen on the fabric subnets
sudo ./gb10-cluster-check                             # adds the cable EEPROM check
./gb10-cluster-check --json > node1.json              # machine-readable
```

Exit code 0 means PASS or WARN, 1 means at least one FAIL. Pass `--ifaces` to check
interface names other than the four GB10 defaults (`enp1s0f0np0`, `enP2p1s0f0np0`,
`enp1s0f1np1`, `enP2p1s0f1np1`). `sudo` drops your NCCL variables, so check those in a
normal shell, or with `sudo -E`.

### Bandwidth test (opt-in)

`--bw` never starts anything on another machine. Start a one-shot server on the peer
yourself, then run the client from this node:

```
# on the peer (exits after one test)
ib_write_bw -d rocep1s0f0 -x 3 --report_gbits -D 5 -q 4 -s 1048576
iperf3 -s -1

# on this node
./gb10-cluster-check --peer 192.168.100.1 --bw
```

Before it runs a test, the tool looks for GPU compute processes, GPU utilization of 10
percent or more, and a high load average. If any of those show the node is busy, it skips
the test unless you pass `--force`. It picks the RDMA device whose subnet holds the peer
and the RoCE v2 GID index it found. One `ib_write_bw` process measures one PCIe half:
expect about 109 to 112 Gb/s. The full cable needs both halves at once (about 196 Gb/s,
roughly 24.5 GB/s), which is what NCCL does when `NCCL_IB_HCA` lists both.

## Sample output

Real runs on GB10 systems in a switched fabric, with addresses replaced by documentation
placeholders. A healthy node, run as a normal user (so the cable EEPROM check is skipped),
with its two peer addresses:

```
[PASS] platform           NVIDIA GB10, driver 580.178.04, kernel 7.0.0-1019-nvidia
[PASS] link.port0         Port 0: enp1s0f0np0,enP2p1s0f0np0 up at 200G (Direct Attach Copper)
[SKIP] link.port1         Port 1: no carrier on enp1s0f1np1,enP2p1s0f1np1 (unused port, or no cable)
[PASS] rdma.map           RDMA devices active: roceP2p1s0f0->enP2p1s0f0np0, rocep1s0f0->enp1s0f0np0
[PASS] rdma.counters      RoCE error counters low (0 sequence errors since boot)
[SKIP] cable.port0        Port 0 module EEPROM not readable (needs root; re-run with sudo)
[PASS] mtu                MTU 9000 on every up fabric interface
[PASS] ip.enp1s0f0np0     enp1s0f0np0 192.168.100.5/24
[PASS] ip.enP2p1s0f0np0   enP2p1s0f0np0 192.168.200.5/24
[PASS] ip.subnets         one subnet per half (192.168.100.0/24, 192.168.200.0/24)
[PASS] ip.default         no default route via a fabric interface
[PASS] roce.gid           RoCE v2 IPv4 GIDs: rocep1s0f0 idx 3, roceP2p1s0f0 idx 3
[SKIP] nccl.env           NCCL_SOCKET_IFNAME not set in this shell (set it where you launch NCCL, e.g. NCCL_SOCKET_IFNAME=enp1s0f0np0)
[SKIP] nccl.env           NCCL_IB_HCA not set in this shell (list both halves of the cabled port, e.g. NCCL_IB_HCA=rocep1s0f0,roceP2p1s0f0)
[SKIP] nccl.env           NCCL_IB_GID_INDEX not set in this shell (RoCE v2 IPv4 GID here is index 3)
[PASS] peer.192.168.100.6     192.168.100.6 reachable via enp1s0f0np0, 0.753 ms, 8972-byte DF ping ok
[PASS] peer.192.168.200.6     192.168.200.6 reachable via enP2p1s0f0np0, 0.790 ms, 8972-byte DF ping ok

Summary: 12 pass, 0 warn, 0 fail, 5 skip -> PASS
```

The same node pair with `--bw`, both ends idle, one-shot servers started on the peer:

```
[PASS] platform           NVIDIA GB10, driver 580.178.04, kernel 7.0.0-1019-nvidia
[PASS] link.port0         Port 0: enp1s0f0np0,enP2p1s0f0np0 up at 200G (Direct Attach Copper)
[SKIP] link.port1         Port 1: no carrier on enp1s0f1np1,enP2p1s0f1np1 (unused port, or no cable)
[PASS] rdma.map           RDMA devices active: roceP2p1s0f0->enP2p1s0f0np0, rocep1s0f0->enp1s0f0np0
[PASS] rdma.counters      RoCE error counters low (0 sequence errors since boot)
[SKIP] cable.port0        Port 0 module EEPROM not readable (needs root; re-run with sudo)
[PASS] mtu                MTU 9000 on every up fabric interface
[PASS] ip.enp1s0f0np0     enp1s0f0np0 192.168.100.5/24
[PASS] ip.enP2p1s0f0np0   enP2p1s0f0np0 192.168.200.5/24
[PASS] ip.subnets         one subnet per half (192.168.100.0/24, 192.168.200.0/24)
[PASS] ip.default         no default route via a fabric interface
[PASS] roce.gid           RoCE v2 IPv4 GIDs: rocep1s0f0 idx 3, roceP2p1s0f0 idx 3
[SKIP] nccl.env           NCCL_SOCKET_IFNAME not set in this shell (set it where you launch NCCL, e.g. NCCL_SOCKET_IFNAME=enp1s0f0np0)
[SKIP] nccl.env           NCCL_IB_HCA not set in this shell (list both halves of the cabled port, e.g. NCCL_IB_HCA=rocep1s0f0,roceP2p1s0f0)
[SKIP] nccl.env           NCCL_IB_GID_INDEX not set in this shell (RoCE v2 IPv4 GID here is index 3)
[PASS] peer.192.168.100.6     192.168.100.6 reachable via enp1s0f0np0, 0.842 ms, 8972-byte DF ping ok
[PASS] bw.iperf3          iperf3 4 streams to 192.168.100.6: 110.5 Gb/s (TCP on the ARM cores; RDMA is the authority for link health)
[PASS] bw.ib_write_bw     ib_write_bw rocep1s0f0 to 192.168.100.6: 111.86 Gb/s, healthy for one PCIe half (expect about 109 to 112)

Summary: 13 pass, 0 warn, 0 fail, 5 skip -> PASS
```

On a third node the tool found a real problem that every status command missed: the link
was up at 200G, but it had dropped and come back 21 times since boot.

```
[PASS] platform           NVIDIA GB10, driver 580.178.04, kernel 7.0.0-1019-nvidia
[PASS] link.port0         Port 0: enp1s0f0np0,enP2p1s0f0np0 up at 200G (Direct Attach Copper)
[WARN] link.port0.drops   Port 0: link dropped 21 times since boot (a healthy link shows 0); check `journalctl -k | grep enp1s0f0np0` for when, then reseat or swap the cable if it keeps happening
[SKIP] link.port1         Port 1: no carrier on enp1s0f1np1,enP2p1s0f1np1 (unused port, or no cable)
[PASS] rdma.map           RDMA devices active: roceP2p1s0f0->enP2p1s0f0np0, rocep1s0f0->enp1s0f0np0
[PASS] rdma.counters      RoCE error counters low (0 sequence errors since boot)
[SKIP] cable.port0        Port 0 module EEPROM not readable (needs root; re-run with sudo)
[PASS] mtu                MTU 9000 on every up fabric interface
[PASS] ip.enp1s0f0np0     enp1s0f0np0 192.168.100.1/24
[PASS] ip.enP2p1s0f0np0   enP2p1s0f0np0 192.168.200.1/24
[PASS] ip.subnets         one subnet per half (192.168.100.0/24, 192.168.200.0/24)
[PASS] ip.default         no default route via a fabric interface
[PASS] roce.gid           RoCE v2 IPv4 GIDs: rocep1s0f0 idx 3, roceP2p1s0f0 idx 3
[SKIP] nccl.env           NCCL_SOCKET_IFNAME not set in this shell (set it where you launch NCCL, e.g. NCCL_SOCKET_IFNAME=enp1s0f0np0)
[SKIP] nccl.env           NCCL_IB_HCA not set in this shell (list both halves of the cabled port, e.g. NCCL_IB_HCA=rocep1s0f0,roceP2p1s0f0)
[SKIP] nccl.env           NCCL_IB_GID_INDEX not set in this shell (RoCE v2 IPv4 GID here is index 3)
[PASS] peer.192.168.100.2     192.168.100.2 reachable via enp1s0f0np0, 0.731 ms, 8972-byte DF ping ok
[PASS] peer.192.168.200.2     192.168.200.2 reachable via enP2p1s0f0np0, 0.950 ms, 8972-byte DF ping ok

Summary: 12 pass, 1 warn, 0 fail, 5 skip -> WARN
```

`--json` prints the same results with details (abridged here):

```
{
  "tool": "gb10-cluster-check",
  "version": "0.1.0",
  "summary": {"pass": 12, "warn": 0, "fail": 0, "skip": 5, "result": "PASS"},
  "results": [
    {
      "check": "roce.gid",
      "status": "PASS",
      "message": "RoCE v2 IPv4 GIDs: rocep1s0f0 idx 3, roceP2p1s0f0 idx 3",
      "detail": {"gid_index": 3}
    }
  ]
}
```

## Wiring

Every GB10 system has two QSFP ports on one ConnectX-7. Port 0 is the cage next to the
RJ45 jack and Port 1 is the one further from it. Each port is fed by two PCIe Gen 5 x4
links, so Linux shows it as two Ethernet interfaces and two RDMA devices:

| Port | Ethernet interfaces | RDMA devices |
|---|---|---|
| Port 0 | `enp1s0f0np0`, `enP2p1s0f0np0` | `rocep1s0f0`, `roceP2p1s0f0` |
| Port 1 | `enp1s0f1np1`, `enP2p1s0f1np1` | `rocep1s0f1`, `roceP2p1s0f1` |

One half carries about 100 Gb/s. Both halves together on one cable measure 196 Gb/s
(about 24.5 GB/s). Configure both halves on every node.

**Two nodes.** One cable between the two units (NVIDIA's "Connect Two Sparks" playbook).
Using Port 0 on both keeps the interface names the same on each side.

**Three nodes, switchless ring.** Three cables, and every cable joins a Port 0 to a Port 1,
as in NVIDIA's "Connect Three DGX Spark in a Ring Topology" playbook:

| Cable | From | To |
|---|---|---|
| 1 | Node 1 Port 0 | Node 2 Port 1 |
| 2 | Node 2 Port 0 | Node 3 Port 1 |
| 3 | Node 3 Port 0 | Node 1 Port 1 |

The playbook's addressing puts matching subnets on a Port 0 and the Port 1 it faces. Wire
Port 0 to Port 0 and every interface still shows Up, but the two ends sit in different
subnets and ping fails. Three nodes is the switchless ceiling: with two ports per node,
three cables give every node a direct link to every other node.

**Four or more nodes.** A 200G switch. NVIDIA documents direct cabling for up to three
systems and a switch for four. Each node runs one cable from Port 0 to a 200G switch port
(or to one leg of a QSFP56-DD to 2 x QSFP56 breakout). Set l2mtu 9216 on the switch ports
and keep the fabric in one hardware-offloaded bridge (see known issues). Wired as four
point-to-point links without a switch, two of the four nodes have no direct path, and stock
NCCL needs one between every pair.

Run the tool on every node after cabling. On a ring, pass each neighbour's addresses with
`--peer`. On a switch, `--discover` tests every node seen on the fabric subnets.

## Known issues

Collected from the NVIDIA developer forums and from our own clusters. Each has the check
that catches it.

1. **200G link, 12 to 13 Gb/s of RDMA.** The link negotiates at full speed and status
   commands look clean, but `ib_write_bw` reads a fraction of line rate. On our systems an
   OS, driver and firmware update plus a reboot took one unit from 12.74 to 111.86 Gb/s;
   other owners fixed it with a full power drain (unplug for a minute) after the first
   cabling. Forum threads:
   [373538](https://forums.developer.nvidia.com/t/373538),
   [363461](https://forums.developer.nvidia.com/t/363461),
   [383649](https://forums.developer.nvidia.com/t/383649).
   Caught by: `--bw` (FAIL below 50 Gb/s).
2. **One TCP stream sits near 12 Gb/s.** That is the ARM cores, not the link, and it looks
   the same as the throttle above. Use `ib_write_bw` as the authority, or several parallel
   streams (`--bw` uses four and measured 110.5 Gb/s above).
3. **NCCL tops out near 100 Gb/s on one cable.** Only one PCIe half is in use because
   `NCCL_IB_HCA` lists one device. List both halves of the cabled port.
   Forum: [362403](https://forums.developer.nvidia.com/t/362403). Caught by: `nccl.env`.
4. **MTU mismatch.** One node at 1500 on a 9000 fabric: small pings work and NCCL
   bootstrap stalls. Prove the path with `ping -M do -s 8972 <peer>`, and on a switch set
   l2mtu 9216 as well. Caught by: `mtu`, `peer`.
5. **Slow through a MikroTik switch, with `packet_seq_err` climbing.** On the CRS812 family
   the switch chip hardware-offloads only one bridge. Ports in a second bridge are
   forwarded by the switch CPU: we measured 196 Gb/s fall to 4.85 Gb/s, with sequence
   errors and congestion notifications on the sender. Forum:
   [378042](https://forums.developer.nvidia.com/t/378042). Caught by: `rdma.counters`,
   `--bw`.
6. **No internet after plugging in the QSFP cable.** A fabric interface picked up a
   default route (DHCP or a missing never-default setting). Forum:
   [364126](https://forums.developer.nvidia.com/t/364126). Caught by: `ip.default`.
7. **Some peers answer and others do not.** A retired bond or old connection profile with
   no carrier still holds an address and a route, and the kernel sends part of the subnet
   into it. Caught by: `ip.stale`, `ip.<iface>`.
8. **Three-node ring does not ping.** Cables wired Port 0 to Port 0. Rewire as in the table
   above. Forum: [365160](https://forums.developer.nvidia.com/t/365160),
   [376215](https://forums.developer.nvidia.com/t/376215). Caught by: `peer`.
9. **Four nodes in a ring, NCCL fails on some pairs.** Non-adjacent nodes have no direct
   link. It works only with community NCCL patches that relay over two hops; otherwise use
   a switch. Forum: [368726](https://forums.developer.nvidia.com/t/368726),
   [377435](https://forums.developer.nvidia.com/t/377435).
10. **A second cable between the same two nodes.** Each port is already two PCIe halves,
    so a second cable between the same pair is a second link, not a faster one. Configure
    both halves of the first cable instead. Forum:
    [383142](https://forums.developer.nvidia.com/t/383142).
11. **Link drops.** A link that goes down and back up for a couple of seconds at a time
    shows nothing in a status snapshot. The tool reads the kernel's carrier-down counter
    and warns above three drops since boot. Caught by: `link.port<N>.drops`.

## Where to get a compatible cable

NVIDIA approves the Amphenol NJAAKK-N911 and NJAAKK0006 and the Luxshare LMTQF022-SD-R
(0.4 to 0.5 m QSFP112 passive DAC) for these ports, and Lenovo sells its own 4X91U42988.
Any of them links at 200G. Petronella Technology Group, Inc. keeps the 0.5 m cable in
stock in the United States:
[GB10 / DGX Spark cluster cable](https://petronellatech.com/hardware/dgx-spark-cluster-cable/).

## More reading

- [capetron/gb10-cluster-guide](https://github.com/capetron/gb10-cluster-guide): the full
  write-up behind these checks, from two nodes to a six-node switched fabric, with the
  measurement commands (CC BY 4.0).
- NVIDIA DGX Spark User Guide, ConnectX-7 networking:
  https://docs.nvidia.com/dgx/dgx-spark/spark-clustering.html
- NVIDIA playbooks: [Connect Two Sparks](https://build.nvidia.com/spark/connect-two-sparks),
  [Connect Three Sparks](https://build.nvidia.com/spark/connect-three-sparks).

## Tests

```
python3 -m unittest discover -s tests
```

The parser tests use real command output captured on GB10 systems, plus two hand-written
module EEPROM fixtures (reading the EEPROM needs root).

## Contributing

Issues and pull requests are welcome. For a new check or a wrong verdict, include the
command output it is based on (with your addresses replaced). The tool must stay read-only
and dependency-free.

## License

MIT. See [LICENSE](LICENSE). Maintained by Petronella Technology Group, Inc.
