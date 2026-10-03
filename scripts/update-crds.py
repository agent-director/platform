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


def fetch_agent_substrate_crds():
    print("Fetching Agent Substrate CRDs...")
    version = None
    try:
        with open("images/agent-substrate/Dockerfile", "r") as f:
            for line in f:
                if line.startswith("ARG AGENT_SUBSTRATE_VERSION="):
                    version = line.strip().split("=")[1]
                    break
    except Exception as e:
        print(f"Error reading Dockerfile: {e}")
        sys.exit(1)
        
    if not version:
        print("Failed to parse Agent Substrate version from images/agent-substrate/Dockerfile! Aborting.")
        sys.exit(1)

    print(f"Detected Agent Substrate version: {version}")

    api_url = f"https://api.github.com/repos/agent-substrate/substrate/contents/manifests/ate-install/generated?ref={version}"
    try:
        req = urllib.request.Request(api_url, headers={'User-Agent': 'Mozilla/5.0'})
        response = urllib.request.urlopen(req)
        directory_contents = json.loads(response.read().decode('utf-8'))
        crds = [item['name'] for item in directory_contents if item['name'].endswith('.yaml')]
    except Exception as e:
        print(f"Failed to fetch CRD list from GitHub API: {e}")
        sys.exit(1)
    os.makedirs("charts/agent-substrate/crds", exist_ok=True)

    for crd in crds:
        url = f"https://raw.githubusercontent.com/agent-substrate/substrate/{version}/manifests/ate-install/generated/{crd}"
        try:
            req = urllib.request.urlopen(url)
            content = req.read().decode("utf-8")
            out_path = f"charts/agent-substrate/crds/{crd}"
            with open(out_path, "w") as out_f:
                out_f.write(content)
            print(f"Successfully fetched {crd}")
        except Exception as e:
            print(f"Failed to fetch {crd}: {e}")
            sys.exit(1)


if __name__ == "__main__":
    fetch_zalando_postgres_crd()
    fetch_agent_substrate_crds()
