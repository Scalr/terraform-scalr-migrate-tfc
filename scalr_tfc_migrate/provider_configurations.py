"""
Create Scalr provider configurations for the cloud accounts used by TFC/E workspaces.

Provider credentials are sensitive in TFC and are never returned by its API, so they cannot
be read by a script running locally. They are available in the environment of a TFC run,
which is how the migrator already recovers sensitive shell variables: a plan is started in
TFC against the workspace configuration, and an `external` data source injected into that
configuration pushes the values to Scalr from inside the run (see
`MigrationService.migrate_sensitive_environment_variables` and templates/).

This script applies the same approach to provider configurations: the environment variables
that configure the major providers are known, so they are mapped to the equivalent Scalr
provider configuration attributes and sent to the Scalr API from inside the TFC run. Only
the provider configuration names come from a local parameters file (or --name-template),
which keeps the whole flow cloud-agnostic: no cloud provider API is ever called.

When a group of workspaces needs no sensitive value at all (everything the provider requires
is readable through the TFC API), the provider configuration is created directly and no TFC
run is started.
"""
import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

from scalr_tfc_migrate import constants, errors
from scalr_tfc_migrate.clients import ScalrClient, TFCClient
from scalr_tfc_migrate.console import ConsoleOutput
from scalr_tfc_migrate.errors import InvalidInputError
from scalr_tfc_migrate.matching import matches_workspace_patterns

DEFAULT_PARAMETERS_FILE = "provider-configuration.parameters.json"
DEFAULT_MAP_FILE = "provider-configurations.map.json"
DEFAULT_TFC_HOSTNAME = "app.terraform.io"
TEMPLATES_DIR = "./templates"

# Providers Scalr validates natively; anything else is created as a custom provider.
BUILT_IN_PROVIDERS = {"aws", "azurerm", "google", "scalr"}

INTERPOLATION_RE = re.compile(r"\$\{([^}]*)}")
# The result of the external data source, as the plan prints it. Only the lines of that one
# block are read: the rest of the plan is the customer's own configuration, and its resources
# have `name` and `id` attributes too.
REMOTE_RESULT_BLOCK_RE = re.compile(
    r'scalr_provider_configuration_result\s*=\s*{(.*?)^\s*}',
    re.MULTILINE | re.DOTALL,
)
REMOTE_RESULT_RE = re.compile(r'^\s*\+?\s*(status|message|name|id|key)\s*=\s*"(.*)"\s*$', re.MULTILINE)
# Terraform colorizes its plan output; the result of the data source has to be read through it.
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# Keys accepted in the parameters file for the cloud account identifier and the PC name.
KEY_ALIASES = ("key", "subscription_id", "subscription-id", "account_id", "account-id", "project", "project_id")
# Several identifiers can share one provider configuration (for example two access key pairs
# of the same AWS account), and an entry can be matched by workspace name instead: the AWS
# account id is not exposed by TFC, so keying by it alone would never match.
EXTRA_KEY_ALIASES = ("keys", "subscription_ids", "account_ids", "projects")
WORKSPACE_ALIASES = ("workspace", "workspaces")
NAME_ALIASES = ("name", "provider_configuration", "provider-configuration",
                "provider_configuration_name", "provider-configuration-name")

STATUS_CREATED = "created"
STATUS_UPDATED = "updated"
STATUS_EXISTING = "existing"
STATUS_WOULD_CREATE = "would-create"
STATUS_NO_PARAMETERS = "missing-parameters"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

METHOD_DIRECT = "direct"
METHOD_REMOTE = "tfc-run"


@dataclass(frozen=True)
class ProviderSpec:
    """How a provider is recognized in TFC and how its variables map to Scalr attributes."""
    name: str
    # Presence of any of these environment variables identifies the provider.
    detect: Tuple[str, ...]
    # Environment variables that identify the cloud account, in order of preference.
    key_variables: Tuple[str, ...]
    # Scalr provider configuration attribute -> TFC environment variables, first one wins.
    attribute_env: Dict[str, Tuple[str, ...]]
    # Attributes without which Scalr cannot create the provider configuration.
    required: Tuple[str, ...]
    # Environment variables meaning the workspace authenticates without static credentials.
    oidc_markers: Tuple[str, ...] = ()
    # Environment variables that cannot be migrated, with the reason.
    unsupported: Dict[str, str] = field(default_factory=dict)
    # How the TFC run can ask the cloud which account the credentials belong to, when TFC
    # itself does not store it.
    identity: Optional[str] = None


PROVIDERS: Tuple[ProviderSpec, ...] = (
    ProviderSpec(
        name="azurerm",
        detect=("ARM_CLIENT_ID", "ARM_SUBSCRIPTION_ID", "ARM_TENANT_ID", "ARM_CLIENT_SECRET"),
        key_variables=("ARM_SUBSCRIPTION_ID",),
        attribute_env={
            "azurerm-client-id": ("ARM_CLIENT_ID",),
            "azurerm-client-secret": ("ARM_CLIENT_SECRET",),
            "azurerm-tenant-id": ("ARM_TENANT_ID",),
            "azurerm-subscription-id": ("ARM_SUBSCRIPTION_ID",),
        },
        required=("azurerm-client-id", "azurerm-client-secret", "azurerm-tenant-id"),
        oidc_markers=("ARM_USE_OIDC", "ARM_OIDC_TOKEN", "ARM_OIDC_REQUEST_TOKEN", "TFC_AZURE_PROVIDER_AUTH"),
    ),
    ProviderSpec(
        name="aws",
        detect=("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_ROLE_ARN"),
        key_variables=("AWS_ACCOUNT_ID", "AWS_ROLE_ARN", "AWS_ACCESS_KEY_ID"),
        attribute_env={
            "aws-access-key": ("AWS_ACCESS_KEY_ID",),
            "aws-secret-key": ("AWS_SECRET_ACCESS_KEY",),
            "aws-role-arn": ("AWS_ROLE_ARN",),
            "aws-external-id": ("AWS_EXTERNAL_ID",),
        },
        required=("aws-access-key", "aws-secret-key"),
        oidc_markers=("AWS_WEB_IDENTITY_TOKEN_FILE", "TFC_AWS_PROVIDER_AUTH"),
        unsupported={
            "AWS_SESSION_TOKEN": "temporary session credentials expire and are not migrated",
        },
        # No TFC variable carries the AWS account id, so the run asks STS for it.
        identity="aws-sts",
    ),
    ProviderSpec(
        name="google",
        detect=("GOOGLE_CREDENTIALS", "GOOGLE_PROJECT", "GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT"),
        key_variables=("GOOGLE_PROJECT", "GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT"),
        attribute_env={
            "google-credentials": ("GOOGLE_CREDENTIALS", "GOOGLE_CLOUD_KEYFILE_JSON"),
            "google-project": ("GOOGLE_PROJECT", "GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT"),
        },
        required=("google-credentials", "google-project"),
        oidc_markers=("TFC_GCP_PROVIDER_AUTH",),
        unsupported={
            "GOOGLE_APPLICATION_CREDENTIALS": "points to a file path in the run environment, not to the key itself",
        },
    ),
)

PROVIDERS_BY_NAME = {spec.name: spec for spec in PROVIDERS}


