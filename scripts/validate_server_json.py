"""Validate server.json against the MCP Registry schema it declares.

Usage: python scripts/validate_server_json.py [server.json] [--schema PATH]

Fetches the schema from its $schema URL unless --schema points at a local copy.
Requires the `jsonschema` package (in the dev dependency group).
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

from jsonschema import Draft7Validator, validators


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("server_json", nargs="?", default="server.json", type=Path)
    parser.add_argument("--schema", type=Path, help="local copy of the schema (skips the download)")
    args = parser.parse_args()

    doc = json.loads(args.server_json.read_text())
    if args.schema:
        schema = json.loads(args.schema.read_text())
    else:
        with urllib.request.urlopen(doc["$schema"], timeout=30) as resp:
            schema = json.load(resp)

    cls = validators.validator_for(schema, default=Draft7Validator)
    errors = sorted(cls(schema).iter_errors(doc), key=lambda e: list(e.path))
    for err in errors:
        where = "/".join(str(p) for p in err.path) or "<root>"
        print(f"{args.server_json}: {where}: {err.message}", file=sys.stderr)
    if errors:
        return 1
    print(f"{args.server_json}: valid against {doc['$schema']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
