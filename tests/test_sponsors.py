#!/usr/bin/env python3
"""Unit and integration tests for scripts/sponsors.py and scripts/kofi-webhook-worker.js."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SPONSORS_PY = REPO_ROOT / "scripts" / "sponsors.py"

spec = importlib.util.spec_from_file_location("sponsors", SPONSORS_PY)
assert spec and spec.loader
sponsors = importlib.util.module_from_spec(spec)
sys.modules["sponsors"] = sponsors
spec.loader.exec_module(sponsors)


class SponsorsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc)
        env_patcher = mock.patch.dict(
            os.environ,
            {
                "GITHUB_LOGIN": "",
                "KOFI_HANDLE": "",
                "GITHUB_REPOSITORY_OWNER": "",
                "GITHUB_REPOSITORY": "",
                "EXTRA_SPONSORS_JSON": "",
                "KOFI_WEBHOOK_PAYLOAD": "",
            },
            clear=False,
        )
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

    def test_load_funding_defaults_and_git_remote_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            funding = repo / ".github" / "FUNDING.yml"
            funding.parent.mkdir(parents=True)
            funding.write_text("github: [octocat]\nko_fi: octokofi # comment\n", encoding="utf-8")
            gh, kofi = sponsors.load_funding_defaults(funding, repo_dir=repo)
            self.assertEqual(gh, "octocat")
            self.assertEqual(kofi, "octokofi")
            self.assertEqual(sponsors.kofi_profile_url(kofi), "https://ko-fi.com/octokofi")

            # Without FUNDING.yml, falls back to git remote origin owner even if CI env is set
            funding.unlink()
            subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
            subprocess.run(
                ["git", "remote", "add", "origin", "https://github.com/torvalds/linux.git"],
                cwd=repo,
                check=True,
            )
            with mock.patch.dict(
                os.environ,
                {"GITHUB_REPOSITORY_OWNER": "ci-owner", "GITHUB_REPOSITORY": "ci-owner/repo"},
                clear=False,
            ):
                gh2, kofi2 = sponsors.load_funding_defaults(funding, repo_dir=repo)
                self.assertEqual(gh2, "torvalds")
                self.assertEqual(kofi2, "torvalds")

            # Without git remote or FUNDING.yml, falls back to GITHUB_REPOSITORY_OWNER / GITHUB_REPOSITORY
            subprocess.run(["git", "remote", "remove", "origin"], cwd=repo, check=True)
            with mock.patch.dict(
                os.environ,
                {"GITHUB_REPOSITORY": "monalisa/octo-repo"},
                clear=False,
            ):
                gh3, kofi3 = sponsors.load_funding_defaults(funding, repo_dir=repo)
                self.assertEqual(gh3, "monalisa")
                self.assertEqual(kofi3, "monalisa")

    def test_fetch_sponsors_falls_back_when_token_lacks_read_org_scope(self) -> None:
        queries_sent: list[str] = []

        def fake_post_graphql(
            query: str,
            variables: dict[str, object],
            token: str,
        ) -> dict[str, object]:
            queries_sent.append(query)
            if "Organization" in query:
                return {
                    "errors": [
                        {
                            "type": "INSUFFICIENT_SCOPES",
                            "message": (
                                "The 'login' field requires one of the following scopes: "
                                "['read:org'], but your token has only been granted the: "
                                "['read:user'] scopes."
                            ),
                        }
                    ]
                }
            return {
                "data": {
                    "user": {
                        "sponsorshipsAsMaintainer": {
                            "totalCount": 1,
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [
                                {
                                    "tier": {
                                        "name": "$5 a month",
                                        "monthlyPriceInDollars": 5,
                                        "isOneTime": False,
                                    },
                                    "sponsorEntity": {
                                        "login": "ada",
                                        "name": "Ada Lovelace",
                                        "avatarUrl": "",
                                        "url": "https://github.com/ada",
                                    },
                                }
                            ],
                        }
                    }
                }
            }

        with mock.patch.object(sponsors, "_post_graphql", side_effect=fake_post_graphql):
            result = sponsors.fetch_sponsors("octocat", "ghp_test_token")

        self.assertEqual(len(queries_sent), 2)
        self.assertIn("... on Organization", queries_sent[0])
        self.assertNotIn("... on Organization", queries_sent[1])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].login, "ada")
        self.assertEqual(result[0].monthly_dollars, 5)
        self.assertFalse(result[0].is_one_time)

    def test_sponsors_config_overrides_and_key_preservation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "sponsors.json"
            state.write_text(
                json.dumps(
                    {
                        "featured_threshold": 10,
                        "active_window_days": 60,
                        "kofi_tiers": {"gold member": 15},
                        "allowed_hosts": ["example.org"],
                        "sponsors": [],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            cfg = sponsors.load_sponsors_config(state)
            self.assertEqual(cfg.featured_threshold, 10)
            self.assertEqual(cfg.active_window_days, 60)
            self.assertEqual(cfg.kofi_tiers["gold member"], 15)
            self.assertIn("example.org", cfg.allowed_hosts)
            self.assertIn("www.example.org", cfg.allowed_hosts)

            # Ingesting a custom tier preserves top-level config keys in sponsors.json
            self.assertTrue(
                sponsors.ingest_kofi_payload(
                    {
                        "kofi_transaction_id": "tx-cfg-1",
                        "is_public": True,
                        "from_name": "Ada (@ada)",
                        "amount": "15.00",
                        "tier_name": "Gold Member",
                        "is_subscription_payment": True,
                    },
                    state,
                    now=self.now,
                    network_lookup=False,
                )
            )
            written_doc = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(written_doc["featured_threshold"], 10)
            self.assertEqual(written_doc["active_window_days"], 60)
            self.assertEqual(written_doc["kofi_tiers"], {"gold member": 15})
            self.assertEqual(len(written_doc["sponsors"]), 1)
            self.assertEqual(written_doc["sponsors"][0]["monthly_dollars"], 15)
            self.assertEqual(written_doc["sponsors"][0]["expires_at"], "2027-01-04T12:00:00Z")

            loaded = sponsors.load_extra_sponsors(state, now=self.now)
            svg = sponsors.render_sponsors_svg(loaded, featured_threshold=cfg.featured_threshold)
            self.assertIn("avatar-ring-featured", svg)

    def test_anonymous_and_test_kofi_events_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "sponsors.json"

            # Blank or generic anonymous names without social tags are skipped
            for anon_name in ("", "   ", "Anonymous", "Someone", "Supporter", "a supporter", "unknown"):
                updated = sponsors.ingest_kofi_payload(
                    {
                        "is_public": True,
                        "from_name": anon_name,
                        "message": "Keep up the great work!",
                        "amount": "10.00",
                    },
                    state,
                    now=self.now,
                    network_lookup=False,
                )
                self.assertFalse(updated, f"Expected '{anon_name}' to be skipped")

            # Official Ko-fi "Send Test Webhook" payload is skipped
            self.assertFalse(
                sponsors.ingest_kofi_payload(
                    {
                        "kofi_transaction_id": "00000000-1111-2222-3333-444444444444",
                        "is_public": True,
                        "from_name": "Jo",
                        "message": "Good luck with the integration!",
                        "amount": "3.00",
                        "email": "jo.example@example.com",
                    },
                    state,
                    now=self.now,
                    network_lookup=False,
                )
            )
            self.assertFalse(
                sponsors.ingest_kofi_payload(
                    {
                        "is_public": True,
                        "from_name": "Jo",
                        "amount": "3.00",
                        "email_sha256": hashlib.sha256(b"jo.example@example.com").hexdigest(),
                    },
                    state,
                    now=self.now,
                    network_lookup=False,
                )
            )

            # Anonymous display name WITH an explicit @github tag in message IS ingested under the GitHub handle
            self.assertTrue(
                sponsors.ingest_kofi_payload(
                    {
                        "kofi_transaction_id": "tx-real-1",
                        "is_public": True,
                        "from_name": "Anonymous",
                        "message": "gh: @octocat",
                        "amount": "5.00",
                    },
                    state,
                    now=self.now,
                    network_lookup=False,
                )
            )
            loaded = sponsors.load_extra_sponsors(state, now=self.now)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].login, "octocat")
            self.assertEqual(loaded[0].name, "octocat")
            self.assertEqual(loaded[0].url, "https://github.com/octocat")

    def test_one_time_tip_and_email_sha256_renewal_preserve_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "sponsors.json"
            ada_hash = hashlib.sha256(b"ada@example.com").hexdigest()

            # 1. User subscribes at $25/mo Featured tier with @ada in message
            self.assertTrue(
                sponsors.ingest_kofi_payload(
                    {
                        "kofi_transaction_id": "tx-sub-1",
                        "is_public": True,
                        "from_name": "Ada Lovelace",
                        "message": "gh: @ada",
                        "amount": "25.00",
                        "tier_name": "$25 a month",
                        "is_subscription_payment": True,
                        "email_sha256": ada_hash,
                    },
                    state,
                    now=self.now,
                    network_lookup=False,
                )
            )

            # 2. Month 2 renewal arrives with empty message and display name "Ada Lovelace" (no @ada tag)
            #    Matches via email_sha256 and preserves login="ada" and GitHub profile URL
            self.assertTrue(
                sponsors.ingest_kofi_payload(
                    {
                        "kofi_transaction_id": "tx-sub-2",
                        "is_public": True,
                        "from_name": "Ada Lovelace",
                        "message": "",
                        "amount": "25.00",
                        "tier_name": "$25 a month",
                        "is_subscription_payment": True,
                        "email_sha256": ada_hash,
                    },
                    state,
                    now=self.now,
                    network_lookup=False,
                )
            )

            # 3. Same user sends a $3 one-time tip without message tags -> does not downgrade recurring tier
            self.assertTrue(
                sponsors.ingest_kofi_payload(
                    {
                        "kofi_transaction_id": "tx-tip-3",
                        "is_public": True,
                        "from_name": "ada",
                        "amount": "3.00",
                        "is_subscription_payment": False,
                    },
                    state,
                    now=self.now,
                    network_lookup=False,
                )
            )

            loaded = sponsors.load_extra_sponsors(state, now=self.now)
            self.assertEqual(len(loaded), 1)
            self.assertFalse(loaded[0].is_one_time)
            self.assertEqual(loaded[0].login, "ada")
            self.assertEqual(loaded[0].monthly_dollars, 25)
            self.assertEqual(loaded[0].name, "Ada Lovelace")
            self.assertEqual(loaded[0].url, "https://github.com/ada")

    def test_idempotent_duplicate_webhook_and_cumulative_one_time_tips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "sponsors.json"

            payload = {
                "kofi_transaction_id": "tx-100",
                "is_public": True,
                "from_name": "Linus (@torvalds)",
                "amount": "10.00",
            }
            self.assertTrue(
                sponsors.ingest_kofi_payload(payload, state, now=self.now, network_lookup=False)
            )
            # Duplicate delivery of the exact same transaction ID is ignored
            self.assertFalse(
                sponsors.ingest_kofi_payload(payload, state, now=self.now, network_lookup=False)
            )

            # A second distinct one-time tip of $20 sums to $30, but stays one-time (Standard tier)
            payload2 = {
                "kofi_transaction_id": "tx-101",
                "is_public": True,
                "from_name": "Linus (@torvalds)",
                "amount": "20.00",
            }
            self.assertTrue(
                sponsors.ingest_kofi_payload(payload2, state, now=self.now, network_lookup=False)
            )
            loaded = sponsors.load_extra_sponsors(state, now=self.now)
            self.assertEqual(len(loaded), 1)
            self.assertTrue(loaded[0].is_one_time)
            self.assertEqual(loaded[0].monthly_dollars, 30)
            self.assertFalse(sponsors.has_recurring_sponsors(loaded))

    def test_cross_platform_deduplication_and_recurring_sum(self) -> None:
        gh = [
            sponsors.Sponsor(
                login="alice",
                name="Alice Smith",
                avatar_url="https://github.com/alice.png?size=160",
                url="https://github.com/alice",
                monthly_dollars=5,
                is_one_time=False,
                platform="github",
            )
        ]
        kofi = [
            sponsors.Sponsor(
                login="alice",
                name="alice",
                avatar_url="",
                url="https://github.com/alice",
                monthly_dollars=20,
                is_one_time=False,
                platform="kofi",
            )
        ]
        merged = sponsors.merge_sponsors(gh, kofi)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].login, "alice")
        self.assertEqual(merged[0].name, "Alice Smith")
        self.assertEqual(merged[0].monthly_dollars, 25)
        self.assertFalse(merged[0].is_one_time)

        # One-time Ko-fi tip alongside a $5/mo GitHub sponsorship does NOT inflate recurring to $25/mo
        kofi_tip = [
            sponsors.Sponsor(
                login="alice",
                name="alice",
                avatar_url="",
                url="https://github.com/alice",
                monthly_dollars=25,
                is_one_time=True,
                platform="kofi",
            )
        ]
        merged_with_tip = sponsors.merge_sponsors(gh, kofi_tip)
        self.assertEqual(len(merged_with_tip), 1)
        self.assertFalse(merged_with_tip[0].is_one_time)
        self.assertEqual(merged_with_tip[0].monthly_dollars, 5)

    def test_orphan_branch_state_persistence_without_dirtying_main(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)

            # Copy scripts/sponsors.py and .github/sponsors.json into the temp repo
            (repo / "scripts").mkdir()
            (repo / "scripts" / "sponsors.py").write_text(
                SPONSORS_PY.read_text(encoding="utf-8"), encoding="utf-8"
            )
            (repo / ".github").mkdir()
            main_extra = repo / ".github" / "sponsors.json"
            main_extra.write_text('{\n  "sponsors": []\n}\n', encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

            out_svg = repo / "out" / "sponsors.svg"
            kofi_1 = json.dumps({
                "kofi_transaction_id": "tx-b1",
                "is_public": True,
                "from_name": "Grace Hopper",
                "amount": "25.00",
                "tier_name": "$25 a month",
                "is_subscription_payment": True,
            })
            subprocess.run(
                [
                    sys.executable,
                    str(repo / "scripts" / "sponsors.py"),
                    "--demo",
                    "--output",
                    str(out_svg),
                    "--extra",
                    str(main_extra),
                    "--ingest-kofi",
                    kofi_1,
                    "--branch",
                    "badges",
                ],
                cwd=repo,
                check=True,
                capture_output=True,
            )

            # Verify main_extra is completely untouched
            self.assertEqual(json.loads(main_extra.read_text(encoding="utf-8")), {"sponsors": []})

            # Verify badges branch has sponsors.json with Grace Hopper
            badges_json_1 = json.loads(
                subprocess.run(
                    ["git", "show", "refs/heads/badges:sponsors.json"],
                    cwd=repo,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
            )
            self.assertEqual(len(badges_json_1["sponsors"]), 1)
            self.assertEqual(badges_json_1["sponsors"][0]["login"], "Grace Hopper")

            # Second run with a second Ko-fi webhook preserves Grace and adds Alan
            kofi_2 = json.dumps({
                "kofi_transaction_id": "tx-b2",
                "is_public": True,
                "from_name": "Alan Turing",
                "amount": "5.00",
                "tier_name": "$5 a month",
                "is_subscription_payment": True,
            })
            subprocess.run(
                [
                    sys.executable,
                    str(repo / "scripts" / "sponsors.py"),
                    "--demo",
                    "--output",
                    str(out_svg),
                    "--extra",
                    str(main_extra),
                    "--ingest-kofi",
                    kofi_2,
                    "--branch",
                    "badges",
                ],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            badges_json_2 = json.loads(
                subprocess.run(
                    ["git", "show", "refs/heads/badges:sponsors.json"],
                    cwd=repo,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
            )
            self.assertEqual(
                [s["login"] for s in badges_json_2["sponsors"]],
                ["Grace Hopper", "Alan Turing"],
            )

    def test_worker_kofi_and_github_webhooks(self) -> None:
        script = """
