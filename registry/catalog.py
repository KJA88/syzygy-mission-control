"""Load and query the machine-readable SYZYGY capability manifest."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "config" / "syzygy-capabilities.json"
PERMISSIONS = {"read", "write", "restricted"}
EXECUTIONS = {"registry", "proxy", "declared", "restricted"}
PROXY_CAPABILITIES = {"health.read", "health.write", "health.audit"}
FORBIDDEN_NAMES = ("shell", "exec", "filesystem", "fs.", "eval", "xlsx", "delete", "replace")


class CatalogError(ValueError):
    pass


class Catalog:
    def __init__(self, document: dict):
        self.document = document
        self.subsystems = {item["id"]: item for item in document["subsystems"]}
        self.capabilities = {item["name"]: item for item in document["capabilities"]}
        self._validate()

    @classmethod
    def load(cls, path: Path | None = None) -> Catalog:
        target = path or MANIFEST_PATH
        return cls(json.loads(target.read_text(encoding="utf-8")))

    def profile(self, name: str) -> dict:
        profiles = self.document["profiles"]
        if name not in profiles:
            raise KeyError(name)
        return profiles[name]

    def permissions(self, profile: str) -> set[str]:
        return set(self.profile(profile)["permissions"])

    def systems(self) -> list[dict]:
        rows = []
        for item in self.document["subsystems"]:
            names = [cap["name"] for cap in self.document["capabilities"] if cap["subsystem"] == item["id"]]
            row = _public_record(item)
            row["capabilities"] = names
            rows.append(row)
        return rows

    def tools(self, profile: str) -> list[dict]:
        allowed = self.permissions(profile)
        rows = []
        for cap in self.document["capabilities"]:
            executable = cap["permission"] in allowed and cap["execution"] != "restricted"
            row = _public_capability(cap)
            row["executable"] = executable
            rows.append(row)
        return rows

    def capability(self, name: str) -> dict:
        return _public_capability(self.capabilities[name])

    def owner_of(self, name: str) -> dict:
        cap = self.capabilities[name]
        system = self.subsystems[cap["subsystem"]]
        return {
            "capability": name,
            "owner": cap["owner"],
            "subsystem": system["id"],
            "subsystem_name": system["name"],
            "endpoint": cap["endpoint"],
        }

    def safety_of(self, name: str) -> dict:
        if name in self.capabilities:
            cap = self.capabilities[name]
            system = self.subsystems[cap["subsystem"]]
            return {
                "name": name,
                "permission": cap["permission"],
                "safety": list(cap["safety"]),
                "subsystem_safety": list(system["safety"]),
            }
        if name in self.subsystems:
            system = self.subsystems[name]
            return {"name": name, "safety": list(system["safety"])}
        raise KeyError(name)

    def state(self) -> dict:
        return {
            "probed": False,
            "subsystems": [
                {
                    "id": item["id"],
                    "name": item["name"],
                    "availability": item["availability"],
                    "endpoint": item["endpoint"],
                    "owner": item["owner"],
                }
                for item in self.document["subsystems"]
            ],
        }

    def mcp_tools(self) -> list[dict]:
        return [
            {
                "name": cap["name"],
                "description": cap["safety"][0],
                "inputSchema": cap["input_schema"],
            }
            for cap in self.document["capabilities"]
        ]

    def _validate(self) -> None:
        if self.document.get("schema_version") != 1:
            raise CatalogError("schema_version must be 1")
        if len(self.subsystems) != len(self.document["subsystems"]):
            raise CatalogError("duplicate subsystem id")
        if len(self.capabilities) != len(self.document["capabilities"]):
            raise CatalogError("duplicate capability name")
        for name, cap in self.capabilities.items():
            lowered = name.casefold()
            if any(token in lowered for token in FORBIDDEN_NAMES):
                raise CatalogError("forbidden capability name: " + name)
            if cap["permission"] not in PERMISSIONS:
                raise CatalogError("bad permission: " + name)
            if cap["execution"] not in EXECUTIONS:
                raise CatalogError("bad execution: " + name)
            if cap["subsystem"] not in self.subsystems:
                raise CatalogError("unknown subsystem: " + name)
            if cap["execution"] == "proxy" and name not in PROXY_CAPABILITIES:
                raise CatalogError("proxy is not implemented: " + name)
            if cap["execution"] == "restricted" and cap["permission"] != "restricted":
                raise CatalogError("restricted execution requires restricted permission: " + name)
        for profile in self.document["profiles"].values():
            unknown = set(profile["permissions"]).difference({"read", "write"})
            if unknown:
                raise CatalogError("profile grants a permission this layer cannot execute")


def _public_record(item: dict) -> dict:
    hidden = {"capabilities"}
    return {key: value for key, value in item.items() if key not in hidden}


def _public_capability(cap: dict) -> dict:
    hidden = {"input_schema", "declared_result"}
    return {key: value for key, value in cap.items() if key not in hidden}
