"""
scripts/seed_api_key.py — create a tenant API key for local testing.

Usage:
    docker compose exec app python scripts/seed_api_key.py \
        --tenant acme-corp \
        --description "Acme Corp NOC"

Prints the raw API key (only shown once). Store it in your secrets manager.
"""

import asyncio
import hashlib
import os
import secrets
import sys
from argparse import ArgumentParser

# Adjust path so we can import from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from db.session import AsyncSessionLocal, init_db
from models import ApiKey


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def create_key(tenant_id: str, description: str) -> str:
    await init_db()
    raw_key = secrets.token_urlsafe(32)
    key_hash = _hash_key(raw_key)

    async with AsyncSessionLocal() as session:
        api_key = ApiKey(
            key_hash=key_hash,
            tenant_id=tenant_id,
            description=description,
            is_active="1",
        )
        session.add(api_key)
        await session.commit()
        print(f"\n✅ API key created")
        print(f"   Tenant:      {tenant_id}")
        print(f"   Description: {description}")
        print(f"   Key (raw):   {raw_key}")
        print(f"\n   Add to X-API-Key header. This key is shown only once.\n")

    return raw_key


if __name__ == "__main__":
    parser = ArgumentParser(description="Create a TriageOps API key")
    parser.add_argument("--tenant", required=True, help="Tenant ID (e.g. acme-corp)")
    parser.add_argument("--description", default="", help="Human-readable description")
    args = parser.parse_args()

    asyncio.run(create_key(args.tenant, args.description))
