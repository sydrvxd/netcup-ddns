#!/usr/bin/env python3
"""
Netcup DNS Manager — Combined DDNS Updater + Record Cleanup
Supports multiple domains in a single container.

Environment Variables:
  DOMAINS             Comma-separated list of domain configs:
                      domain:hosts:credentials_prefix
                      e.g. "sydrv.de:*,@:SYDRV,fm-p.de:*,@:FMP"

  Per-domain credentials (prefix from DOMAINS config):
    {PREFIX}_CUSTOMER_ID    Netcup customer number
    {PREFIX}_API_KEY        Netcup CCP API key
    {PREFIX}_API_PASSWORD   Netcup CCP API password

  Or global credentials (used when no prefix match):
    NETCUP_CUSTOMER_ID
    NETCUP_API_KEY
    NETCUP_API_PASSWORD

  DDNS_INTERVAL       DDNS check interval in seconds (default: 300)
  CLEANUP_INTERVAL    Cleanup interval in seconds (default: 86400 = 24h)
  CLEANUP_ENABLED     Enable cleanup (default: true)
  CLEANUP_MAX_AGE     Max age for duplicate/stale records in hours (default: 24)
  LOG_LEVEL           Logging level (default: INFO)
"""

import os
import json
import time
import socket
import logging
import threading
from datetime import datetime, timezone

import requests

# ── Logging ──────────────────────────────────────────────────────────────────

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("netcup-dns")

# ── Config ───────────────────────────────────────────────────────────────────

API_URL = "https://ccp.netcup.net/run/webservice/servers/endpoint.php?JSON"
DDNS_INTERVAL = int(os.environ.get("DDNS_INTERVAL", "300"))
CLEANUP_INTERVAL = int(os.environ.get("CLEANUP_INTERVAL", "86400"))
CLEANUP_ENABLED = os.environ.get("CLEANUP_ENABLED", "true").lower() in ("true", "1", "yes")

WAN_SERVICES = [
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
    "https://checkip.amazonaws.com",
]


# ── Domain Config Parser ─────────────────────────────────────────────────────

class DomainConfig:
    def __init__(self, domain: str, hosts: list[str], customer_id: str, api_key: str, api_password: str):
        self.domain = domain
        self.hosts = hosts
        self.customer_id = customer_id
        self.api_key = api_key
        self.api_password = api_password
        self.session_id = None

    def __repr__(self):
        return f"DomainConfig({self.domain}, hosts={self.hosts})"


def parse_domains() -> list[DomainConfig]:
    """Parse domain configs from environment."""
    domains_str = os.environ.get("DOMAINS", "")

    if domains_str:
        configs = []
        for entry in domains_str.split(";"):
            entry = entry.strip()
            if not entry:
                continue
            parts = entry.split(":")
            if len(parts) < 2:
                log.error(f"Invalid domain config: {entry}")
                continue

            domain = parts[0]
            hosts = parts[1].split(",") if len(parts) > 1 else ["@"]
            prefix = parts[2] if len(parts) > 2 else ""

            if prefix:
                cid = os.environ.get(f"{prefix}_CUSTOMER_ID", os.environ.get("NETCUP_CUSTOMER_ID", ""))
                key = os.environ.get(f"{prefix}_API_KEY", os.environ.get("NETCUP_API_KEY", ""))
                pwd = os.environ.get(f"{prefix}_API_PASSWORD", os.environ.get("NETCUP_API_PASSWORD", ""))
            else:
                cid = os.environ.get("NETCUP_CUSTOMER_ID", "")
                key = os.environ.get("NETCUP_API_KEY", "")
                pwd = os.environ.get("NETCUP_API_PASSWORD", "")

            if not all([cid, key, pwd]):
                log.error(f"Missing credentials for {domain} (prefix: {prefix or 'global'})")
                continue

            configs.append(DomainConfig(domain, hosts, cid, key, pwd))
        return configs

    # Fallback: single domain from legacy env vars
    domain = os.environ.get("NETCUP_DOMAIN_NAME") or os.environ.get("NETCUP_DOMAIN")
    hosts = (os.environ.get("ZONE_HOSTS") or os.environ.get("NETCUP_HOSTS", "*,@")).split(",")
    cid = os.environ.get("NETCUP_CUSTOMER_ID") or os.environ.get("NETCUP_CUSTOMER_NR", "")
    key = os.environ.get("NETCUP_API_KEY", "")
    pwd = os.environ.get("NETCUP_API_PASSWORD", "")

    if domain and all([cid, key, pwd]):
        return [DomainConfig(domain, hosts, cid, key, pwd)]

    log.error("No valid domain configuration found!")
    return []


