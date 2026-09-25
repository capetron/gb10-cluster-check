#!/usr/bin/env python3
"""gb10-cluster-check: read-only health check for the ConnectX-7 cluster fabric
on NVIDIA GB10 workstations (DGX Spark and the OEM GB10 systems).

Standard library only. Nothing here changes system state: every command it runs
is a query (ethtool, ip, ibdev2netdev, nvidia-smi, ping, and, only with --bw,
an iperf3 / ib_write_bw client against a server you started).
"""

import argparse
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys

VERSION = "0.1.0"

# The four Ethernet halves of the two QSFP ports on every GB10 system.
# Port 0 is the QSFP cage next to the RJ45 jack.
DEFAULT_IFACES = [
    ("enp1s0f0np0", 0, "A"),
    ("enP2p1s0f0np0", 0, "B"),
    ("enp1s0f1np1", 1, "A"),
    ("enP2p1s0f1np1", 1, "B"),
]
EXPECTED_SPEED = 200000  # Mb/s per port, as negotiated by the GB10 ConnectX-7

# Cables NVIDIA approves for the DGX Spark QSFP ports, plus parts owners report
# working at full rate. Matching is by substring of the module's Vendor PN.
KNOWN_CABLES = {
    "NJAAKK-N911": "Amphenol, NVIDIA-approved, 0.4 m QSFP112",
    "NJAAKK0006": "Amphenol, NVIDIA-approved, 0.5 m QSFP112",
    "NJAAKR-0006": "Amphenol, 0.5 m QSFP112 (same family, owners report full rate)",
    "LMTQF022-SD-R": "Luxshare, NVIDIA-approved, 0.4 m QSFP112",
    "4X91U42988": "Lenovo ThinkStation PGX QSFP link cable, 0.4 m",
}

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"
RANK = {PASS: 0, SKIP: 0, WARN: 1, FAIL: 2}


# ---------------------------------------------------------------------------
# helpers


def run(cmd, timeout=15):
    """Run a command, never raise. Returns (rc, stdout, stderr)."""
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout, universal_newlines=True)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", "not found: %s" % cmd[0]
    except subprocess.TimeoutExpired:
        return 124, "", "timeout after %ss: %s" % (timeout, " ".join(cmd))


def have(tool):
    return shutil.which(tool) is not None


def read(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except (OSError, IOError, ValueError):
        return default


class Report:
    def __init__(self):
        self.results = []

    def add(self, check, status, message, **detail):
        self.results.append({"check": check, "status": status,
                             "message": message, "detail": detail})

    def counts(self):
        c = {PASS: 0, WARN: 0, FAIL: 0, SKIP: 0}
        for r in self.results:
            c[r["status"]] += 1
        return c

    def worst(self):
        w = PASS
        for r in self.results:
            if RANK[r["status"]] > RANK[w]:
                w = r["status"]
        return w


# ---------------------------------------------------------------------------
# parsers (pure functions, unit tested with fixtures)


def parse_ethtool(text):
    """Parse `ethtool <iface>` into speed (Mb/s or None), link (bool), port."""
    out = {"speed": None, "link": None, "port": None, "lanes": None}
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"Speed:\s*(\d+)Mb/s", line)
        if m:
            out["speed"] = int(m.group(1))
        elif line.startswith("Speed:"):
            out["speed"] = None
        m = re.match(r"Link detected:\s*(yes|no)", line)
        if m:
            out["link"] = m.group(1) == "yes"
        m = re.match(r"Port:\s*(.+)", line)
        if m:
            out["port"] = m.group(1).strip()
        m = re.match(r"Lanes:\s*(\d+)", line)
        if m:
            out["lanes"] = int(m.group(1))
    return out