import worker from "./scripts/kofi-webhook-worker.js";

const calls = [];
globalThis.fetch = async (url, init) => {
  calls.push({ url, init: { ...init, body: JSON.parse(init.body) } });
  return new Response(null, { status: 204 });
};

const env = {
  KOFI_VERIFICATION_TOKEN: "secret-kofi-token",
  GITHUB_DISPATCH_TOKEN: "ghp_test_dispatch",
  GITHUB_WEBHOOK_SECRET: "secret-gh-hmac",
  GITHUB_REPO: "octocat/octocat",
};

// 1. Ko-fi test webhook -> 200 OK without calling GitHub
let form = new URLSearchParams();
form.set("data", JSON.stringify({
  verification_token: "secret-kofi-token",
  kofi_transaction_id: "00000000-1111-2222-3333-444444444444",
  from_name: "Jo",
  email: "jo.example@example.com",
  amount: "3.00",
  is_public: true,
}));
let res = await worker.fetch(new Request("https://example.workers.dev/", {
  method: "POST",
  headers: { "content-type": "application/x-www-form-urlencoded" },
  body: form.toString(),
}), env);
if (res.status !== 200 || calls.length !== 0) throw new Error("Expected test webhook to skip dispatch");

// 2. Real Ko-fi webhook -> dispatches kofi-webhook with hashed email
form = new URLSearchParams();
form.set("data", JSON.stringify({
  verification_token: "secret-kofi-token",
  kofi_transaction_id: "real-tx-42",
  message_id: "msg-42",
  from_name: "Ada (@ada)",
  email: "ada@example.com",
  amount: "25.00",
  tier_name: "$25 a month",
  is_subscription_payment: true,
  is_public: true,
}));
res = await worker.fetch(new Request("https://example.workers.dev/", {
  method: "POST",
  headers: { "content-type": "application/x-www-form-urlencoded" },
  body: form.toString(),
}), env);
if (res.status !== 200 || calls.length !== 1) throw new Error("Expected real Ko-fi webhook to dispatch");
if (calls[0].init.body.event_type !== "kofi-webhook") throw new Error("Wrong event_type");
if (calls[0].init.body.client_payload.kofi.email) throw new Error("Raw email leaked");
if (!calls[0].init.body.client_payload.kofi.email_sha256) throw new Error("Missing email_sha256");

// 3. GitHub Sponsors webhook with valid HMAC -> dispatches sponsors-updated
const ghBody = JSON.stringify({ action: "created" });
const enc = new TextEncoder();
const key = await crypto.subtle.importKey("raw", enc.encode(env.GITHUB_WEBHOOK_SECRET), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
const sigBuf = await crypto.subtle.sign("HMAC", key, enc.encode(ghBody));
const sigHex = Array.from(new Uint8Array(sigBuf)).map(b => b.toString(16).padStart(2, "0")).join("");

res = await worker.fetch(new Request("https://example.workers.dev/", {
  method: "POST",
  headers: {
    "content-type": "application/json",
    "x-github-event": "sponsorship",
    "x-hub-signature-256": `sha256=${sigHex}`,
  },
  body: ghBody,
}), env);
if (res.status !== 200 || calls.length !== 2) throw new Error("Expected GitHub sponsorship webhook to dispatch");
if (calls[1].init.body.event_type !== "sponsors-updated") throw new Error("Wrong GH event_type");

console.log("WORKER_OK");
"""
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        self.assertIn("WORKER_OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
