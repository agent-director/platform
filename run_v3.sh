#!/bin/bash
make cluster-down || true
make cluster-up
./scripts/test-integration.sh platform-dev
