"""Migrator constants."""
MAX_TERRAFORM_VERSION = "1.5.7"
DEFAULT_MANAGEMENT_ENV_NAME = "scalr-admin"
TFC_MIGRATOR_DEFAULT_SECRETS_VARSET_NAME = "Scalr-Creds"  # created in TFC by init_backend_secrets; not migrated to Scalr
DEFAULT_PC_MAP_FILE = "provider-configurations.map.json"  # written by create_provider_configurations.py
DEFAULT_INVENTORY_DIR = "migration-inventory"  # written by --inventory
INVENTORY_FORMAT_CSV = "csv"
INVENTORY_FORMAT_TSV = "tsv"
WORKSPACE_MAP_FILE = "generated-terraform/workspace-map.json"  # TFC → Scalr workspace mapping, merged across runs
LAYOUT_SINGLE = "single"
LAYOUT_PER_WORKSPACE = "per-workspace"
# Exit code of a run that completed but failed to migrate some workspaces: the post-migration
# steps still have to import what was created, so migrate.sh tells it apart from a fatal error.
EXIT_PARTIAL_FAILURE = 2
