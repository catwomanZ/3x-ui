#!/usr/bin/env python3
"""Keep Catwoman's managed Railway endpoint complete and reachable.

3x-ui stores a share endpoint in the legacy externalProxy block, the v3.10
Hosts table, and three URI settings for raw/JSON/Clash links shown by the panel.
Railway exposes none of Xray's internal ports; every public config must use the actual request
host on port 443 with TLS, SNI, WebSocket Host and path all aligned.

Only an inbound on ``inbound_port`` that is identifiable by the ``brand``
remark (on the inbound or one of its Hosts) is managed. Custom Hosts and
externalProxy entries created in the panel are deliberately left alone.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import secrets
import sqlite3
import string
import time
import urllib.parse
from dataclasses import dataclass


@dataclass
class SyncResult:
    host: str
    settings: int = 0
    inbounds: int = 0
    hosts: int = 0
    hosts_created: int = 0

    @property
    def changed(self) -> int:
        return self.settings + self.inbounds + self.hosts + self.hosts_created


def normalize_public_host(value: str | None) -> str | None:
    """Return a safe DNS hostname (without a port), or ``None``."""
    value = (value or "").strip()
    if not value or any(ch in value for ch in "/\\@?#"):
        return None
    try:
        host = urllib.parse.urlsplit("//" + value).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.rstrip(".").lower()
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    if host == "localhost" or "." not in host or len(host) > 253:
        return None
    labels = host.split(".")
    if any(
        not label
        or len(label) > 63
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
        for label in labels
    ):
        return None
    return host


def _group_id() -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(16))


def _upsert_setting(con: sqlite3.Connection, key: str, value: str) -> int:
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row and row[0] == value:
        return 0
    if row:
        con.execute("UPDATE settings SET value=? WHERE key=?", (value, key))
    else:
        con.execute("INSERT INTO settings(key,value) VALUES(?,?)", (key, value))
    return 1


def _managed_stream(raw: str, host: str, port: int, security: str,
                    brand: str, tunnel_path: str) -> tuple[str, bool]:
    """Update only Catwoman's externalProxy entry in one streamSettings JSON."""
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError):
        return raw, False
    if not isinstance(obj, dict):
        return raw, False

    proxies = obj.get("externalProxy")
    if not isinstance(proxies, list):
        proxies = []

    managed = [
        i for i, item in enumerate(proxies)
        if isinstance(item, dict) and item.get("remark") == brand
    ]

    endpoint = {
        "forceTls": security,
        "dest": host,
        "port": port,
        "remark": brand,
        "sni": host,
    }
    if managed:
        # The brand label is reserved for the one Catwoman-managed Railway
        # endpoint. Keep the oldest occurrence (including any extra fields an
        # upgrade added) and collapse stale duplicates so subscriptions do not
        # emit the same server more than once.
        keep = managed[0]
        updated = dict(proxies[keep])
        updated.update(endpoint)
        proxies[keep] = updated
        duplicate_indexes = set(managed[1:])
        proxies = [
            item for index, item in enumerate(proxies)
            if index not in duplicate_indexes
        ]
    else:
        proxies.append(endpoint)
    obj["externalProxy"] = proxies

    # This is also the transport data used by direct share links. Keeping it in
    # step prevents an old WS Host/path from surviving beside a correct Host row.
    ws = obj.get("wsSettings")
    if not isinstance(ws, dict):
        ws = {}
    ws["path"] = tunnel_path
    ws["host"] = host
    headers = ws.get("headers")
    if not isinstance(headers, dict):
        headers = {}
    headers["Host"] = host
    ws["headers"] = headers
    obj["wsSettings"] = ws

    cooked = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return cooked, cooked != raw


