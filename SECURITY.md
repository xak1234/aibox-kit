# Security

aibox is a single-owner tool for a trusted LAN: one token opens everything, and whoever holds it can start
processes, run code in sandboxes and download files on the box. Keep the ports off the internet (use a VPN),
keep the token private, and rotate it with `sudo aibox token --rotate` if it leaks.

The design (unprivileged service account, the `aibox-priv` root helper, origin checks, network-locked
terminal.. container, sandboxed coding commands) is described in the README's Security section.

To report a vulnerability, open a GitHub security advisory on this repository (Security tab → "Report a
vulnerability") rather than a public issue. Include the version (`git log -1`), what an attacker needs
(token? LAN access? a local account?) and the steps to reproduce.
