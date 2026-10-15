"""papaia-addon-mediawiki — seed the MCP server's ACL from the wiki's access tiers.

Runs once before the MCP server starts, inside the MCP image, and writes
grants through that image's own ``AclResolver`` so the table format always
matches the server version.

For every tier namespace ``Name`` with groups ``g1, g2`` it grants ``rw`` on
``Name:``, ``Name talk:`` and ``Name_talk:`` to each group. Any non-wildcard
grant "claims" its prefix, so a wildcard grant elsewhere never reaches into
it. The main namespace (``*``) goes to MEDIAWIKI_MCP_MAIN_GROUPS, by default
every group named in a tier.

MEDIAWIKI_MCP_ACL_SEED:
  if-empty  (default) seed only an empty table, so grants an MCP admin
            changed later are kept
  sync      upsert the derived grants on every start; never revokes
  off       do nothing
"""
from __future__ import annotations

import asyncio
import os
import re
import sys

# The MCP image keeps its application code in /app.
sys.path.insert(0, "/app")

from auth.acl import AclResolver
from auth.models import AclEntry

_NAME = r"[A-Za-z][A-Za-z0-9_]*"


def _split(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def derive_grants() -> list[AclEntry]:
    tiers: dict[str, list[str]] = {}
    for rule in os.environ.get("MW_NAMESPACE_READ_GROUPS", "").split(";"):
        if not rule.strip():
            continue
        match = re.fullmatch(rf"\s*({_NAME})\s*=\s*(.+)", rule)
        if not match:
            sys.exit(f"mcp-acl-seed: MW_NAMESPACE_READ_GROUPS: cannot parse {rule!r}")
        tiers[match.group(1)] = _split(match.group(2))

    namespaces = [
        spec.split(":", 1)[0] for spec in _split(os.environ.get("MW_NAMESPACES", ""))
    ]
    unknown = set(tiers) - set(namespaces)
    if unknown:
        sys.exit(f"mcp-acl-seed: groups given for unknown namespaces: {sorted(unknown)}")

    grants: list[AclEntry] = []
    for name in namespaces:
        for group in tiers.get(name, []):
            for prefix in (f"{name}:", f"{name} talk:", f"{name}_talk:"):
                grants.append(AclEntry(role=group, prefix=prefix, access="rw"))

    main_groups = _split(os.environ.get("MEDIAWIKI_MCP_MAIN_GROUPS", "")) or sorted(
        {group for groups in tiers.values() for group in groups}
    )
    for group in main_groups:
        grants.append(AclEntry(role=group, prefix="*", access="rw"))
    return grants


async def main() -> None:
    mode = os.environ.get("MEDIAWIKI_MCP_ACL_SEED", "if-empty").strip() or "if-empty"
    if mode == "off":
        print("mcp-acl-seed: disabled")
        return
    if mode not in ("if-empty", "sync"):
        sys.exit(f"mcp-acl-seed: MEDIAWIKI_MCP_ACL_SEED must be if-empty, sync or off, not {mode!r}")

    grants = derive_grants()
    resolver = AclResolver(os.environ.get("RBAC_ACL_DB_PATH", "/data/acl.sqlite"), cache_ttl=1)
    if mode == "if-empty":
        await resolver.seed_if_empty(grants)
    else:
        for grant in grants:
            await resolver.grant(grant)
    for grant in await resolver.list_all():
        print(f"mcp-acl-seed: {grant.role} {grant.access} {grant.prefix}")


if __name__ == "__main__":
    asyncio.run(main())
