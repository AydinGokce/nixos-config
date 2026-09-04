# Make Tailscale and the Mullvad app coexist.
#
# Problem: Mullvad's firewall (`inet mullvad` nftables table) drops tailnet
# traffic whenever the tunnel is up or blocking — the CGNAT range
# 100.64.0.0/10 is not in its allow-LAN set and inbound tailscale0 traffic
# has no accept rule — so the machine is unreachable over Tailscale while
# on the VPN.
#
# Fix: stamp Tailscale-related packets with Mullvad's own split-tunneling
# marks, making its firewall treat them as excluded:
#   ct mark 0x00000f41   — Mullvad's input/output/forward chains accept
#                          flows carrying this conntrack mark, in every
#                          tunnel state including blocked/lockdown.
#   meta mark 0x6d6f6c65 — skips Mullvad's "not fwmark 0x6d6f6c65 lookup
#                          <tunnel table>" ip rule, so the packet is
#                          re-routed (route hook) out the physical
#                          interface instead of into the tunnel, where
#                          Mullvad's NAT chain would drop it.
#
# Constants verified against the mullvadvpn-app source (talpid-core
# split_tunnel MARK = 0xf41; mullvad-types TUNNEL_FWMARK = 0x6d6f6c65,
# ASCII "mole"); stable across Mullvad 2021.6 -> 2026.4. Approach per
# github.com/r3nor/mullvad-tailscale.
#
# Scope: ONLY tailnet-addressed traffic (in/out of the tailscale0 TUN)
# gets the exclusion marks. tailscaled's own transport — control plane,
# DERP, WireGuard UDP — deliberately keeps riding INSIDE the Mullvad
# tunnel: Mullvad's routing rules outrank Tailscale's fwmark rule, so
# 0x80000-marked sockets route into the tunnel and Mullvad accepts its own
# tunnel egress. Do NOT add a rule bypassing tailscaled's sockets
# (meta mark 0x80000): on the office network that pushes DERP/STUN onto
# the filtered ISP path, which blocks UDP and kills DERP connections —
# observed as "netcheck: UDP is blocked" + DERP Recv errors on gc-hitl-1.
# Consequence: Tailscale needs the tunnel up (or Mullvad fully
# disconnected, not blocking) to relay; the office LAN (allow-LAN) remains
# the fallback admin path during Mullvad outages.
#
# Deliberately a standalone unit rather than networking.nftables: with
# stateVersion < 23.11 (or the legacy ruleset option) that module defaults
# to `flush ruleset` on every reload, which would delete Mullvad's
# kill-switch table (a full leak) and Tailscale's tables. This unit only
# ever adds/deletes its own table.
#
# Caveats:
# - After a mullvad package bump, verify the daemon still installs its
#   accept rules:  sudo nft list table inet mullvad | grep -c 0x00000f41
#   (expect >= 3). Mullvad 2026.x skips them when its split-tunnel cgroup
#   init fails — then this table marks packets nothing accepts (fails
#   closed: tailnet unreachable again, no leak).
# - Never advertise this machine as a Tailscale exit node or subnet router
#   while Mullvad is up: exit-node routes + the mark rewrite loop traffic.
# - After nixos-rebuild bumps mullvad, restart mullvad-daemon before
#   trusting `mullvad` CLI output (old daemon + new CLI speak different
#   gRPC).
{ pkgs, ... }:

let
  rules = pkgs.writeText "mullvad-tailscale-exclude.nft" ''
    add table inet mullvad-ts
    delete table inet mullvad-ts

    table inet mullvad-ts {
      chain output {
        # `type route`: routing is re-evaluated after the mark changes.
        # -100 = after Mullvad's mangle chain (-150), before its filter (0).
        type route hook output priority -100; policy accept;

        # Locally-originated traffic into the tailnet: sshd replies,
        # MagicDNS (100.100.100.100 is inside the CGNAT range), tailnet
        # IPv6 ULA.
        oifname "tailscale0" ct mark set 0x00000f41 meta mark set 0x6d6f6c65
        ip daddr 100.64.0.0/10 ct mark set 0x00000f41 meta mark set 0x6d6f6c65
        ip6 daddr fd7a:115c:a1e0::/48 ct mark set 0x00000f41 meta mark set 0x6d6f6c65
      }

      chain prerouting {
        # After conntrack (-200) and Mullvad's prerouting (-199), before
        # its input filter (0), which accepts flows with the ct mark.
        type filter hook prerouting priority -100; policy accept;

        # Decrypted tailnet traffic arriving on the Tailscale interface
        # (inbound SSH etc.). Interface match, not saddr: CGNAT source
        # addresses could be spoofed from the LAN, the TUN cannot.
        iifname "tailscale0" ct mark set 0x00000f41 meta mark set 0x6d6f6c65
      }
    }
  '';
in
{
  systemd.services.mullvad-tailscale-exclusion = {
    description = "nftables marks letting Tailscale bypass the Mullvad firewall";
    wantedBy = [ "multi-user.target" ];
    # Not ordered against mullvad-daemon/tailscaled on purpose: the table
    # is independent of both and harmless while either is down.
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = "${pkgs.nftables}/bin/nft -f ${rules}";
      ExecStop = "${pkgs.nftables}/bin/nft delete table inet mullvad-ts";
    };
  };
}
