"""Migrator constants."""
MAX_TERRAFORM_VERSION = "1.5.7"
DEFAULT_MANAGEMENT_ENV_NAME = "scalr-admin"
TFC_MIGRATOR_DEFAULT_SECRETS_VARSET_NAME = "Scalr-Creds"  # created in TFC by init_backend_secrets; not migrated to Scalr
DEFAULT_PC_MAP_FILE = "provider-configurations.map.json"  # written by create_provider_configurations.py