def parse_ibdev2netdev(text):
    """`rocep1s0f0 port 1 ==> enp1s0f0np0 (Up)` -> {netdev: (rdmadev, state)}."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"\s*(\S+)\s+port\s+(\d+)\s+==>\s+(\S+)\s+\((\w+)\)", line)
        if m:
            out[m.group(3)] = (m.group(1), m.group(4))
    return out


def parse_length_m(value):
    """'0.5m', '0.50 m', '1 m', '0.001km' -> metres as float, else None."""
    m = re.search(r"([\d.]+)\s*(km|m)\b", value)
    if not m:
        return None
    n = float(m.group(1))
    return n * 1000.0 if m.group(2) == "km" else n


def parse_module_eeprom(text):
    """Parse `ethtool -m <iface>` (SFF-8636 or CMIS layout)."""
    fields = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        fields[k.strip()] = v.strip()
    out = {"identifier": None, "vendor": None, "part": None, "serial_present": False,
           "length_m": None, "media": None, "raw_keys": len(fields)}
    for k, v in fields.items():
        kl = k.lower()
        if kl == "identifier":
            out["identifier"] = v
        elif kl == "vendor name":
            out["vendor"] = v
        elif kl == "vendor pn":
            out["part"] = v
        elif kl == "vendor sn":
            out["serial_present"] = bool(v)  # never stored or printed
        elif kl.startswith("length (copper") or kl.startswith("length (active"):
            out["length_m"] = parse_length_m(v)
        elif kl in ("transmitter technology", "media interface technology",
                    "module type", "connector"):
            out["media"] = (out["media"] + "; " if out["media"] else "") + v
    return out


def classify_cable(mod, speed):
    """Return (status, message) for a parsed module EEPROM and link speed."""
    part = (mod.get("part") or "").upper()
    ident = (mod.get("identifier") or "")
    known = None
    for pn, desc in KNOWN_CABLES.items():
        if pn.upper() in part.replace(" ", ""):
            known = desc
            break
    notes = []
    status = PASS
    if "QSFP" not in ident.upper() and "CMIS" not in ident.upper():
        status = WARN
        notes.append("identifier '%s' is not a QSFP module" % ident)
    length = mod.get("length_m")
    if length is not None:
        if length > 3.0:
            status = WARN
            notes.append("%.1f m is long for a passive 200G copper DAC; expect errors "
                         "or a lower speed beyond about 2 to 3 m" % length)
        elif length > 1.0:
            notes.append("%.1f m (fine; 0.4 to 0.5 m is the stacked/side-by-side length)"
                         % length)
    if speed is not None and speed < EXPECTED_SPEED:
        status = WARN
        notes.append("link negotiated %d Mb/s, not 200000; a 100G (QSFP28) DAC or a "
                     "marginal cable does this" % speed)
    if known:
        msg = "%s %s: %s" % (mod.get("vendor") or "?", mod.get("part"), known)
    else:
        msg = "%s %s: not on the known-good list (fine if it negotiates 200G)" % (
            mod.get("vendor") or "?", mod.get("part") or "?")
    if notes:
        msg += "; " + "; ".join(notes)
    return status, msg


def parse_ip_addr_json(text):
    """`ip -j -4 addr` -> {ifname: {'operstate', 'mtu', 'addrs': [cidr]}}."""
    out = {}
    try:
        data = json.loads(text or "[]")
    except ValueError:
        return out
    for link in data:
        addrs = []
        for a in link.get("addr_info", []):
            if a.get("family") == "inet":
                addrs.append("%s/%s" % (a["local"], a["prefixlen"]))
        out[link.get("ifname")] = {"operstate": link.get("operstate"),
                                   "mtu": link.get("mtu"),
                                   "flags": link.get("flags", []),
                                   "addrs": addrs}
    return out


def parse_routes_json(text):
    try:
        return json.loads(text or "[]")
    except ValueError:
        return []


def parse_gids(entries):
    """entries: list of (index, gid, type). Return {ip: index} for RoCE v2 IPv4 GIDs."""
    out = {}
    for idx, gid, gtype in entries:
        if not gid or not gtype or "v2" not in gtype.lower():
            continue
        g = gid.replace(":", "").lower()
        if len(g) == 32 and g.startswith("00000000000000000000ffff"):
            ip = ".".join(str(int(g[24 + i:26 + i], 16)) for i in range(0, 8, 2))
            out.setdefault(ip, idx)
    return out


def parse_ping(text):
    """Return (received, transmitted, avg_ms or None)."""
    m = re.search(r"(\d+) packets transmitted, (\d+) (?:packets )?received", text)
    tx, rx = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    m = re.search(r"= [\d.]+/([\d.]+)/", text)
    return rx, tx, (float(m.group(1)) if m else None)


def parse_iperf3_json(text):
    """Return receiver Gb/s from `iperf3 -J` client output, or None."""
    try:
        data = json.loads(text)
        return data["end"]["sum_received"]["bits_per_second"] / 1e9
    except (ValueError, KeyError, TypeError):
        return None


def parse_ib_write_bw(text):
    """Return BW average in Gb/s from `ib_write_bw --report_gbits` client output."""
    val = None
    for line in text.splitlines():
        cols = line.split()
        # "#bytes #iterations BW peak[Gb/sec] BW average[Gb/sec] MsgRate[Mpps]"
        if len(cols) >= 5 and cols[0].isdigit() and cols[1].isdigit():
            try:
                val = float(cols[3])
            except ValueError:
                pass
    return val


def nccl_advice(env, up_pairs, gid_index):
    """Check NCCL env vars. Returns list of (status, message)."""
    res = []
    sock = env.get("NCCL_SOCKET_IFNAME")
    hca = env.get("NCCL_IB_HCA")
    gidx = env.get("NCCL_IB_GID_INDEX")
    up_nets = [n for n, _ in up_pairs]
    up_rdma = [r for _, r in up_pairs if r]
    if sock is None:
        res.append((SKIP, "NCCL_SOCKET_IFNAME not set in this shell (set it where you "
                          "launch NCCL, e.g. NCCL_SOCKET_IFNAME=%s)" %
                    (up_nets[0] if up_nets else "enp1s0f0np0")))
    else:
        names = [s.lstrip("=^") for s in sock.split(",") if s]
        if any(n in up_nets or any(u.startswith(n) for u in up_nets) for n in names):
            res.append((PASS, "NCCL_SOCKET_IFNAME=%s matches an up fabric interface" % sock))
        else:
            res.append((WARN, "NCCL_SOCKET_IFNAME=%s does not match an up fabric "
                              "interface (%s)" % (sock, ",".join(up_nets) or "none")))
    if hca is None:
        res.append((SKIP, "NCCL_IB_HCA not set in this shell (list both halves of the "
                          "cabled port, e.g. NCCL_IB_HCA=%s)" %
                    (",".join(up_rdma) or "rocep1s0f0,roceP2p1s0f0")))
    else:
        listed = [h.split(":")[0].lstrip("=^") for h in hca.split(",") if h]
        missing = [r for r in up_rdma if not any(r.startswith(l) for l in listed)]
        if missing:
            res.append((WARN, "NCCL_IB_HCA=%s leaves out %s; one PCIe half caps a "
                              "cable near 100 Gb/s" % (hca, ",".join(missing))))
        else:
            res.append((PASS, "NCCL_IB_HCA=%s covers every up RDMA half" % hca))
    if gidx is None:
        res.append((SKIP, "NCCL_IB_GID_INDEX not set in this shell%s" %
                    ("" if gid_index is None else
                     " (RoCE v2 IPv4 GID here is index %s)" % gid_index)))
    elif gid_index is not None and str(gidx) != str(gid_index):
        res.append((WARN, "NCCL_IB_GID_INDEX=%s but the RoCE v2 IPv4 GID is index %s"
                    % (gidx, gid_index)))
    else:
        res.append((PASS, "NCCL_IB_GID_INDEX=%s" % gidx))
    return res


# ---------------------------------------------------------------------------
# checks


def discover_ifaces(names_arg):
    if names_arg:
        out = []
        for n in names_arg.split(","):
            port = 1 if re.search(r"f1(np1)?$", n) else 0
            half = "B" if n.startswith("enP2") else "A"
            out.append((n, port, half))
        return out
    present = [t for t in DEFAULT_IFACES if os.path.exists("/sys/class/net/" + t[0])]
    if present:
        return present
    # Fallback: any mlx5 netdev.
    out = []
    for n in sorted(os.listdir("/sys/class/net")):
        drv = os.path.realpath("/sys/class/net/%s/device/driver" % n)
        if drv.endswith("mlx5_core"):
            out.append((n, 1 if n.endswith("1") else 0, "B" if "P2" in n else "A"))
    return out


def check_platform(rep):
    model = (read("/proc/device-tree/model", "") or "").replace("\x00", "")
    gpu, drv = None, None
    if have("nvidia-smi"):
        rc, out, _ = run(["nvidia-smi", "--query-gpu=name,driver_version",
                          "--format=csv,noheader"])
        if rc == 0 and out.strip():
            parts = [p.strip() for p in out.splitlines()[0].split(",")]
            gpu, drv = parts[0], (parts[1] if len(parts) > 1 else None)
    kernel = os.uname().release
    if gpu and "GB10" in gpu:
        rep.add("platform", PASS, "%s, driver %s, kernel %s" % (gpu, drv, kernel),
                gpu=gpu, driver=drv, kernel=kernel, model=model or None)
    else:
        rep.add("platform", WARN, "not detected as a GB10 system (gpu=%s); checks "
                "still run but expectations assume GB10" % gpu,
                gpu=gpu, driver=drv, kernel=kernel, model=model or None)


def check_links(rep, ifaces):
    """Link state and speed per half, judged per physical port."""
    state = {}
    for name, port, half in ifaces:
        info = {"name": name, "port": port, "half": half,
                "carrier": read("/sys/class/net/%s/carrier" % name) == "1",
                "operstate": read("/sys/class/net/%s/operstate" % name),
                "speed": None, "carrier_changes": None}
        sp = read("/sys/class/net/%s/speed" % name)
        if sp and sp.lstrip("-").isdigit() and int(sp) > 0:
            info["speed"] = int(sp)
        cc = read("/sys/class/net/%s/carrier_changes" % name)
        info["carrier_changes"] = int(cc) if cc and cc.isdigit() else None
        dc = read("/sys/class/net/%s/carrier_down_count" % name)
        info["link_drops"] = int(dc) if dc and dc.isdigit() else (
            info["carrier_changes"] // 2 if info["carrier_changes"] else None)
        if have("ethtool"):
            rc, out, _ = run(["ethtool", name])
            e = parse_ethtool(out)
            if e["speed"]:
                info["speed"] = e["speed"]
            if e["link"] is not None:
                info["carrier"] = e["link"]
            info["media"] = e["port"]
            info["lanes"] = e["lanes"]
        rc, out, _ = run(["ethtool", "-i", name]) if have("ethtool") else (1, "", "")
        m = re.search(r"firmware-version:\s*(\S+)", out)
        info["firmware"] = m.group(1) if m else None
        state[name] = info

    for port in (0, 1):
        halves = [state[n] for n, p, _ in ifaces if p == port and n in state]
        if not halves:
            continue
        up = [h for h in halves if h["carrier"]]
        label = "port%d" % port
        names = ",".join(h["name"] for h in halves)
        detail = {"halves": halves}
        if not up:
            rep.add("link." + label, SKIP, "Port %d: no carrier on %s (unused port, or "
                    "no cable)" % (port, names), **detail)
            continue
        if len(up) < len(halves):
            down = ",".join(h["name"] for h in halves if not h["carrier"])
            rep.add("link." + label, FAIL, "Port %d: %s up but %s down; both halves ride "
                    "the same cable, so this is a driver or config fault, not the wire"
                    % (port, ",".join(h["name"] for h in up), down), **detail)
            continue
        speeds = sorted(set(h["speed"] for h in up))
        if speeds == [EXPECTED_SPEED]:
            rep.add("link." + label, PASS, "Port %d: %s up at 200G (%s)" %
                    (port, names, up[0].get("media") or "media unknown"), **detail)
        else:
            rep.add("link." + label, WARN, "Port %d: up at %s Mb/s, expected 200000; "
                    "100G usually means a 100G cable, a switch port not forced to "
                    "200G-baseCR4, or a marginal cage" % (port, "/".join(
                        str(s) for s in speeds)), **detail)
        drops = max((h.get("link_drops") or 0) for h in up)
        if drops > 3:
            rep.add("link.%s.drops" % label, WARN, "Port %d: link dropped %d times since "
                    "boot (a healthy link shows 0); check `journalctl -k | grep %s` for "
                    "when, then reseat or swap the cable if it keeps happening" %
                    (port, drops, up[0]["name"]), **detail)
    if not any(i["carrier"] for i in state.values()):
        rep.add("link", FAIL, "no ConnectX-7 fabric interface has carrier")
    return state


def check_rdma_map(rep, ifaces, links):
    mapping = {}
    if have("ibdev2netdev"):
        rc, out, _ = run(["ibdev2netdev"])
        mapping = parse_ibdev2netdev(out)
    if not mapping and os.path.isdir("/sys/class/infiniband"):
        for dev in os.listdir("/sys/class/infiniband"):
            netdir = "/sys/class/infiniband/%s/device/net" % dev
            if os.path.isdir(netdir):
                for n in os.listdir(netdir):
                    st = read("/sys/class/infiniband/%s/ports/1/state" % dev, "")
                    mapping[n] = (dev, "Up" if "ACTIVE" in st else "Down")
    if not mapping:
        rep.add("rdma.map", FAIL, "no RDMA devices found (is the mlx5_ib module loaded?)")
        return {}
    bad = []
    for name, info in links.items():
        if not info["carrier"]:
            continue
        if name not in mapping:
            bad.append("%s has no RDMA device" % name)
            continue
        dev = mapping[name][0]
        st = read("/sys/class/infiniband/%s/ports/1/state" % dev, "")
        if "ACTIVE" not in st:
            bad.append("%s (%s) state %s" % (dev, name, st or "?"))
    up_map = {n: m[0] for n, m in mapping.items() if links.get(n, {}).get("carrier")}
    if bad:
        rep.add("rdma.map", FAIL, "; ".join(bad), mapping=mapping)
    else:
        active = ", ".join("%s->%s" % (v, k) for k, v in sorted(up_map.items()))
        rep.add("rdma.map", PASS, "RDMA devices active: %s" % (active or "none"),
                mapping=mapping)
    return mapping


def check_counters(rep, mapping, links):
    watch = ["packet_seq_err", "out_of_sequence", "local_ack_timeout_err",
             "np_cnp_sent", "rp_cnp_handled"]
    found = {}
    for name, (dev, _) in mapping.items():
        if not links.get(name, {}).get("carrier"):
            continue
        base = "/sys/class/infiniband/%s/ports/1/hw_counters/" % dev
        vals = {}
        for c in watch:
            v = read(base + c)
            if v is not None and v.isdigit():
                vals[c] = int(v)
        if vals:
            found[dev] = vals
    if not found:
        rep.add("rdma.counters", SKIP, "no RoCE hw_counters readable")
        return
    seq = sum(v.get("packet_seq_err", 0) + v.get("out_of_sequence", 0)
              for v in found.values())
    if seq > 1000:
        rep.add("rdma.counters", WARN, "%d packet_seq_err/out_of_sequence since boot; "
                "if it grows during a bandwidth test, the path is dropping packets (on a "
                "MikroTik this is the second-bridge CPU-forwarding trap)" % seq,
                counters=found)
    else:
        rep.add("rdma.counters", PASS, "RoCE error counters low (%d sequence errors "
                "since boot)" % seq, counters=found)


def sudo_prefix():
    if os.geteuid() == 0:
        return []
    if have("sudo"):
        rc, _, _ = run(["sudo", "-n", "true"], timeout=5)
        if rc == 0:
            return ["sudo", "-n"]
    return None


def check_modules(rep, ifaces, links):
    if not have("ethtool"):
        rep.add("cable", SKIP, "ethtool not installed")
        return
    pre = sudo_prefix()
    for port in (0, 1):
        names = [n for n, p, h in ifaces if p == port and h == "A"] or \
                [n for n, p, _ in ifaces if p == port]
        if not names:
            continue
        name = names[0]
        if not links.get(name, {}).get("carrier"):
            continue
        rc, out, err = run(["ethtool", "-m", name])
        if rc != 0 and pre:
            rc, out, err = run(pre + ["ethtool", "-m", name])
        if rc != 0 or not out.strip():
            why = "needs root; re-run with sudo" if pre is None or \
                "not permitted" in err.lower() else err.strip()[:120]
            rep.add("cable.port%d" % port, SKIP, "Port %d module EEPROM not readable (%s)"
                    % (port, why))
            continue
        mod = parse_module_eeprom(out)
        status, msg = classify_cable(mod, links[name].get("speed"))
        mod.pop("raw_keys", None)
        rep.add("cable.port%d" % port, status, "Port %d cable: %s" % (port, msg),
                module=mod)


def get_addrs():
    rc, out, _ = run(["ip", "-j", "-4", "addr"])
    return parse_ip_addr_json(out) if rc == 0 else {}


def get_routes():
    rc, out, _ = run(["ip", "-j", "-4", "route", "show", "table", "main"])
    return parse_routes_json(out) if rc == 0 else []


def check_mtu(rep, links, addrs):
    up = {n: addrs.get(n, {}).get("mtu") or int(read("/sys/class/net/%s/mtu" % n, "0"))
          for n, i in links.items() if i["carrier"]}
    if not up:
        rep.add("mtu", SKIP, "no fabric interface up")
        return None
    vals = sorted(set(up.values()))
    desc = ", ".join("%s=%s" % kv for kv in sorted(up.items()))
    if len(vals) > 1:
        rep.add("mtu", FAIL, "MTU differs across fabric interfaces (%s)" % desc, mtu=up)
    elif vals[0] >= 9000:
        rep.add("mtu", PASS, "MTU %d on every up fabric interface" % vals[0], mtu=up)
    else:
        rep.add("mtu", WARN, "MTU %d; set 9000 on every node (and l2mtu 9216 on a switch)"
                % vals[0], mtu=up)
    return max(vals)


def analyze_ip(links, addrs, routes):
    """Pure analysis of addressing and routes. Returns list of (status, check, msg)."""
    res = []
    fabric = set(links)
    nets = {}
    for n, info in links.items():
        a = addrs.get(n, {}).get("addrs", [])
        if info["carrier"]:
            if not a:
                res.append((WARN, "ip.%s" % n, "%s is up with no IPv4 address" % n))
            elif len(a) > 1:
                res.append((WARN, "ip.%s" % n, "%s has %d IPv4 addresses (%s); keep one "
                            "per half" % (n, len(a), ", ".join(a))))
            else:
                res.append((PASS, "ip.%s" % n, "%s %s" % (n, a[0])))
            for cidr in a:
                nets.setdefault(str(ipaddress.ip_interface(cidr).network), []).append(n)
        elif a:
            res.append((WARN, "ip.%s" % n, "%s has no carrier but still holds %s; a "
                        "stale address can capture routes" % (n, ", ".join(a))))
    shared = {k: v for k, v in nets.items() if len(set(v)) > 1}
    if shared:
        res.append((WARN, "ip.subnets", "halves share a subnet (%s); use one subnet per "
                    "PCIe half so the kernel picks the right interface" % "; ".join(
                        "%s on %s" % (k, ",".join(v)) for k, v in shared.items())))
    elif nets:
        res.append((PASS, "ip.subnets", "one subnet per half (%s)" % ", ".join(sorted(nets))))
    fabric_nets = [ipaddress.ip_network(k) for k in nets]
    for r in routes:
        dev = r.get("dev")
        dst = r.get("dst")
        if dst == "default":
            if dev in fabric:
                res.append((FAIL, "ip.default", "default route via fabric interface %s; "
                            "internet and DNS break when a QSFP cable is plugged in" % dev))
            continue
        linkdown = "linkdown" in (r.get("flags") or [])
        no_carrier = dev in links and not links[dev]["carrier"]
        if not (linkdown or no_carrier):
            continue
        try:
            dnet = ipaddress.ip_network(dst if "/" in dst else dst + "/32", strict=False)
        except ValueError:
            continue
        overlaps = any(dnet.overlaps(n) for n in fabric_nets)
        if dev in fabric or overlaps or (dev or "").startswith("bond"):
            res.append((FAIL, "ip.stale", "route %s points at %s, which has no carrier; "
                        "a stale bond or profile can capture fabric traffic" % (dst, dev)))
    if not any(c == "ip.default" for _, c, _ in res):
        res.append((PASS, "ip.default", "no default route via a fabric interface"))
    return res


def check_ip(rep, links, addrs, routes):
    for status, check, msg in analyze_ip(links, addrs, routes):
        rep.add(check, status, msg)


def fabric_networks(links, addrs):
    out = []
    for n, info in links.items():
        if not info["carrier"]:
            continue
        for cidr in addrs.get(n, {}).get("addrs", []):
            out.append((n, ipaddress.ip_interface(cidr)))
    return out


def discover_peers(links, addrs):
    """Peers from the neighbour table, limited to the fabric subnets (read-only)."""
    nets = fabric_networks(links, addrs)
    local = set(str(i.ip) for _, i in nets)
    rc, out, _ = run(["ip", "-j", "-4", "neigh"])
    peers = []
    try:
        for e in json.loads(out or "[]"):
            ip = e.get("dst")
            if not ip or ip in local or "FAILED" in (e.get("state") or []):
                continue
            for n, iface in nets:
                if e.get("dev") == n and ipaddress.ip_address(ip) in iface.network:
                    peers.append(ip)
    except ValueError:
        pass
    return sorted(set(peers), key=lambda s: tuple(int(x) for x in s.split(".")))


def check_peers(rep, peers, links, addrs, mtu):
    if not peers:
        rep.add("peers", SKIP, "no peers given (use --peer IP, or --discover)")
        return
    nets = fabric_networks(links, addrs)
    size = (mtu or 1500) - 28
    for peer in peers:
        try:
            pip = ipaddress.ip_address(peer)
        except ValueError:
            rep.add("peer.%s" % peer, FAIL, "not an IPv4 address")
            continue
        via = [n for n, i in nets if pip in i.network]
        if not via:
            rc, out, _ = run(["ip", "-j", "route", "get", peer])
            dev = None
            try:
                dev = json.loads(out)[0].get("dev")
            except (ValueError, IndexError, TypeError):
                pass
            rep.add("peer.%s.route" % peer, WARN, "%s is not on a fabric subnet; it would "
                    "go via %s" % (peer, dev or "?"))
        rc, out, _ = run(["ping", "-c", "3", "-i", "0.2", "-W", "1", "-q", peer], timeout=10)
        rx, tx, avg = parse_ping(out)
        if rx == 0:
            rep.add("peer.%s" % peer, FAIL, "%s unreachable (0/%d replies)" % (peer, tx))
            continue
        jrc, jout, _ = run(["ping", "-c", "3", "-i", "0.2", "-W", "1", "-q", "-M", "do",
                            "-s", str(size), peer], timeout=10)
        jrx, jtx, _ = parse_ping(jout)
        if size >= 8972 and jrx == 0:
            rep.add("peer.%s" % peer, FAIL, "%s answers small pings (%.3f ms) but not "
                    "%d-byte do-not-fragment pings: MTU mismatch on the far end or the "
                    "switch (l2mtu)" % (peer, avg or 0, size), via=via)
        else:
            rep.add("peer.%s" % peer, PASS, "%s reachable via %s, %.3f ms, %d-byte DF ping "
                    "ok" % (peer, ",".join(via) or "non-fabric route", avg or 0, size),
                    via=via, rtt_ms=avg)


def check_roce(rep, mapping, links, addrs, env):
    gid_index = None
    up_pairs = []
    rows = []
    for name, info in links.items():
        if not info["carrier"]:
            continue
        dev = mapping.get(name, (None,))[0]
        up_pairs.append((name, dev))
        if not dev:
            continue
        base = "/sys/class/infiniband/%s/ports/1/" % dev
        entries = []
        try:
            idxs = sorted(int(i) for i in os.listdir(base + "gids"))
        except OSError:
            idxs = []
        for i in idxs[:32]:
            entries.append((i, read(base + "gids/%d" % i), read(base + "gid_attrs/types/%d" % i)))
        v2 = parse_gids(entries)
        mine = [str(ipaddress.ip_interface(c).ip) for c in addrs.get(name, {}).get("addrs", [])]
        hit = [(ip, v2[ip]) for ip in mine if ip in v2]
        if hit:
            rows.append("%s idx %d" % (dev, hit[0][1]))
            gid_index = hit[0][1] if gid_index is None else gid_index
        else:
            rep.add("roce.gid.%s" % dev, FAIL if mine else WARN, "%s has no RoCE v2 GID "
                    "for %s" % (dev, ",".join(mine) or "(no IPv4 address)"))
    if rows:
        rep.add("roce.gid", PASS, "RoCE v2 IPv4 GIDs: %s" % ", ".join(rows),
                gid_index=gid_index)
    for status, msg in nccl_advice(env, up_pairs, gid_index):
        rep.add("nccl.env", status, msg)
    return gid_index


def busy_state():
    """Read-only look at whether this node is doing real work."""
    reasons, containers = [], []
    if have("nvidia-smi"):
        rc, out, _ = run(["nvidia-smi", "--query-compute-apps=pid,process_name",
                          "--format=csv,noheader"])
        procs = [l for l in out.splitlines() if l.strip()] if rc == 0 else []
        if procs:
            reasons.append("%d GPU compute process(es)" % len(procs))
        rc, out, _ = run(["nvidia-smi", "--query-gpu=utilization.gpu",
                          "--format=csv,noheader,nounits"])
        try:
            util = int(out.split()[0]) if rc == 0 and out.strip() else 0
        except ValueError:
            util = 0
        if util >= 10:
            reasons.append("GPU %d%% busy" % util)
    try:
        load1 = os.getloadavg()[0]
        if load1 > (os.cpu_count() or 1) * 0.5:
            reasons.append("load average %.1f" % load1)
    except OSError:
        pass
    if have("docker"):
        rc, out, _ = run(["docker", "ps", "--format", "{{.Image}}"], timeout=10)
        if rc == 0:
            containers = [l for l in out.splitlines() if l.strip()]
    return reasons, containers


def check_bw(rep, args, peers, mapping, links, gid_index):
    if not args.bw:
        return
    if not peers:
        rep.add("bw", SKIP, "--bw needs --peer (the node running the server)")
        return
    reasons, containers = busy_state()
    if reasons and not args.force:
        rep.add("bw", SKIP, "node busy (%s); not running bandwidth tests (use --force)"
                % "; ".join(reasons), containers=containers)
        return
    peer = peers[0]
    if "iperf3" in args.bw_tools:
        if not have("iperf3"):
            rep.add("bw.iperf3", SKIP, "iperf3 not installed")
        else:
            rc, out, err = run(["iperf3", "-c", peer, "-t", str(args.bw_seconds), "-P", "4",
                                "-J"], timeout=args.bw_seconds + 20)
            gbps = parse_iperf3_json(out)
            if gbps is None:
                rep.add("bw.iperf3", SKIP, "no result from %s (start `iperf3 -s -1` there "
                        "first)" % peer)
            else:
                st = PASS if gbps >= 40 else WARN
                rep.add("bw.iperf3", st, "iperf3 4 streams to %s: %.1f Gb/s (TCP on the "
                        "ARM cores; RDMA is the authority for link health)" % (peer, gbps),
                        gbps=round(gbps, 2))
    if "ib" in args.bw_tools:
        if not have("ib_write_bw"):
            rep.add("bw.ib_write_bw", SKIP, "ib_write_bw (perftest) not installed")
            return
        dev = args.bw_dev
        if not dev:
            # Prefer the half whose subnet holds the peer, then the first up half.
            pip = ipaddress.ip_address(peer)
            addrs = get_addrs()
            same = [mapping[n][0] for n, i in fabric_networks(links, addrs)
                    if pip in i.network and n in mapping]
            up = [mapping[n][0] for n in links if links[n]["carrier"] and n in mapping]
            dev = (same or up or [None])[0]
        if not dev:
            rep.add("bw.ib_write_bw", SKIP, "no active RDMA device")
            return
        cmd = ["ib_write_bw", "-d", dev, "--report_gbits", "-D", str(args.bw_seconds),
               "-q", "4", "-s", "1048576"]
        if gid_index is not None:
            cmd += ["-x", str(gid_index)]
        rc, out, err = run(cmd + [peer], timeout=args.bw_seconds + 30)
        gbps = parse_ib_write_bw(out)
        if gbps is None:
            rep.add("bw.ib_write_bw", SKIP, "no result from %s (start `ib_write_bw -d <dev> "
                    "-x <gid> --report_gbits -D %d -q 4 -s 1048576` there first)"
                    % (peer, args.bw_seconds), stderr=err.strip()[-300:])
        else:
            if gbps >= 95:
                st, note = PASS, "healthy for one PCIe half (expect about 109 to 112)"
            elif gbps >= 50:
                st, note = WARN, "below the ~100 Gb/s one half should give"
            else:
                st, note = FAIL, ("far below line rate: firmware or driver throttle, a "
                                  "CPU-forwarded switch path, or no power drain after "
                                  "first cabling")
            rep.add("bw.ib_write_bw", st, "ib_write_bw %s to %s: %.2f Gb/s, %s" %
                    (dev, peer, gbps, note), gbps=round(gbps, 2), device=dev)


# ---------------------------------------------------------------------------
# output


def render_text(rep, color):
    col = {PASS: "\033[32m", WARN: "\033[33m", FAIL: "\033[31m", SKIP: "\033[90m"}
    lines = []
    for r in rep.results:
        tag = r["status"]
        if color:
            tag = col[tag] + tag + "\033[0m"
        lines.append("[%s] %-18s %s" % (tag, r["check"], r["message"]))
    c = rep.counts()
    lines.append("")
    lines.append("Summary: %d pass, %d warn, %d fail, %d skip -> %s" % (
        c[PASS], c[WARN], c[FAIL], c[SKIP], rep.worst()))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gb10-cluster-check", description=(
        "Read-only health check for the ConnectX-7 cluster fabric on NVIDIA GB10 "
        "workstations (DGX Spark and OEM GB10 systems)."))
    ap.add_argument("--peer", action="append", default=[], metavar="IP",
                    help="fabric IP of another node to test (repeatable)")
    ap.add_argument("--discover", action="store_true",
                    help="also test peers found in the neighbour table on the fabric subnets")
    ap.add_argument("--ifaces", help="comma-separated fabric interfaces "
                    "(default: the four GB10 ConnectX-7 halves)")
    ap.add_argument("--bw", action="store_true", help="run a short bandwidth test against "
                    "the first --peer (you start the server there yourself)")
    ap.add_argument("--bw-tools", default="ib,iperf3",
                    help="which bandwidth tools to use: ib, iperf3 (default both)")
    ap.add_argument("--bw-dev", help="RDMA device for ib_write_bw (default: first active)")
    ap.add_argument("--bw-seconds", type=int, default=5)
    ap.add_argument("--force", action="store_true",
                    help="run --bw even if this node looks busy")
    ap.add_argument("--json", action="store_true", help="print JSON instead of text")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--version", action="version", version="%(prog)s " + VERSION)
    args = ap.parse_args(argv)
    args.bw_tools = [t.strip() for t in args.bw_tools.split(",") if t.strip()]

    rep = Report()
    ifaces = discover_ifaces(args.ifaces)
    if not ifaces:
        rep.add("ifaces", FAIL, "no ConnectX-7 interfaces found")
    else:
        check_platform(rep)
        links = check_links(rep, ifaces)
        mapping = check_rdma_map(rep, ifaces, links)
        check_counters(rep, mapping, links)
        check_modules(rep, ifaces, links)
        addrs, routes = get_addrs(), get_routes()
        mtu = check_mtu(rep, links, addrs)
        check_ip(rep, links, addrs, routes)
        gid_index = check_roce(rep, mapping, links, addrs, dict(os.environ))
        peers = list(args.peer)
        if args.discover:
            peers += [p for p in discover_peers(links, addrs) if p not in peers]
        check_peers(rep, peers, links, addrs, mtu)
        check_bw(rep, args, args.peer or peers, mapping, links, gid_index)

    if args.json:
        c = rep.counts()
        print(json.dumps({"tool": "gb10-cluster-check", "version": VERSION,
                          "summary": {"pass": c[PASS], "warn": c[WARN], "fail": c[FAIL],
                                      "skip": c[SKIP], "result": rep.worst()},
                          "results": rep.results}, indent=2, default=str))
    else:
        print(render_text(rep, sys.stdout.isatty() and not args.no_color))
    return 1 if rep.worst() == FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
