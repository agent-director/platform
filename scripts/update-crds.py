#!/usr/bin/env python3
import urllib.request
import yaml  # type: ignore
import json
import os
import sys


def fetch_zalando_postgres_crd():
    print("Fetching Zalando PostgreSQL CRD...")
    url = "https://raw.githubusercontent.com/zalando/postgres-operator/master/charts/postgres-operator/crds/postgresqls.yaml"
    try:
        req = urllib.request.urlopen(url)
        crd = yaml.safe_load(req.read())
    except Exception as e:
        print(f"Failed to fetch CRD: {e}")
        sys.exit(1)

    for v in crd["spec"]["versions"]:
        if v["name"] == "v1":
            schema = v["schema"]["openAPIV3Schema"]
            break

    schema["required"] = ["kind", "apiVersion", "spec"]
    schema["properties"]["kind"] = {"type": "string", "enum": ["postgresql"]}
    schema["properties"]["apiVersion"] = {
        "type": "string",
        "enum": ["acid.zalan.do/v1"],
    }

    if "metadata" not in schema["properties"]:
        schema["properties"]["metadata"] = {"type": "object"}

    os.makedirs(".schemas/acid.zalan.do", exist_ok=True)
    out_path = ".schemas/acid.zalan.do/postgresql_v1.json"
    with open(out_path, "w") as f:
        json.dump(schema, f, indent=2)
    print(f"Successfully generated {out_path}")


if __name__ == "__main__":
    fetch_zalando_postgres_crd()
