import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from public_endpoint import (  # noqa: E402
    HOST_STATE_KEY,
    normalize_public_host,
    sync_public_endpoint,
)


class PublicEndpointTest(unittest.TestCase):
    def setUp(self):
        handle, self.db = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        con = sqlite3.connect(self.db)
        con.executescript(
            """
            CREATE TABLE settings(key TEXT, value TEXT);
            CREATE TABLE inbounds(
                id INTEGER PRIMARY KEY, remark TEXT, port INTEGER,
                stream_settings TEXT
            );
            CREATE TABLE hosts(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT, inbound_id INTEGER NOT NULL,
                sort_order INTEGER DEFAULT 0, remark TEXT,
                is_disabled numeric DEFAULT 0, is_hidden numeric DEFAULT 0,
                address TEXT, port INTEGER DEFAULT 0,
                security TEXT DEFAULT 'same', sni TEXT, host_header TEXT,
                path TEXT, override_sni_from_address numeric,
                keep_sni_blank numeric, allow_insecure numeric,
                created_at INTEGER, updated_at INTEGER
            );
            INSERT INTO settings VALUES('subURI','http://old.invalid:2096/random/');
            """
        )
        stream = {
            "network": "ws",
            "externalProxy": [
                {"remark": "CATWOMAN", "dest": "old.invalid", "port": 10085,
                 "forceTls": "none", "sni": "", "fingerprint": "chrome"},
                {"remark": "MY-CDN", "dest": "manual.cdn.net", "port": 8443,
                 "forceTls": "tls", "sni": "manual.sni.net"},
                {"remark": "CATWOMAN", "dest": "duplicate.invalid", "port": 1,
                 "forceTls": "none", "sni": ""},
            ],
            "wsSettings": {
                "path": "/old", "host": "old.invalid",
                "headers": {"X-Custom": "kept"},
            },
        }
        con.execute(
            "INSERT INTO inbounds VALUES(1,'CATWOMAN',10085,?)",
            (json.dumps(stream),),
        )
        con.execute(
            "INSERT INTO hosts(group_id,inbound_id,sort_order,remark,address,port,"
            "security,sni,host_header,path,allow_insecure,is_disabled,is_hidden) "
            "VALUES('managed',1,0,'CATWOMAN','old.invalid',10085,'none','','','',1,1,1)"
        )
        con.execute(
            "INSERT INTO hosts(group_id,inbound_id,sort_order,remark,address,port,"
            "security,sni,host_header,path,allow_insecure) "
            "VALUES('duplicate',1,9,'CATWOMAN','duplicate.invalid',1,'none','','','/bad',1)"
        )
        con.execute(
            "INSERT INTO hosts(group_id,inbound_id,sort_order,remark,address,port,"
            "security,sni,host_header,path,allow_insecure) "
            "VALUES('custom',1,1,'MY-CDN','manual.cdn.net',8443,'tls',"
            "'manual.sni.net','manual.host.net','/manual',0)"
        )

        missing = {
            "network": "ws",
            "externalProxy": [
                {"remark": "OTHER", "dest": "other.cdn.net", "port": 9443,
                 "forceTls": "tls", "sni": "other.sni.net"}
            ],
            "wsSettings": {"path": "/stale", "headers": {"X-Keep": "yes"}},
        }
        con.execute(
            "INSERT INTO inbounds VALUES(2,'CATWOMAN',10085,?)",
            (json.dumps(missing),),
        )
        con.execute("INSERT INTO inbounds VALUES(3,'UNRELATED',10085,'{\"keep\":true}')")
        con.execute("INSERT INTO inbounds VALUES(4,'CATWOMAN',10999,'{\"keep\":true}')")
        con.commit()
        con.close()

    def tearDown(self):
        os.unlink(self.db)

    def test_hostname_normalization_and_rejection(self):
        self.assertEqual(
            normalize_public_host("New-Cat.UP.Railway.App:443"),
            "new-cat.up.railway.app",
        )
        for bad in (
            "", "localhost", "127.0.0.1", "[::1]", "one-label",
            "bad host.example", "evil.example/path", "user@evil.example",
        ):
            self.assertIsNone(normalize_public_host(bad), bad)

    def test_sync_is_selective_complete_idempotent_and_deduplicated(self):
        first = sync_public_endpoint(self.db, "new-cat.up.railway.app")
        self.assertGreater(first.changed, 0)
        con = sqlite3.connect(self.db)
        self.assertEqual(
            dict(con.execute(
                "SELECT key,value FROM settings WHERE key IN "
                "('subURI','subJsonURI','subClashURI')"
            )),
            {
                "subURI": "https://new-cat.up.railway.app/sub/",
                "subJsonURI": "https://new-cat.up.railway.app/sub-json/",
                "subClashURI": "https://new-cat.up.railway.app/sub-clash/",
            },
        )
        self.assertEqual(
            con.execute(
                "SELECT count(*) FROM hosts WHERE inbound_id=1 AND remark='CATWOMAN'"
            ).fetchone()[0],
            1,
        )
        managed = con.execute(
            "SELECT address,port,security,sni,host_header,path,allow_insecure,"
            "is_disabled,is_hidden FROM hosts WHERE group_id='managed'"
        ).fetchone()
        self.assertEqual(
            managed,
            ("new-cat.up.railway.app", 443, "tls", "new-cat.up.railway.app",
             "new-cat.up.railway.app", "/ws", 0, 0, 0),
        )
        custom = con.execute(
            "SELECT address,port,security,sni,host_header,path FROM hosts "
            "WHERE group_id='custom'"
        ).fetchone()
        self.assertEqual(
            custom,
            ("manual.cdn.net", 8443, "tls", "manual.sni.net",
             "manual.host.net", "/manual"),
        )
        self.assertEqual(
            con.execute(
                "SELECT count(*) FROM hosts WHERE inbound_id=2 AND remark='CATWOMAN'"
            ).fetchone()[0],
            1,
        )

        stream = json.loads(con.execute(
            "SELECT stream_settings FROM inbounds WHERE id=1"
        ).fetchone()[0])
        managed_proxies = [p for p in stream["externalProxy"]
                           if p.get("remark") == "CATWOMAN"]
        self.assertEqual(len(managed_proxies), 1)
        self.assertEqual(
            {k: managed_proxies[0][k] for k in ("dest", "port", "forceTls", "sni")},
            {"dest": "new-cat.up.railway.app", "port": 443, "forceTls": "tls",
             "sni": "new-cat.up.railway.app"},
        )
        self.assertEqual(managed_proxies[0]["fingerprint"], "chrome")
        self.assertEqual(stream["wsSettings"]["path"], "/ws")
        self.assertEqual(
            stream["wsSettings"]["headers"],
            {"X-Custom": "kept", "Host": "new-cat.up.railway.app"},
        )
        self.assertEqual(
            con.execute("SELECT stream_settings FROM inbounds WHERE id=3").fetchone()[0],
            '{"keep":true}',
        )
        self.assertEqual(
            con.execute("SELECT stream_settings FROM inbounds WHERE id=4").fetchone()[0],
            '{"keep":true}',
        )
        con.close()
        self.assertEqual(
            sync_public_endpoint(self.db, "new-cat.up.railway.app").changed,
            0,
        )

    def test_panel_edit_of_managed_host_is_preserved_across_requests(self):
        # The first request after upgrading repairs the old wrapper-owned row
        # and records the exact automatic baseline.
        sync_public_endpoint(self.db, "new-cat.up.railway.app")
        con = sqlite3.connect(self.db)
        state = json.loads(con.execute(
            "SELECT value FROM settings WHERE key=?", (HOST_STATE_KEY,)
        ).fetchone()[0])
        self.assertEqual(state["inbounds"]["1"]["mode"], "auto")

        # This is the database result of a successful Save in 3x-ui's Hosts
        # page. The next nginx auth subrequest must detect, not undo, it.
        con.execute(
            "UPDATE hosts SET address='manual.cdn.example',port=8443,"
            "security='tls',sni='manual.sni.example',"
            "host_header='manual.host.example',path='/manual-saved' "
            "WHERE group_id='managed'"
        )
        con.commit()
        con.close()

        result = sync_public_endpoint(self.db, "changed-cat.up.railway.app")
        self.assertEqual(result.hosts_preserved, 1)
        con = sqlite3.connect(self.db)
        self.assertEqual(
            con.execute(
                "SELECT address,port,security,sni,host_header,path FROM hosts "
                "WHERE group_id='managed'"
            ).fetchone(),
            ("manual.cdn.example", 8443, "tls", "manual.sni.example",
             "manual.host.example", "/manual-saved"),
        )
        state = json.loads(con.execute(
            "SELECT value FROM settings WHERE key=?", (HOST_STATE_KEY,)
        ).fetchone()[0])
        self.assertEqual(state["inbounds"]["1"]["mode"], "manual")
        con.close()

        # The ownership decision is persisted in SQLite, so later requests and
        # container restarts keep the panel edit even if the Railway host seen
        # by the wrapper changes again.
        sync_public_endpoint(self.db, "third-cat.up.railway.app")
        con = sqlite3.connect(self.db)
        self.assertEqual(
            con.execute(
                "SELECT address,host_header,path FROM hosts "
                "WHERE group_id='managed'"
            ).fetchone(),
            ("manual.cdn.example", "manual.host.example", "/manual-saved"),
        )
        con.close()

    def test_deleting_manual_managed_host_restores_automatic_mode(self):
        sync_public_endpoint(self.db, "new-cat.up.railway.app")
        con = sqlite3.connect(self.db)
        con.execute(
            "UPDATE hosts SET host_header='manual.host.example' "
            "WHERE group_id='managed'"
        )
        con.commit()
        con.close()
        sync_public_endpoint(self.db, "new-cat.up.railway.app")

        con = sqlite3.connect(self.db)
        con.execute("DELETE FROM hosts WHERE group_id='managed'")
        con.commit()
        con.close()
        result = sync_public_endpoint(self.db, "restored-cat.up.railway.app")
        self.assertEqual(result.hosts_created, 1)

        con = sqlite3.connect(self.db)
        row = con.execute(
            "SELECT address,port,security,sni,host_header,path FROM hosts "
            "WHERE inbound_id=1 AND remark='CATWOMAN'"
        ).fetchone()
        state = json.loads(con.execute(
            "SELECT value FROM settings WHERE key=?", (HOST_STATE_KEY,)
        ).fetchone()[0])
        con.close()
        self.assertEqual(
            row,
            ("restored-cat.up.railway.app", 443, "tls",
             "restored-cat.up.railway.app", "restored-cat.up.railway.app", "/ws"),
        )
        self.assertEqual(state["inbounds"]["1"]["mode"], "auto")

    def test_public_edge_cannot_be_downgraded(self):
        with self.assertRaises(ValueError):
            sync_public_endpoint(self.db, "new-cat.up.railway.app", public_port=10085)
        with self.assertRaises(ValueError):
            sync_public_endpoint(self.db, "new-cat.up.railway.app", security="none")
        with self.assertRaises(ValueError):
            sync_public_endpoint(
                self.db, "new-cat.up.railway.app", sub_json_path="/sub/"
            )


if __name__ == "__main__":
    unittest.main()
