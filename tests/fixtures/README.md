Test fixtures.

- `ethtool_200g.txt`, `ethtool_down.txt`, `ibdev2netdev.txt`, `ib_write_bw.txt`:
  real output captured on GB10 systems (ib_write_bw GIDs, keys and addresses
  replaced with documentation placeholders).
- `ethtool_m_*_synthetic.txt`: hand-written in the layout ethtool prints for
  CMIS and SFF-8636 modules, because reading the module EEPROM needs root.
  Serial numbers are placeholders.
