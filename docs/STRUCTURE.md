# Repository structure

```text
mesflow-deploy-agent/
├── agent.py                  # Stable application entry point / deploy orchestration
├── VERSION.txt
├── requirements.txt
├── templates/                # Agent web UI
├── tests/                    # Deploy safety and architecture regression tests
├── docker/
│   ├── Dockerfile
│   ├── compose.windows.yml   # Docker Desktop: safe/dry-run mode
│   ├── compose.linux.yml     # Linux: full deploy mode
│   └── README.md
├── install/
│   ├── install_agent.py      # Existing installer support
│   └── linux/install_agent.sh
├── tools/                    # Maintenance/update utilities
└── docs/
    ├── DEPLOY_DOCKER.md
    ├── STRUCTURE.md
    └── history/              # Historical release/update notes
```

`agent.py` remains at repository root deliberately so existing systemd installations and tests are not broken by a cosmetic package move. Future refactoring can split web, deployment and host-operations modules behind this stable entry point after regression coverage is expanded.