# ── Netcup API ───────────────────────────────────────────────────────────────

def api_call(cfg: DomainConfig, action: str, extra_params: dict = None, use_session: bool = True) -> dict | None:
    """Call Netcup CCP API."""
    params = {
        "customernumber": cfg.customer_id,
        "apikey": cfg.api_key,
    }
    if use_session and cfg.session_id:
        params["apisessionid"] = cfg.session_id
    elif not use_session:
        params["apipassword"] = cfg.api_password

    if extra_params:
        params.update(extra_params)

    try:
        resp = requests.post(API_URL, json={"action": action, "param": params}, timeout=30)
        data = resp.json()

        status = data.get("status", "")
        code = data.get("statuscode", 0)
        if isinstance(code, str):
            code = int(code) if code.isdigit() else 0

        # Session expired → re-login and retry once
        if status != "success" and code == 4001 and use_session:
            log.warning(f"[{cfg.domain}] Session expired, re-authenticating...")
            if login(cfg):
                params["apisessionid"] = cfg.session_id
                resp = requests.post(API_URL, json={"action": action, "param": params}, timeout=30)
                data = resp.json()
                if data.get("status") != "success":
                    log.error(f"[{cfg.domain}] API {action} failed after retry: {data.get('longmessage', '')}")
                    return None
                return data
            return None

        if status != "success":
            log.error(f"[{cfg.domain}] API {action} failed: {data.get('longmessage', data)}")
            return None

        return data
    except Exception as e:
        log.error(f"[{cfg.domain}] API {action} error: {e}")
        return None


def login(cfg: DomainConfig) -> bool:
    """Login to Netcup API."""
    result = api_call(cfg, "login", {"apipassword": cfg.api_password}, use_session=False)
    if result:
        cfg.session_id = result.get("responsedata", {}).get("apisessionid")
        log.debug(f"[{cfg.domain}] Logged in")
        return True
    return False


def logout(cfg: DomainConfig):
    """Logout from Netcup API."""
    if cfg.session_id:
        api_call(cfg, "logout")
        cfg.session_id = None


def get_dns_records(cfg: DomainConfig) -> list[dict]:
    """Fetch DNS records for domain."""
    result = api_call(cfg, "infoDnsRecords", {"domainname": cfg.domain})
    if not result:
        return []

    resp_data = result.get("responsedata", {})
    # Handle both API response shapes
    records = resp_data.get("dnsrecords", [])
    if not records:
        recordset = resp_data.get("dnsrecordset", {})
        records = recordset.get("dnsrecords", [])
    return records


def update_dns_records(cfg: DomainConfig, records: list[dict]) -> bool:
    """Update DNS records."""
    result = api_call(cfg, "updateDnsRecords", {
        "domainname": cfg.domain,
        "dnsrecordset": {"dnsrecords": records},
    })
    return result is not None


# ── WAN IP ───────────────────────────────────────────────────────────────────

def get_wan_ip() -> str | None:
    """Get current WAN IPv4 address."""
    for service in WAN_SERVICES:
        try:
            resp = requests.get(service, timeout=10)
            if resp.status_code == 200:
                ip = resp.text.strip()
                if ip and "." in ip and len(ip) < 20:
                    return ip
        except Exception:
            continue
    return None


def resolve_domain(domain: str) -> str | None:
    """Resolve domain A record."""
    try:
        addrs = socket.getaddrinfo(domain, None, socket.AF_INET)
        if addrs:
            return addrs[0][4][0]
    except socket.gaierror:
        pass
    return None


# ── DDNS Updater ─────────────────────────────────────────────────────────────

last_ips: dict[str, str] = {}