def static_attributes(spec: ProviderSpec, present: Set[str]) -> Dict[str, str]:
    """Attributes derived from which variables a workspace defines, not from their values."""
    if spec.name == "azurerm":
        if "ARM_CLIENT_SECRET" in present:
            return {"azurerm-auth-type": "client-secrets"}
        return {}
    if spec.name == "aws":
        # aws-account-type is required by the API; the parameters file can override it with
        # gov-cloud or cn-cloud.
        account_type = {"aws-account-type": "regular"}
        if {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"} <= present:
            return {"aws-credentials-type": "access_keys", **account_type}
        if "AWS_ROLE_ARN" in present:
            return {"aws-credentials-type": "role_delegation", "aws-trusted-entity-type": "aws_account",
                    **account_type}
        return account_type
    if spec.name == "google":
        if "GOOGLE_CREDENTIALS" in present or "GOOGLE_CLOUD_KEYFILE_JSON" in present:
            return {"google-auth-type": "service-account-key"}
        return {}
    return {}


def required_attributes(spec: ProviderSpec, present: Set[str]) -> Tuple[str, ...]:
    """What Scalr needs depends on the credentials type the workspace uses."""
    if spec.name == "aws":
        if {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"} <= present:
            return "aws-access-key", "aws-secret-key"
        if "AWS_ROLE_ARN" in present:
            return ("aws-role-arn",)
    return spec.required


def normalize_key(key: str) -> str:
    """Cloud account identifiers are compared case-insensitively and without padding."""
    return key.strip().lower()


@dataclass
class ProviderConfigurationArgs:
    scalr_hostname: str
    scalr_token: str
    tfc_hostname: str
    tfc_token: str
    tfc_organization: str
    key_variable: Optional[str] = None
    provider: Optional[str] = None
    parameters_file: str = DEFAULT_PARAMETERS_FILE
    name_template: Optional[str] = None
    map_file: str = DEFAULT_MAP_FILE
    workspaces: str = "*"
    scalr_environment: Optional[str] = None
    tfc_project: Optional[str] = None
    skip_variable_sets: bool = False
    skip_remote_runs: bool = False
    skip_backend_secrets: bool = False
    update_existing: bool = False
    include_unused: bool = False
    fail_on_unresolved: bool = False
    dry_run: bool = False
    terraform_binary: str = "terraform"
    credentials_set_name: str = constants.TFC_MIGRATOR_DEFAULT_SECRETS_VARSET_NAME
    account_id: Optional[str] = None

    @classmethod
    def from_argparse(cls, args: argparse.Namespace) -> 'ProviderConfigurationArgs':
        return cls(
            scalr_hostname=args.scalr_hostname,
            scalr_token=args.scalr_token,
            tfc_hostname=args.tfc_hostname,
            tfc_token=args.tfc_token,
            tfc_organization=args.tfc_organization,
            key_variable=args.key_variable,
            provider=args.provider,
            parameters_file=args.parameters_file,
            name_template=args.name_template,
            map_file=args.map_file,
            workspaces=args.workspaces or "*",
            scalr_environment=args.scalr_environment,
            tfc_project=args.tfc_project,
            skip_variable_sets=args.skip_variable_sets,
            skip_remote_runs=args.skip_remote_runs,
            skip_backend_secrets=args.skip_backend_secrets,
            update_existing=args.update_existing,
            include_unused=args.include_unused,
            fail_on_unresolved=args.fail_on_unresolved,
            dry_run=args.dry_run,
            terraform_binary=args.terraform_binary,
            credentials_set_name=args.credentials_set_name or constants.TFC_MIGRATOR_DEFAULT_SECRETS_VARSET_NAME,
        )


@dataclass
class ProviderConfigurationEntry:
    """A single parameters-file entry, already merged with the file defaults."""
    key: str
    name: str
    workspaces: List[str] = field(default_factory=list)
    provider_name: Optional[str] = None
    attributes: Dict[str, str] = field(default_factory=dict)
    parameters: List[Dict] = field(default_factory=list)
    environments: List[str] = field(default_factory=list)
    is_shared: Optional[bool] = None
    export_shell_variables: Optional[bool] = None
    is_custom: Optional[bool] = None


@dataclass
class TFCVariable:
    value: Optional[str]
    sensitive: bool
    source: str
    # Variables of a priority variable set override the workspace's own variables.
    priority: bool = False
    # Where this variable lives in TFC, so the migration can skip what became a provider
    # configuration instead of copying the same credentials into Scalr a second time.
    id: Optional[str] = None
    var_set_id: Optional[str] = None
    var_set_name: Optional[str] = None


@dataclass
class WorkspaceInfo:
    tf_workspace: Dict
    name: str
    env_variables: Dict[str, TFCVariable]
    terraform_variables: Dict[str, TFCVariable]
    provider: Optional[ProviderSpec] = None
    key_variable: Optional[str] = None
    key_value: Optional[str] = None
    reason: Optional[str] = None


@dataclass
class ConfigurationGroup:
    group_id: str
    provider: ProviderSpec
    workspaces: List[WorkspaceInfo]
    key_variable: Optional[str] = None
    key_value: Optional[str] = None


@dataclass
class VariableSetCandidate:
    """A TFC variable set, with the scope it applies to and the variables it defines."""
    name: str
    is_global: bool
    variables: Dict[str, TFCVariable]
    raw: Dict = field(default_factory=dict)
    workspace_ids: Set[str] = field(default_factory=set)
    project_ids: Set[str] = field(default_factory=set)


class ProviderConfigurationService:

    def __init__(self, args: ProviderConfigurationArgs):
        self.args = args
        self.tfc: TFCClient = TFCClient(args.tfc_hostname, args.tfc_token)
        self.scalr: ScalrClient = ScalrClient(args.scalr_hostname, args.scalr_token)
        self.entries: Dict[str, ProviderConfigurationEntry] = {}
        self.entries_by_workspace: Dict[str, ProviderConfigurationEntry] = {}
        # Entries whose identifier no workspace matched: only those can still be matched by
        # an identity that the TFC run resolves from the cloud itself.
        self.unmatched_entries: List[ProviderConfigurationEntry] = []
        self.environment_cache: Dict[str, str] = {}
        self.variable_sets: Optional[List[VariableSetCandidate]] = None
        self.variable_set_vars_cache: Dict[str, Dict[str, TFCVariable]] = {}
        self.workspace_varsets_supported = True
        self.backend_secrets_ready = False
        # Provider configuration names already handled by this run: several cloud accounts
        # may be mapped to one provider configuration, and it must be written only once.
        self.handled_names: Dict[str, Dict] = {}

        self.load_account_id()

    def load_account_id(self) -> None:
        accounts = self.scalr.get("accounts")["data"]
        if not accounts:
            raise InvalidInputError("No account is associated with the given Scalr token.")
        if len(accounts) > 1:
            raise InvalidInputError("The token is associated with more than 1 account.")
        self.args.account_id = accounts[0]["id"]

    # --- parameters file -------------------------------------------------

    def interpolate(self, value, key: str, context: str):
        """Resolve ${key} and ${env:VAR} placeholders in a parameters-file value."""
        if isinstance(value, dict):
            return {k: self.interpolate(v, key, context) for k, v in value.items()}
        if isinstance(value, list):
            return [self.interpolate(v, key, context) for v in value]
        if not isinstance(value, str):
            return value

        def replace(match: re.Match) -> str:
            token = match.group(1).strip()
            if token == "key":
                return key
            if token.startswith("env:"):
                env_name = token[len("env:"):].strip()
                if env_name not in os.environ:
                    raise InvalidInputError(
                        f"{context}: environment variable '{env_name}' is not set, "
                        f"but is referenced as '${{env:{env_name}}}'"
                    )
                return os.environ[env_name]
            raise InvalidInputError(
                f"{context}: unknown placeholder '${{{token}}}'. Supported: ${{key}}, ${{env:VAR}}"
            )

        return INTERPOLATION_RE.sub(replace, value)

    @staticmethod
    def _as_list(value) -> List:
        if value is None:
            return []
        return value if isinstance(value, list) else [value]

    @staticmethod
    def _pick(entry: Dict, aliases: Tuple[str, ...]):
        for alias in aliases:
            if entry.get(alias):
                return entry[alias]
        return None

    # Names that look right but are not what the Scalr API calls these attributes.
    ATTRIBUTE_ALIASES = {
        "aws-access-key-id": "aws-access-key",
        "aws-secret-access-key": "aws-secret-key",
        "google-credentials-json": "google-credentials",
        "azurerm-subscription": "azurerm-subscription-id",
    }

    @classmethod
    def _normalize_attributes(cls, attributes: Dict) -> Dict:
        """Accept both API names (azurerm-client-id) and snake_case (azurerm_client_id)."""
        normalized = {}
        for key, value in (attributes or {}).items():
            name = str(key).replace("_", "-")
            if name in cls.ATTRIBUTE_ALIASES:
                ConsoleOutput.warning(
                    f"Attribute '{key}' is called '{cls.ATTRIBUTE_ALIASES[name]}' in the Scalr API, using that"
                )
                name = cls.ATTRIBUTE_ALIASES[name]
            normalized[name] = value
        return normalized

    @staticmethod
    def _merge_parameters(defaults: List[Dict], overrides: List[Dict]) -> List[Dict]:
        merged: Dict[str, Dict] = {p["key"]: p for p in defaults if p.get("key")}
        for parameter in overrides:
            if not parameter.get("key"):
                raise InvalidInputError("Every custom provider parameter requires a 'key'")
            merged[parameter["key"]] = parameter
        return list(merged.values())

    def load_parameters(self) -> Dict[str, ProviderConfigurationEntry]:
        path = self.args.parameters_file
        if not os.path.exists(path):
            if self.args.name_template:
                ConsoleOutput.info(
                    f"No parameters file at '{path}', naming provider configurations with "
                    f"'{self.args.name_template}'"
                )
                return {}
            raise InvalidInputError(
                f"Parameters file '{path}' does not exist. Provide one, or pass --name-template "
                f"to derive the provider configuration names from the cloud account identifier."
            )

        try:
            with open(path, 'r') as f:
                document = json.load(f)
        except json.JSONDecodeError as e:
            raise InvalidInputError(f"Parameters file '{path}' is not valid JSON: {e}")

        if isinstance(document, list):
            defaults: Dict = {}
            raw_entries: List = document
        elif isinstance(document, dict):
            defaults = document.get("defaults") or {}
            raw_entries = (
                document.get("provider_configurations")
                or document.get("provider-configurations")
                or document.get("configurations")
                or []
            )
        else:
            raise InvalidInputError(
                f"Parameters file '{path}' must contain a list of entries or an object with "
                f"'defaults' and 'provider_configurations'"
            )

        if not raw_entries:
            raise InvalidInputError(f"Parameters file '{path}' does not contain any provider configuration entry")

        default_attributes = self._normalize_attributes(defaults.get("attributes"))
        default_parameters = defaults.get("parameters") or []

        entries: Dict[str, ProviderConfigurationEntry] = {}
        for index, raw in enumerate(raw_entries):
            if not isinstance(raw, dict):
                raise InvalidInputError(f"Entry #{index + 1} in '{path}' is not an object")

            key = self._pick(raw, KEY_ALIASES)
            name = self._pick(raw, NAME_ALIASES)
            extra_keys = self._as_list(self._pick(raw, EXTRA_KEY_ALIASES))
            workspace_names = self._as_list(self._pick(raw, WORKSPACE_ALIASES))

            if not key and not extra_keys and not workspace_names:
                raise InvalidInputError(
                    f"Entry #{index + 1} in '{path}' matches nothing: give it a cloud account identifier "
                    f"({', '.join(KEY_ALIASES)}) or the workspaces it applies to ({', '.join(WORKSPACE_ALIASES)})"
                )
            if not name:
                raise InvalidInputError(
                    f"Entry '{key or workspace_names[0]}' in '{path}' has no provider configuration name "
                    f"(one of: {', '.join(NAME_ALIASES)})"
                )

            all_keys = [str(k) for k in ([key] if key else []) + extra_keys]
            normalized = normalize_key(all_keys[0]) if all_keys else f"workspace:{normalize_key(workspace_names[0])}"
            for candidate_key in all_keys:
                if normalize_key(candidate_key) in entries:
                    raise InvalidInputError(
                        f"Duplicate entry for '{candidate_key}' in '{path}': "
                        f"'{entries[normalize_key(candidate_key)].name}' and '{name}'"
                    )

            def inherited(field_name: str, dashed: str):
                for source in (raw, defaults):
                    if field_name in source:
                        return source[field_name]
                    if dashed in source:
                        return source[dashed]
                return None

            entries[normalized] = ProviderConfigurationEntry(
                key=str(key).strip() if key else "",
                name=str(name).strip(),
                workspaces=[str(w).strip() for w in workspace_names],
                provider_name=inherited("provider_name", "provider-name"),
                attributes={**default_attributes, **self._normalize_attributes(raw.get("attributes"))},
                parameters=self._merge_parameters(default_parameters, raw.get("parameters") or []),
                environments=inherited("environments", "environments") or [],
                is_shared=inherited("is_shared", "is-shared"),
                export_shell_variables=inherited("export_shell_variables", "export-shell-variables"),
                is_custom=inherited("is_custom", "is-custom"),
            )

            # The same entry answers to every identifier it declares, and to its workspaces.
            for candidate_key in all_keys[1:]:
                entries[normalize_key(candidate_key)] = entries[normalized]
            for workspace_name in workspace_names:
                self.entries_by_workspace[normalize_key(str(workspace_name))] = entries[normalized]

        ConsoleOutput.info(
            f"Loaded {len(set(id(e) for e in entries.values()))} provider configuration entr(ies) from '{path}'"
        )
        return entries

    # --- TFC discovery ---------------------------------------------------

    def get_project_id(self) -> Optional[str]:
        if not self.args.tfc_project:
            return None

        project = self.tfc.get_project(self.args.tfc_organization, self.args.tfc_project)
        if not project:
            raise InvalidInputError(
                f"Project '{self.args.tfc_project}' not found in organization '{self.args.tfc_organization}'"
            )
        ConsoleOutput.info(f"Filtering TFC workspaces by project: '{self.args.tfc_project}'")
        return project["id"]

    @staticmethod
    def _index_variables(variables: List[Dict], source: str, priority: bool = False,
                         var_set: Optional[Dict] = None
                         ) -> Tuple[Dict[str, TFCVariable], Dict[str, TFCVariable]]:
        env_variables: Dict[str, TFCVariable] = {}
        terraform_variables: Dict[str, TFCVariable] = {}
        for variable in variables:
            attributes = variable.get("attributes") or {}
            key = attributes.get("key")
            if not key:
                continue
            entry = TFCVariable(
                value=attributes.get("value"),
                sensitive=bool(attributes.get("sensitive")),
                source=source,
                priority=priority,
                id=variable.get("id"),
                var_set_id=var_set["id"] if var_set else None,
                var_set_name=(var_set.get("attributes") or {}).get("name") if var_set else None,
            )
            if attributes.get("category") == "env":
                env_variables[key] = entry
            else:
                terraform_variables[key] = entry
        return env_variables, terraform_variables

    def list_all_variable_set_vars(self, varset_id: str) -> List[Dict]:
        page = 1
        variables: List[Dict] = []
        while True:
            response = self.tfc.get_variable_set_vars(varset_id, page)
            variables.extend(response.get("data", []))
            next_page = response.get("meta", {}).get("pagination", {}).get("next-page")
            if not next_page:
                break
            page = next_page
        return variables

    @staticmethod
    def _relationship_ids(document: Dict, relationship_key: str) -> Set[str]:
        rel = (document.get("relationships") or {}).get(relationship_key) or {}
        data = rel.get("data")
        if data is None:
            return set()
        if isinstance(data, dict):
            return {data["id"]} if data.get("id") else set()
        return {item["id"] for item in data if item.get("id")}

    def get_variable_sets(self) -> List[VariableSetCandidate]:
        """TFC variable sets of the organization, with their variables and their scope."""
        if self.variable_sets is not None:
            return self.variable_sets

        candidates: List[VariableSetCandidate] = []
        page = 1
        while True:
            response = self.tfc.list_variable_sets(self.args.tfc_organization, page)
            for var_set in response.get("data", []):
                attributes = var_set.get("attributes") or {}
                name = attributes.get("name", var_set["id"])
                try:
                    variables = self.list_all_variable_set_vars(var_set["id"])
                except errors.APIError as e:
                    ConsoleOutput.warning(f"Could not read variables of TFC variable set '{name}': {e}")
                    continue

                env_variables, _ = self._index_variables(variables, f"variable set '{name}'")
                if not env_variables:
                    continue

                is_global = bool(attributes.get("global", False))
                workspace_ids: Set[str] = set()
                project_ids: Set[str] = set()

                if not is_global:
                    try:
                        detail = self.tfc.get_variable_set(var_set["id"], {"include": "workspaces,projects"})
                        workspace_ids = self._relationship_ids(detail, "workspaces")
                        project_ids = self._relationship_ids(detail, "projects")
                    except errors.APIError as e:
                        ConsoleOutput.warning(
                            f"Could not resolve the scope of TFC variable set '{name}', it will be ignored: {e}"
                        )
                        continue

                candidates.append(VariableSetCandidate(
                    name=name,
                    is_global=is_global,
                    variables=env_variables,
                    raw=var_set,
                    workspace_ids=workspace_ids,
                    project_ids=project_ids,
                ))

            next_page = response.get("meta", {}).get("pagination", {}).get("next-page")
            if not next_page:
                break
            page = next_page

        self.variable_sets = candidates
        return candidates

    def variable_set_vars(self, var_set: Dict) -> Dict[str, TFCVariable]:
        """Environment variables of one variable set, read once per set."""
        var_set_id = var_set["id"]
        if var_set_id not in self.variable_set_vars_cache:
            attributes = var_set.get("attributes") or {}
            name = attributes.get("name", var_set_id)
            try:
                variables = self.list_all_variable_set_vars(var_set_id)
            except errors.APIError as e:
                ConsoleOutput.warning(f"Could not read variables of TFC variable set '{name}': {e}")
                variables = []
            env_variables, _ = self._index_variables(
                variables, f"variable set '{name}'", priority=bool(attributes.get("priority")),
                var_set=var_set,
            )
            self.variable_set_vars_cache[var_set_id] = env_variables
        return self.variable_set_vars_cache[var_set_id]

    def workspace_variable_sets(self, tf_workspace: Dict) -> Optional[List[Dict]]:
        """
        The variable sets TFC itself reports as applied to the workspace, which is the only
        answer that also covers global sets and sets attached to the workspace's project.
        Returns None when the TFC/E in use does not expose that route, so that the caller
        can fall back to the organization-wide listing.
        """
        if not self.workspace_varsets_supported:
            return None

        var_sets: List[Dict] = []
        page = 1
        while True:
            try:
                response = self.tfc.get_workspace_variable_sets(tf_workspace["id"], page)
            except errors.APIError as e:
                ConsoleOutput.warning(
                    f"Cannot list the variable sets of a workspace on this TFC/E ({e}), falling back "
                    f"to the organization-wide listing"
                )
                self.workspace_varsets_supported = False
                return None

            var_sets.extend(response.get("data", []))
            next_page = response.get("meta", {}).get("pagination", {}).get("next-page")
            if not next_page:
                break
            page = next_page

        return var_sets

    def _var_set_scope(self, var_set: Dict, workspace_id: str, project_id: Optional[str]) -> int:
        """0 global, 1 project, 2 workspace: a more specific variable set wins."""
        attributes = var_set.get("attributes") or {}
        if attributes.get("global"):
            return 0
        if workspace_id in self._relationship_ids(var_set, "workspaces"):
            return 2
        if project_id and project_id in self._relationship_ids(var_set, "projects"):
            return 1
        # Applied to the workspace, but the payload does not say through which scope.
        return 1

    def variable_set_variables(self, tf_workspace: Dict) -> Tuple[Dict[str, TFCVariable], List[str]]:
        """Variables a workspace inherits from variable sets, and the names of those sets."""
        if self.args.skip_variable_sets:
            return {}, []

        workspace_id = tf_workspace["id"]
        project_id = None
        project_rel = (tf_workspace.get("relationships") or {}).get("project") or {}
        if project_rel.get("data"):
            project_id = project_rel["data"].get("id")

        applied = self.workspace_variable_sets(tf_workspace)

        if applied is None:
            # Fallback: scan the organization and match the scopes locally.
            applied = []
            for candidate in self.get_variable_sets():
                if (candidate.is_global
                        or workspace_id in candidate.workspace_ids
                        or (project_id and project_id in candidate.project_ids)):
                    applied.append(candidate.raw)

        # Least specific first, and priority sets last so that they win.
        applied.sort(key=lambda v: (self._var_set_scope(v, workspace_id, project_id),
                                    bool((v.get("attributes") or {}).get("priority"))))

        inherited: Dict[str, TFCVariable] = {}
        names: List[str] = []
        for var_set in applied:
            names.append((var_set.get("attributes") or {}).get("name", var_set["id"]))
            inherited.update(self.variable_set_vars(var_set))

        return inherited, names

    def detect_provider(self, env_variables: Dict[str, TFCVariable]) -> Optional[ProviderSpec]:
        if self.args.provider:
            spec = PROVIDERS_BY_NAME.get(self.args.provider)
            if not spec:
                raise InvalidInputError(
                    f"Unknown provider '{self.args.provider}'. Known: {', '.join(PROVIDERS_BY_NAME)}"
                )
            return spec

        for spec in PROVIDERS:
            if any(name in env_variables for name in spec.detect):
                return spec
        return None

    def resolve_key(self, spec: ProviderSpec, info: WorkspaceInfo) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """Return (key variable, key value, reason the value is unknown)."""
        candidates = (self.args.key_variable,) if self.args.key_variable else spec.key_variables

        for key_variable in candidates:
            variable = info.env_variables.get(key_variable) or info.terraform_variables.get(key_variable)
            if not variable:
                continue
            if variable.value:
                return key_variable, variable.value.strip(), None
            if variable.sensitive:
                # Unknown here, but the TFC run can still read it and resolve the name itself.
                return key_variable, None, f"'{key_variable}' is sensitive in TFC"
        return None, None, f"none of {', '.join(candidates)} is defined"

    def collect_workspaces(self) -> Tuple[List[WorkspaceInfo], List[Dict]]:
        ConsoleOutput.section("Inspecting TFC workspaces")

        project_id = self.get_project_id()
        collected: List[WorkspaceInfo] = []
        skipped: List[Dict] = []
        next_page = 1

        while True:
            response = self.tfc.get_workspaces(self.args.tfc_organization, next_page, project_id=project_id)
            next_page = response["meta"]["pagination"]["next-page"]

            for tf_workspace in response["data"]:
                workspace_name = tf_workspace["attributes"]["name"]
                if not matches_workspace_patterns(workspace_name, self.args.workspaces):
                    continue

                workspace_vars = self.tfc.get_workspace_vars(
                    self.args.tfc_organization, workspace_name
                ).get("data", [])
                own_env, own_terraform = self._index_variables(workspace_vars, "workspace")

                inherited, var_set_names = self.variable_set_variables(tf_workspace)
                env_variables = dict(inherited)
                env_variables.update(own_env)
                # A priority variable set overrides the variables of the workspace itself.
                env_variables.update({k: v for k, v in inherited.items() if v.priority})

                if os.getenv("SCALR_DEBUG_ENABLED"):
                    ConsoleOutput.debug(
                        f"'{workspace_name}': {len(own_env)} workspace environment variable(s), "
                        f"{len(inherited)} from variable set(s) {var_set_names or '[]'}: "
                        f"{sorted(env_variables)}"
                    )

                info = WorkspaceInfo(
                    tf_workspace=tf_workspace,
                    name=workspace_name,
                    env_variables=env_variables,
                    terraform_variables=own_terraform,
                )

                spec = self.detect_provider(env_variables)
                if not spec:
                    source = f"{len(own_env)} workspace environment variable(s)"
                    if var_set_names:
                        source += (f" and {len(inherited)} from variable set(s) "
                                   f"{', '.join(repr(n) for n in var_set_names)}")
                    elif not self.args.skip_variable_sets:
                        source += " and no variable set applied to it"
                    info.reason = f"no known provider credentials found: {source}"
                    skipped.append({"workspace": workspace_name, "reason": info.reason})
                    continue

                # A credential the workspace does not define at all is not readable inside a
                # TFC run either, so those workspaces are reported instead of failing in a run.
                present = set(env_variables)
                undefined = [a for a in required_attributes(spec, present)
                             if not any(e in present for e in spec.attribute_env[a])]
                if undefined:
                    oidc = [marker for marker in spec.oidc_markers if marker in env_variables]
                    if oidc:
                        info.reason = (
                            f"authenticates with dynamic credentials ({', '.join(oidc)}); there is no static "
                            f"credential to copy, configure OIDC on the Scalr provider configuration instead"
                        )
                    else:
                        info.reason = (
                            f"no variable provides {', '.join(undefined)} for the {spec.name} provider "
                            f"configuration"
                        )
                    skipped.append({"workspace": workspace_name, "reason": info.reason})
                    continue

                for variable, why in spec.unsupported.items():
                    if variable in env_variables:
                        ConsoleOutput.warning(f"Workspace '{workspace_name}': {variable} is ignored, {why}")

                info.provider = spec
                info.key_variable, info.key_value, reason = self.resolve_key(spec, info)
                if not info.key_variable:
                    info.reason = f"{spec.name} credentials found, but {reason}"
                    skipped.append({"workspace": workspace_name, "reason": info.reason})
                    continue

                collected.append(info)

            if not next_page:
                break

        ConsoleOutput.info(
            f"{len(collected)} workspace(s) with recognized provider credentials, {len(skipped)} skipped"
        )
        for item in skipped:
            ConsoleOutput.warning(f"Workspace '{item['workspace']}': {item['reason']}")

        return collected, skipped

    def build_groups(self, workspaces: List[WorkspaceInfo]) -> List[ConfigurationGroup]:
        """
        One provider configuration per cloud account. Workspaces whose account identifier is
        sensitive are handled one by one: only the TFC run can read the value.
        """
        groups: Dict[str, ConfigurationGroup] = {}

        for info in workspaces:
            if info.key_value:
                group_id = f"{info.provider.name}:{normalize_key(info.key_value)}"
            else:
                group_id = f"{info.provider.name}:workspace:{info.name}"

            if group_id not in groups:
                groups[group_id] = ConfigurationGroup(
                    group_id=group_id,
                    provider=info.provider,
                    workspaces=[],
                    key_variable=info.key_variable,
                    key_value=info.key_value,
                )
            groups[group_id].workspaces.append(info)

        return list(groups.values())

    # --- Scalr -----------------------------------------------------------

    def get_environment_id(self, name: str, create: bool = True) -> Optional[str]:
        if name not in self.environment_cache:
            environment = self.scalr.get_environment(name)
            if not environment and not create:
                return None
            if not environment:
                # The environment is usually created by the migration itself, which may not have
                # run yet; creating it here lets a provider configuration be scoped to it now.
                ConsoleOutput.info(f"Creating Scalr environment '{name}' to scope provider configurations to it")
                environment = self.scalr.create_environment(name, self.args.account_id)["data"]
            self.environment_cache[name] = environment["id"]
        return self.environment_cache[name]

    def find_provider_configuration(self, name: str) -> Optional[Dict]:
        configurations = self.scalr.get_provider_configurations(name=name).get("data", [])
        for configuration in configurations:
            if configuration["attributes"]["name"] == name:
                return configuration
        return None

    def entry_for(self, group: ConfigurationGroup) -> Optional[ProviderConfigurationEntry]:
        if group.key_value:
            entry = self.entries.get(normalize_key(group.key_value))
            if entry:
                return entry
        for info in group.workspaces:
            entry = self.entries_by_workspace.get(normalize_key(info.name))
            if entry:
                return entry
        return None

    @staticmethod
    def entry_key(group: ConfigurationGroup, entry: Optional[ProviderConfigurationEntry]) -> str:
        """What ${key} resolves to: the identifier found in TFC, or the one the entry declares."""
        return group.key_value or (entry.key if entry else "")

    def resolve_name(self, group: ConfigurationGroup) -> Optional[str]:
        entry = self.entry_for(group)
        if entry:
            return self.interpolate(entry.name, self.entry_key(group, entry), f"entry '{entry.name}'")
        if group.key_value and self.args.name_template:
            return self.args.name_template.format(provider=group.provider.name, key=group.key_value)
        return None

    def flags_and_environments(self, entry: Optional[ProviderConfigurationEntry],
                               key: Optional[str] = None,
                               create_environments: bool = True) -> Tuple[Dict, List[str]]:
        """Sharing, shell variable export and environment access of a provider configuration."""
        # Least privilege: a provider configuration nobody asked to share reaches no environment
        # until one is named here, or until a migration links it to a workspace and grants its
        # own environment access.
        flags: Dict = {"is-shared": False, "export-shell-variables": True}
        environment_ids: List[str] = []

        environments = []
        if entry:
            environments = self.interpolate(entry.environments, key or entry.key, f"entry '{entry.name}'")
        if not environments and self.args.scalr_environment:
            # Least privilege: reachable from the environment the workspaces are migrated into,
            # and from nowhere else. The migration grants further environments when it needs them.
            environments = [self.args.scalr_environment]

        if environments == ["*"]:
            flags["is-shared"] = True
        elif environments:
            resolved = [(name, self.get_environment_id(name, create=create_environments))
                        for name in environments]
            for name, environment_id in resolved:
                if environment_id is None:
                    ConsoleOutput.warning(
                        f"Scalr environment '{name}' does not exist yet; it is granted access when a "
                        f"migration into it links this provider configuration"
                    )
            environment_ids = [environment_id for _, environment_id in resolved if environment_id]

        if not entry:
            return flags, environment_ids

        if entry.is_shared is not None:
            flags["is-shared"] = entry.is_shared
        if entry.export_shell_variables is not None:
            flags["export-shell-variables"] = entry.export_shell_variables
        if entry.is_custom is not None:
            flags["is-custom"] = entry.is_custom

        return flags, environment_ids

    def local_attributes(self, group: ConfigurationGroup, entry: Optional[ProviderConfigurationEntry]) -> Dict:
        """Provider attributes whose values are readable through the TFC API."""
        attributes: Dict = {}
        variables = group.workspaces[0].env_variables

        for attribute, env_names in group.provider.attribute_env.items():
            for env_name in env_names:
                variable = variables.get(env_name)
                if variable and variable.value:
                    attributes[attribute] = variable.value
                    break

        attributes.update(static_attributes(group.provider, set(variables)))

        if entry:
            attributes.update(self.interpolate(
                entry.attributes, self.entry_key(group, entry), f"entry '{entry.name}'"
            ))

        return attributes

    # --- creation --------------------------------------------------------

    def create_directly(self, group: ConfigurationGroup, name: str, existing: Optional[Dict]) -> Dict:
        entry = self.entry_for(group)
        flags, environment_ids = self.flags_and_environments(entry, self.entry_key(group, entry))
        attributes = {
            "name": name,
            "provider-name": group.provider.name,
            **self.local_attributes(group, entry),
            **flags,
        }

        if existing:
            self.scalr.update_provider_configuration(existing["id"], attributes)
            ConsoleOutput.success(f"Updated provider configuration '{name}' ({existing['id']})")
            return {"name": name, "id": existing["id"], "status": STATUS_UPDATED, "method": METHOD_DIRECT}

        response = self.scalr.create_provider_configuration(self.args.account_id, attributes, environment_ids or None)
        pc_id = response["data"]["id"]

        for parameter in self.interpolate(entry.parameters if entry else [], group.key_value or "",
                                          f"group '{group.group_id}'"):
            self.scalr.create_provider_configuration_parameter(pc_id, parameter)

        ConsoleOutput.success(
            f"Created provider configuration '{name}' ({pc_id}) from values readable in TFC"
        )
        return {"name": name, "id": pc_id, "status": STATUS_CREATED, "method": METHOD_DIRECT}

    def ensure_backend_secrets(self) -> None:
        """The TFC run authenticates against Scalr with the credentials of this variable set."""
        if self.backend_secrets_ready:
            return
        if self.args.skip_backend_secrets:
            ConsoleOutput.info(
                f"Assuming the TFC variable set '{self.args.credentials_set_name}' with the Scalr "
                f"credentials already exists"
            )
        else:
            ConsoleOutput.info(
                f"Creating the TFC variable set '{self.args.credentials_set_name}' so that TFC runs "
                f"can reach the Scalr API"
            )
            self.tfc.init_backend_secrets(self.args)
        self.backend_secrets_ready = True

    def build_payload(self, group: ConfigurationGroup, name: Optional[str],
                      vehicle: Optional[WorkspaceInfo] = None) -> Dict:
        entry = self.entry_for(group)
        flags, environment_ids = self.flags_and_environments(entry, self.entry_key(group, entry))
        # The credentials type and the required attributes have to describe the workspace the
        # run happens in, which is not always the first of the group.
        present = set((vehicle or group.workspaces[0]).env_variables)

        return {
            "account_id": self.args.account_id,
            "provider_name": group.provider.name,
            "attribute_env": {a: list(e) for a, e in group.provider.attribute_env.items()},
            "static_attributes": static_attributes(group.provider, present),
            "extra_attributes": self.interpolate(
                entry.attributes if entry else {}, self.entry_key(group, entry), f"group '{group.group_id}'"
            ),
            "parameters": self.interpolate(
                entry.parameters if entry else [], self.entry_key(group, entry), f"group '{group.group_id}'"
            ),
            "required": list(required_attributes(group.provider, present)),
            "key_env": group.key_variable,
            "resolve_identity": group.provider.identity,
            "name": name,
            "entries": self.build_entry_map(),
            "name_template": self.args.name_template,
            "environment_ids": environment_ids,
            "is-shared": flags.get("is-shared"),
            "export-shell-variables": flags.get("export-shell-variables"),
            "is-custom": flags.get("is-custom"),
            "update_existing": self.args.update_existing,
        }

    def build_entry_map(self) -> Dict[str, Dict]:
        """
        Everything the run needs about the entries it may match: when the cloud identity is
        only known inside the run, the entry is picked there, so its attributes, parameters
        and environments have to travel with it - not just its name.
        """
        entry_map: Dict[str, Dict] = {}
        for key, entry in self.entries.items():
            try:
                # An entry here may never be matched; resolving it must not create environments.
                flags, environment_ids = self.flags_and_environments(entry, create_environments=False)
            except InvalidInputError as e:
                ConsoleOutput.warning(f"Entry '{entry.name}': {e}, its environments are ignored")
                flags, environment_ids = {"is-shared": False, "export-shell-variables": True}, []

            entry_map[key] = {
                "name": self.interpolate(entry.name, entry.key, f"entry '{entry.name}'"),
                "attributes": self.interpolate(entry.attributes, entry.key, f"entry '{entry.name}'"),
                "parameters": self.interpolate(entry.parameters, entry.key, f"entry '{entry.name}'"),
                "environment_ids": environment_ids,
                "is-shared": flags.get("is-shared"),
                "export-shell-variables": flags.get("export-shell-variables"),
                "is-custom": flags.get("is-custom"),
            }
        return entry_map

    def terraform_env(self) -> Dict[str, str]:
        environment = dict(os.environ)
        environment["TF_IN_AUTOMATION"] = "1"
        # Authenticate against TFC with the token this script was given, instead of relying
        # on the credentials file of whoever runs it.
        token_variable = "TF_TOKEN_" + self.args.tfc_hostname.replace("-", "__").replace(".", "_")
        environment.setdefault(token_variable, self.args.tfc_token)
        return environment

    def run_terraform(self, working_directory: str, arguments: List[str], log_path: str) -> Tuple[int, str]:
        with open(log_path, "a") as log_file:
            log_file.write(f"\n$ {' '.join(arguments[:2])}\n")
            log_file.flush()
            process = subprocess.run(
                arguments,
                cwd=working_directory,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=self.terraform_env(),
                text=True,
            )

        with open(log_path, "r") as log_file:
            return process.returncode, log_file.read()

    @staticmethod
    def parse_remote_result(output: str) -> Dict[str, str]:
        """Read back what the in-run script reported through the external data source."""
        plain = ANSI_RE.sub("", output)
        block = REMOTE_RESULT_BLOCK_RE.search(plain)
        if not block:
            return {}

        result: Dict[str, str] = {}
        for field_name, value in REMOTE_RESULT_RE.findall(block.group(1)):
            result.setdefault(field_name, value)
        return result

    def create_through_tfc_run(self, group: ConfigurationGroup, name: Optional[str]) -> Dict:
        """
        Start a plan in TFC against one workspace of the group. The plan executes the in-run
        script, which reads the provider credentials from the run environment - the only place
        they are readable - and creates the provider configuration in Scalr.
        """
        label = name or f"the {group.provider.name} account of '{group.workspaces[0].name}'"
        ConsoleOutput.info(
            f"Credentials for {label} are sensitive in TFC, starting a TFC run to migrate them"
        )

        self.ensure_backend_secrets()

        working_directory = None
        vehicle = None
        for info in group.workspaces:
            working_directory = self.tfc.get_current_cv(info.tf_workspace)
            if working_directory:
                vehicle = info
                break
            ConsoleOutput.warning(
                f"Workspace '{info.name}' has no downloadable configuration version, trying the next one"
            )

        if not working_directory:
            message = "no workspace of this group has a downloadable configuration version"
            ConsoleOutput.error(f"Cannot migrate {label}: {message}")
            return {"name": name, "id": None, "status": STATUS_FAILED, "method": METHOD_REMOTE, "message": message}

        backend_config = f'''terraform {{
  cloud {{
    organization = "{self.args.tfc_organization}"
    workspaces {{
      name = "{vehicle.name}"
    }}
  }}
}}
'''
        with open(os.path.join(working_directory, "scalr_backend_override.tf"), "w") as f:
            f.write(backend_config)

        shutil.copy2(os.path.join(TEMPLATES_DIR, "export-provider-configuration.tf"),
                     os.path.join(working_directory, "export-provider-configuration.tf"))
        shutil.copy2(os.path.join(TEMPLATES_DIR, "migrate-provider-configuration.py"),
                     os.path.join(working_directory, "migrate-provider-configuration.py"))

        payload = base64.b64encode(
            json.dumps(self.build_payload(group, name, vehicle)).encode("utf-8")
        ).decode("ascii")
        log_path = os.path.join(working_directory, "provider-configuration.log")

        code, _ = self.run_terraform(
            working_directory, [self.args.terraform_binary, "init", "-input=false", "-no-color"], log_path
        )
        if code != 0:
            message = f"`{self.args.terraform_binary} init` failed, see {log_path}"
            ConsoleOutput.error(f"Cannot migrate {label}: {message}")
            return {"name": name, "id": None, "status": STATUS_FAILED, "method": METHOD_REMOTE, "message": message}

        ConsoleOutput.info(f"Running a plan in TFC workspace '{vehicle.name}' (log: {log_path})")
        _, output = self.run_terraform(
            working_directory,
            [self.args.terraform_binary, "plan", "-input=false", "-no-color",
             f"-var=scalr_provider_configuration_payload={payload}"],
            log_path,
        )

        # The plan itself can fail for reasons that have nothing to do with the migration
        # (the configuration belongs to the customer), so Scalr is the source of truth.
        remote = self.parse_remote_result(output)
        resolved_name = name or remote.get("name")

        if resolved_name:
            configuration = self.find_provider_configuration(resolved_name)
            if configuration:
                status = STATUS_CREATED
                if remote.get("status") == "exists":
                    status = STATUS_EXISTING
                elif remote.get("status") == "updated":
                    status = STATUS_UPDATED
                ConsoleOutput.success(
                    f"Provider configuration '{resolved_name}' ({configuration['id']}) is available in Scalr"
                )
                return {"name": resolved_name, "id": configuration["id"], "status": status,
                        "method": METHOD_REMOTE, "key": remote.get("key") or group.key_value}

        message = remote.get("message") or f"the TFC run did not create the provider configuration, see {log_path}"
        resolved_key = remote.get("key")
        if resolved_key and not resolved_name:
            # The run found out which cloud account these credentials belong to; that identifier
            # is what an entry has to be keyed by.
            message += (f'. Add an entry with "key": "{resolved_key}" to '
                        f"'{self.args.parameters_file}', or pass --name-template")
        ConsoleOutput.error(f"Migration of {label} failed: {message}")
        return {"name": resolved_name, "id": None, "status": STATUS_FAILED, "method": METHOD_REMOTE,
                "key": resolved_key or group.key_value, "message": message}

    def remember(self, group: ConfigurationGroup, result: Dict) -> None:
        """Record which group a provider configuration name was handled by."""
        name = result.get("name")
        if not name or result["status"] in (STATUS_FAILED, STATUS_SKIPPED, STATUS_NO_PARAMETERS):
            return
        self.handled_names.setdefault(name, {
            "group": group.group_id,
            "id": result.get("id"),
            "method": result.get("method"),
            "status": result["status"],
        })

    def ensure_group(self, group: ConfigurationGroup) -> Dict:
        name = self.resolve_name(group)
        entry = self.entry_for(group)
        existing = None

        if name and name in self.handled_names:
            # Another cloud account of this run is mapped to the same provider configuration.
            # Writing it again would overwrite the credentials of the first one.
            handled = self.handled_names[name]
            ConsoleOutput.info(
                f"Provider configuration '{name}' was already handled for '{handled['group']}', "
                f"reusing it for '{group.group_id}'"
            )
            return {"name": name, "id": handled["id"], "status": STATUS_EXISTING,
                    "method": handled["method"], "shared_with": handled["group"]}

        if name:
            existing = self.find_provider_configuration(name)
            if existing and not self.args.update_existing:
                ConsoleOutput.info(f"Provider configuration '{name}' already exists ({existing['id']}), reusing it")
                return {"name": name, "id": existing["id"], "status": STATUS_EXISTING, "method": METHOD_DIRECT}
        elif group.key_value and group.provider.identity and not self.args.skip_remote_runs \
                and (self.unmatched_entries or self.args.name_template):
            # The variable only identifies the credentials, not the account. The run asks the
            # cloud which account they belong to and matches the parameters file with that.
            ConsoleOutput.info(
                f"No entry for {group.key_variable} '{group.key_value}'; the TFC run will ask "
                f"{group.provider.name} which account these credentials belong to and match on it"
            )
        elif group.key_value:
            # The identifier is known and has no entry: the TFC run could not do better.
            workspace_names = ', '.join(info.name for info in group.workspaces)
            ConsoleOutput.warning(
                f"No entry in '{self.args.parameters_file}' for {group.key_variable} '{group.key_value}' "
                f"(workspaces: {workspace_names}). Add an entry with "
                f'"key": "{group.key_value}", or one with "workspaces": ["{group.workspaces[0].name}"], '
                f"or pass --name-template"
            )
            return {"name": None, "id": None, "status": STATUS_NO_PARAMETERS, "method": None}
        elif not self.entries and not self.args.name_template:
            return {"name": None, "id": None, "status": STATUS_NO_PARAMETERS, "method": None}

        present = set(group.workspaces[0].env_variables)
        attributes = self.local_attributes(group, entry)
        missing = [a for a in required_attributes(group.provider, present) if not attributes.get(a)]

        if not missing and name:
            if self.args.dry_run:
                ConsoleOutput.info(f"[dry-run] Would create provider configuration '{name}' directly")
                return {"name": name, "id": None, "status": STATUS_WOULD_CREATE, "method": METHOD_DIRECT}
            return self.create_directly(group, name, existing)

        if self.args.skip_remote_runs:
            message = (f"needs {', '.join(missing)}, which is sensitive in TFC" if missing
                       else "the name can only be resolved inside the TFC run")
            ConsoleOutput.warning(f"Skipping {group.group_id}: {message} (--skip-remote-runs)")
            return {"name": name, "id": None, "status": STATUS_SKIPPED, "method": None, "message": message}

        if self.args.dry_run:
            ConsoleOutput.info(
                f"[dry-run] Would start a TFC run from workspace '{group.workspaces[0].name}' to create "
                f"'{name or 'a provider configuration named inside the run'}' "
                f"({', '.join(missing) or 'name resolution'} is only available there)"
            )
            return {"name": name, "id": None, "status": STATUS_WOULD_CREATE, "method": METHOD_REMOTE}

        return self.create_through_tfc_run(group, name)

    def create_unused_entries(self, results: Dict[str, Dict]) -> None:
        """Entries of the parameters file that no in-scope workspace uses."""
        used = {result.get("name") for result in results.values()}
        for key, entry in self.entries.items():
            if entry.name in used:
                continue

            existing = self.find_provider_configuration(entry.name)
            if existing:
                results[f"unused:{key}"] = {"name": entry.name, "id": existing["id"],
                                            "status": STATUS_EXISTING, "method": METHOD_DIRECT}
                continue
            if self.args.dry_run:
                results[f"unused:{key}"] = {"name": entry.name, "id": None,
                                            "status": STATUS_WOULD_CREATE, "method": METHOD_DIRECT}
                continue

            if not entry.provider_name:
                ConsoleOutput.warning(
                    f"Entry '{entry.key}' is not used by any workspace and has no 'provider_name', skipping it"
                )
                results[f"unused:{key}"] = {"name": entry.name, "id": None, "status": STATUS_SKIPPED,
                                            "method": None, "message": "no provider_name"}
                continue

            flags, environment_ids = self.flags_and_environments(entry)
            attributes = {
                "name": entry.name,
                "provider-name": entry.provider_name,
                **self.interpolate(entry.attributes, entry.key, f"entry '{entry.name}'"),
                **flags,
            }
            if entry.provider_name not in BUILT_IN_PROVIDERS and "is-custom" not in attributes:
                attributes["is-custom"] = True

            response = self.scalr.create_provider_configuration(
                self.args.account_id, attributes, environment_ids or None
            )
            pc_id = response["data"]["id"]
            for parameter in self.interpolate(entry.parameters, entry.key, f"entry '{entry.key}'"):
                self.scalr.create_provider_configuration_parameter(pc_id, parameter)
            ConsoleOutput.success(f"Created unused provider configuration '{entry.name}' ({pc_id})")
            results[f"unused:{key}"] = {"name": entry.name, "id": pc_id, "status": STATUS_CREATED,
                                        "method": METHOD_DIRECT}

    def consumed_variables(self, group: ConfigurationGroup) -> Tuple[Dict[str, str], Dict[str, str]]:
        """
        The TFC variables and variable sets whose values this provider configuration now holds,
        so the migration does not copy the same credentials into Scalr a second time.
        """
        credential_names: Set[str] = set()
        for env_names in group.provider.attribute_env.values():
            credential_names.update(env_names)

        variables: Dict[str, str] = {}
        variable_sets: Dict[str, str] = {}
        for info in group.workspaces:
            for name in credential_names:
                variable = info.env_variables.get(name)
                if not variable or not variable.id:
                    continue
                variables[variable.id] = name
                if variable.var_set_id:
                    variable_sets[variable.var_set_id] = variable.var_set_name or variable.var_set_id
        return variables, variable_sets

    # --- output ----------------------------------------------------------

    def write_map_file(self, groups: List[ConfigurationGroup], results: Dict[str, Dict],
                       skipped: List[Dict]) -> None:
        workspaces: Dict[str, Dict] = {}
        consumed_variables: Dict[str, Dict] = {}
        consumed_variable_sets: Dict[str, Dict] = {}

        for group in groups:
            result = results.get(group.group_id, {})
            if result.get("name") and result.get("status") in (STATUS_CREATED, STATUS_UPDATED, STATUS_EXISTING):
                variables, variable_sets = self.consumed_variables(group)
                for variable_id, key in variables.items():
                    consumed_variables[variable_id] = {"key": key, "provider_configuration": result["name"]}
                for var_set_id, name in variable_sets.items():
                    consumed_variable_sets[var_set_id] = {"name": name,
                                                          "provider_configuration": result["name"]}
            for info in group.workspaces:
                workspaces[info.name] = {
                    "provider": group.provider.name,
                    "key_variable": group.key_variable,
                    "key": result.get("key") or group.key_value,
                    "provider_configuration": result.get("name"),
                    "status": result.get("status", STATUS_NO_PARAMETERS),
                }

        document = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "tfc_organization": self.args.tfc_organization,
            "tfc_project": self.args.tfc_project,
            "workspaces": workspaces,
            "provider_configurations": results,
            "skipped_workspaces": skipped,
            # What migrate.sh --skip-provider-credentials leaves behind in TFC.
            "consumed": {
                "variables": consumed_variables,
                "variable_sets": consumed_variable_sets,
            },
        }

        directory = os.path.dirname(os.path.abspath(self.args.map_file))
        os.makedirs(directory, exist_ok=True)
        with open(self.args.map_file, 'w') as f:
            f.write(json.dumps(document, indent=2))
            f.write("\n")

        ConsoleOutput.success(f"Wrote the workspace → provider configuration map to '{self.args.map_file}'")

    def run(self) -> int:
        self.entries = self.load_parameters()
        workspaces, skipped = self.collect_workspaces()
        groups = self.build_groups(workspaces)

        matched = {id(entry) for entry in (self.entry_for(group) for group in groups) if entry}
        unique_entries = {id(entry): entry for entry in self.entries.values()}.values()
        self.unmatched_entries = [e for e in unique_entries if id(e) not in matched and e.key]

        ConsoleOutput.section("Creating provider configurations in Scalr")
        ConsoleOutput.info(f"{len(groups)} provider configuration(s) to resolve")

        results: Dict[str, Dict] = {}
        for group in groups:
            result = self.ensure_group(group)
            self.remember(group, result)
            results[group.group_id] = result

        if self.args.include_unused and self.entries:
            self.create_unused_entries(results)

        self.write_map_file(groups, results, skipped)

        ConsoleOutput.section("Summary")
        by_status: Dict[str, int] = {}
        for result in results.values():
            by_status[result["status"]] = by_status.get(result["status"], 0) + 1
        for status, count in sorted(by_status.items()):
            ConsoleOutput.info(f"{status}: {count}")

        failed = [r for r in results.values()
                  if r["status"] in (STATUS_FAILED, STATUS_NO_PARAMETERS, STATUS_SKIPPED)]
        if failed:
            ConsoleOutput.warning(
                f"{len(failed)} provider configuration(s) were not created: "
                f"{', '.join(r.get('name') or '<unnamed>' for r in failed)}"
            )
        if skipped:
            ConsoleOutput.warning(f"{len(skipped)} workspace(s) were skipped, see '{self.args.map_file}'")

        if self.args.fail_on_unresolved and (failed or skipped):
            ConsoleOutput.error("Some workspaces could not be mapped to a provider configuration")
            return 1
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Create Scalr provider configurations for the cloud accounts used by TFC/E workspaces'
    )
    parser.add_argument('--scalr-hostname', type=str, default=os.getenv("SCALR_HOSTNAME"),
                        help='Scalr hostname. Default: $SCALR_HOSTNAME')
    parser.add_argument('--scalr-token', type=str, default=os.getenv("SCALR_TOKEN"),
                        help='Scalr token. Default: $SCALR_TOKEN')
    parser.add_argument('--tfc-hostname', type=str, default=os.getenv("TFC_HOSTNAME") or DEFAULT_TFC_HOSTNAME,
                        help=f'TFC/E hostname. Default: $TFC_HOSTNAME or {DEFAULT_TFC_HOSTNAME}')
    parser.add_argument('--tfc-token', type=str, default=os.getenv("TFC_TOKEN"),
                        help='TFC/E token. Default: $TFC_TOKEN')
    parser.add_argument('--tfc-organization', type=str, default=os.getenv("TFC_ORGANIZATION"),
                        help='TFC/E organization name. Default: $TFC_ORGANIZATION')
    parser.add_argument('--scalr-environment', type=str,
                        help='Scalr environment the workspaces are migrated into. Provider configurations are '
                             'granted to it, and it is created if it does not exist yet. Without it, and without '
                             '"environments" in the parameters file, a provider configuration reaches no '
                             'environment until a migration links it to a workspace')
    parser.add_argument('--tfc-project', type=str, help='TFC project name to filter workspaces by')
    parser.add_argument('-w', '--workspaces', type=str, help='Workspaces to inspect. By default - all')
    parser.add_argument('--provider', type=str, choices=sorted(PROVIDERS_BY_NAME),
                        help='Only migrate this provider instead of detecting it per workspace')
    parser.add_argument('--key-variable', type=str,
                        help='Environment variable identifying the cloud account. Default: the well-known '
                             'variable of the detected provider (ARM_SUBSCRIPTION_ID, GOOGLE_PROJECT, ...)')
    parser.add_argument('--parameters-file', type=str, default=DEFAULT_PARAMETERS_FILE,
                        help=f'JSON file mapping cloud account identifiers to provider configuration names. '
                             f'Default: {DEFAULT_PARAMETERS_FILE}')
    parser.add_argument('--name-template', type=str,
                        help='Name used for identifiers the parameters file does not cover, e.g. '
                             '"{provider}-{key}". Makes the parameters file optional')
    parser.add_argument('--map-file', type=str, default=DEFAULT_MAP_FILE,
                        help=f'Where to write the workspace → provider configuration map. Default: {DEFAULT_MAP_FILE}')
    parser.add_argument('--skip-variable-sets', action='store_true',
                        help='Only read workspace variables, ignore TFC variable sets')
    parser.add_argument('--skip-remote-runs', action='store_true',
                        help='Never start a TFC run: only create provider configurations whose values are '
                             'readable through the TFC API')
    parser.add_argument('--skip-backend-secrets', action='store_true',
                        help='Do not create the TFC variable set holding the Scalr credentials, assume it exists')
    parser.add_argument('--credentials-set-name', type=str,
                        help='Name of the TFC variable set holding the Scalr credentials used by the TFC runs. '
                             f'Default: {constants.TFC_MIGRATOR_DEFAULT_SECRETS_VARSET_NAME}')
    parser.add_argument('--update-existing', action='store_true',
                        help='Update the credentials of provider configurations that already exist')
    parser.add_argument('--include-unused', action='store_true',
                        help='Also create provider configurations for parameters-file entries that no '
                             'in-scope workspace uses')
    parser.add_argument('--fail-on-unresolved', action='store_true',
                        help='Exit with a non-zero code if any in-scope workspace could not be mapped')
    parser.add_argument('--dry-run', action='store_true',
                        help='Report what would be created without changing anything')
    parser.add_argument('--terraform-binary', type=str, default='terraform',
                        help='Binary used for the TFC runs, e.g. tofu. Default: terraform')

    args = parser.parse_args()

    required_args = ['scalr_hostname', 'scalr_token', 'tfc_hostname', 'tfc_token', 'tfc_organization']
    missing_args = [arg for arg in required_args if not getattr(args, arg)]
    if missing_args:
        ConsoleOutput.error(f"Missing required arguments: {', '.join(missing_args)}")
        sys.exit(1)

    try:
        service = ProviderConfigurationService(ProviderConfigurationArgs.from_argparse(args))
        sys.exit(service.run())
    except InvalidInputError as e:
        ConsoleOutput.error(str(e))
        sys.exit(1)
    except errors.APIError as e:
        ConsoleOutput.error(f"Unable to create provider configurations. {e}")
        sys.exit(1)
    except errors.MigrationException as e:
        ConsoleOutput.error(str(e))
        sys.exit(1)
    except Exception as e:
        if os.getenv("SCALR_DEBUG_ENABLED"):
            traceback.print_exc()
        ConsoleOutput.error(f"Unexpected error: {e}")
        sys.exit(1)
