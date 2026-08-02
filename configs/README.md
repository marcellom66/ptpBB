# Profile notes

The built-in profiles are safe laboratory starting points, not declarations of
formal IEEE/ITU compliance. A certified profile also constrains hardware,
topology, packet rates, BMCA behavior, traceability and performance testing.

- `default`: IEEE 1588 default-profile parameters, UDP/IPv4, E2E.
- `g8275.1`: full-timing-support laboratory preset, L2, P2P, domain 24.
- `gptp`: 802.1AS-like preset with transportSpecific 1, L2 and P2P.
- `power`: C37.238-like laboratory preset, L2 and P2P.

Inspect the exact generated configuration before connecting it to a production
timing network:

```sh
beagleptp generate-config grandmaster --profile g8275.1
```