def sync_public_endpoint(
    db_path: str,
    public_host: str,
    inbound_port: int = 10085,
    public_port: int = 443,
    security: str = "tls",
    brand: str = "CATWOMAN",
    sub_path: str = "/sub/",
    sub_json_path: str = "/sub-json/",
    sub_clash_path: str = "/sub-clash/",
    tunnel_path: str = "/ws",
) -> SyncResult:
    """Synchronize Catwoman's managed endpoint in one SQLite transaction."""
    host = normalize_public_host(public_host)
    if not host:
        raise ValueError("invalid public hostname")
    if public_port != 443 or security != "tls":
        raise ValueError("Railway's public endpoint must be port 443 with TLS")
    public_paths = {
        "subURI": sub_path,
        "subJsonURI": sub_json_path,
        "subClashURI": sub_clash_path,
    }
    if any(not path.startswith("/") or not path.endswith("/")
           for path in public_paths.values()):
        raise ValueError("subscription paths must start and end with '/'")
    if len(set(public_paths.values())) != len(public_paths):
        raise ValueError("subscription paths must be distinct")
    if not tunnel_path.startswith("/"):
        raise ValueError("tunnel_path must start with '/'")

    result = SyncResult(host=host)
    con = sqlite3.connect(db_path, timeout=10)
    try:
        con.execute("BEGIN IMMEDIATE")
        for key, path in public_paths.items():
            result.settings += _upsert_setting(
                con, key, f"https://{host}{path}"
            )

        inbound_ids: list[int] = []
        rows = con.execute(
            "SELECT i.id, i.stream_settings FROM inbounds i "
            "WHERE i.port=? AND (i.remark=? OR EXISTS ("
            "SELECT 1 FROM hosts h WHERE h.inbound_id=i.id AND h.remark=?))",
            (int(inbound_port), brand, brand),
        ).fetchall()
        for inbound_id, raw in rows:
            inbound_ids.append(inbound_id)
            cooked, changed = _managed_stream(
                raw, host, public_port, security, brand, tunnel_path
            )
            if changed:
                con.execute(
                    "UPDATE inbounds SET stream_settings=? WHERE id=?",
                    (cooked, inbound_id),
                )
                result.inbounds += 1

        now_ms = int(time.time() * 1000)
        for inbound_id in inbound_ids:
            host_rows = con.execute(
                "SELECT id, remark, address, port, security, sni, "
                "host_header, path, allow_insecure, is_disabled, is_hidden "
                "FROM hosts WHERE inbound_id=? ORDER BY id",
                (inbound_id,),
            ).fetchall()
            managed_rows = [row for row in host_rows if row[1] == brand]

            if managed_rows:
                # Exactly one row owns the reserved brand. Retain the oldest
                # one and remove only same-brand duplicates; custom rows are
                # never candidates for deletion.
                row = managed_rows[0]
                desired = (
                    host, public_port, security, host,
                    host, tunnel_path, 0, 0, 0,
                )
                current = row[2:]
                try:
                    current = (current[0], int(current[1]), *current[2:])
                except (TypeError, ValueError):
                    pass
                if current != desired:
                    con.execute(
                        "UPDATE hosts SET address=?, port=?, security=?, sni=?, "
                        "host_header=?, path=?, allow_insecure=0, "
                        "is_disabled=0, is_hidden=0, "
                        "override_sni_from_address=0, keep_sni_blank=0, "
                        "updated_at=? WHERE id=?",
                        (*desired[:6], now_ms, row[0]),
                    )
                    result.hosts += 1
                duplicate_ids = [duplicate[0] for duplicate in managed_rows[1:]]
                if duplicate_ids:
                    marks = ",".join("?" for _ in duplicate_ids)
                    con.execute(
                        f"DELETE FROM hosts WHERE id IN ({marks})",
                        duplicate_ids,
                    )
                    result.hosts += len(duplicate_ids)

            # A fresh API-created inbound normally gets a Host automatically.
            # If it is missing, add one without changing any custom Host rows.
            if not managed_rows:
                con.execute(
                    "INSERT INTO hosts (group_id,inbound_id,sort_order,remark,"
                    "is_disabled,is_hidden,address,port,security,sni,host_header,"
                    "path,override_sni_from_address,keep_sni_blank,allow_insecure,"
                    "created_at,updated_at) VALUES (?,?,0,?,0,0,?,?,?,?,?,? ,0,0,0,?,?)",
                    (
                        _group_id(), inbound_id, brand, host, public_port,
                        security, host, host, tunnel_path, now_ms, now_ms,
                    ),
                )
                result.hosts_created += 1

        con.commit()
        return result
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("host")
    parser.add_argument("--db", default="/etc/x-ui/x-ui.db")
    parser.add_argument("--inbound-port", type=int, default=10085)
    parser.add_argument("--brand", default="CATWOMAN")
    parser.add_argument("--sub-path", default="/sub/")
    parser.add_argument("--sub-json-path", default="/sub-json/")
    parser.add_argument("--sub-clash-path", default="/sub-clash/")
    parser.add_argument("--tunnel-path", default="/ws")
    args = parser.parse_args()
    result = sync_public_endpoint(
        args.db,
        args.host,
        inbound_port=args.inbound_port,
        brand=args.brand,
        sub_path=args.sub_path,
        sub_json_path=args.sub_json_path,
        sub_clash_path=args.sub_clash_path,
        tunnel_path=args.tunnel_path,
    )
    print(
        "[public-endpoint] https://%s:443 synchronized "
        "(settings=%d, inbounds=%d, hosts=%d, hosts_created=%d)"
        % (
            result.host,
            result.settings,
            result.inbounds,
            result.hosts,
            result.hosts_created,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