def ddns_update(configs: list[DomainConfig]):
    """Check and update DNS for all domains."""
    wan_ip = get_wan_ip()
    if not wan_ip:
        log.error("Could not determine WAN IP")
        return

    for cfg in configs:
        domain_key = cfg.domain
        dns_ip = resolve_domain(cfg.domain)

        if wan_ip == last_ips.get(domain_key) and wan_ip == dns_ip:
            log.debug(f"[{cfg.domain}] IP unchanged: {wan_ip}")
            continue

        if wan_ip == dns_ip:
            last_ips[domain_key] = wan_ip
            continue

        log.info(f"[{cfg.domain}] IP mismatch: WAN={wan_ip}, DNS={dns_ip} → updating")

        if not login(cfg):
            continue

        try:
            records = get_dns_records(cfg)
            if not records:
                log.error(f"[{cfg.domain}] Could not fetch DNS records")
                continue

            updates = []
            for record in records:
                if (record.get("type") == "A" and
                        record.get("hostname") in cfg.hosts and
                        record.get("destination") != wan_ip):
                    record["destination"] = wan_ip
                    record["deleterecord"] = False
                    updates.append(record)
                    log.info(f"[{cfg.domain}] Updating {record['hostname']} → {wan_ip}")

            if updates:
                if update_dns_records(cfg, updates):
                    log.info(f"[{cfg.domain}] Updated {len(updates)} record(s)")
                    last_ips[domain_key] = wan_ip
            else:
                log.info(f"[{cfg.domain}] No A-records needed updating")
                last_ips[domain_key] = wan_ip
        finally:
            logout(cfg)


# ── DNS Cleanup ──────────────────────────────────────────────────────────────

def dns_cleanup(configs: list[DomainConfig]):
    """Remove duplicate/stale A-records for managed hosts."""
    for cfg in configs:
        log.info(f"[{cfg.domain}] Running DNS cleanup...")

        if not login(cfg):
            continue

        try:
            records = get_dns_records(cfg)
            if not records:
                continue

            # Group A-records by hostname
            a_records: dict[str, list[dict]] = {}
            for r in records:
                if r.get("type") == "A" and r.get("hostname") in cfg.hosts:
                    a_records.setdefault(r["hostname"], []).append(r)

            to_delete = []
            wan_ip = get_wan_ip()

            for hostname, recs in a_records.items():
                if len(recs) <= 1:
                    continue

                # Keep the one with current IP, delete duplicates
                keep = None
                for r in recs:
                    if r.get("destination") == wan_ip:
                        keep = r
                        break

                if not keep:
                    keep = recs[0]  # keep first if none matches

                for r in recs:
                    if r is not keep:
                        r["deleterecord"] = True
                        to_delete.append(r)
                        log.info(f"[{cfg.domain}] Deleting duplicate: {hostname} → {r.get('destination')}")

            if to_delete:
                if update_dns_records(cfg, to_delete):
                    log.info(f"[{cfg.domain}] Cleaned up {len(to_delete)} duplicate record(s)")
            else:
                log.info(f"[{cfg.domain}] No duplicates found")

        finally:
            logout(cfg)


# ── Main Loop ────────────────────────────────────────────────────────────────

def ddns_loop(configs: list[DomainConfig]):
    """DDNS update loop."""
    while True:
        try:
            ddns_update(configs)
        except Exception as e:
            log.error(f"DDNS cycle error: {e}")
        time.sleep(DDNS_INTERVAL)


def cleanup_loop(configs: list[DomainConfig]):
    """Cleanup loop."""
    time.sleep(60)  # wait a bit before first cleanup
    while True:
        try:
            dns_cleanup(configs)
        except Exception as e:
            log.error(f"Cleanup cycle error: {e}")
        time.sleep(CLEANUP_INTERVAL)


def main():
    configs = parse_domains()

    if not configs:
        log.error("No valid domain configurations. Exiting.")
        return

    log.info(f"Netcup DNS Manager started")
    log.info(f"Domains: {', '.join(c.domain for c in configs)}")
    log.info(f"DDNS interval: {DDNS_INTERVAL}s")
    log.info(f"Cleanup: {'enabled' if CLEANUP_ENABLED else 'disabled'} (interval: {CLEANUP_INTERVAL}s)")

    # Start cleanup thread
    if CLEANUP_ENABLED:
        cleanup_thread = threading.Thread(target=cleanup_loop, args=(configs,), daemon=True)
        cleanup_thread.start()

    # Run DDNS in main thread
    ddns_loop(configs)


if __name__ == "__main__":
    main()
