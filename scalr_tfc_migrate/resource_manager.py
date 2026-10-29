"""Persists generated Terraform resources."""
import glob
import os
import re
from datetime import datetime
from typing import Dict, List, Optional

from scalr_tfc_migrate.constants import LAYOUT_PER_WORKSPACE, LAYOUT_SINGLE
from scalr_tfc_migrate.hcl import (
    AbstractTerraformResource,
    TerraformDataSource,
    TerraformResource,
    extract_resources,
    transform_name,
)


PROVIDERS_BLOCK = '''terraform {
  required_providers {
    scalr = {
      source  = "scalr/scalr"
      # scalr_workspace_remote_state_consumer and remote_state_sharing
      version = ">= 3.20.0"
    }
  }
}

'''


class ResourceManager:
    def __init__(self, output_dir: str, layout: str = LAYOUT_SINGLE) -> None:
        self.resources: List[TerraformResource] = []
        self.data_sources: List[TerraformDataSource] = []
        self.output_dir = output_dir
        self.layout = layout
        self._load_existing_data_sources()
        self._load_existing_resources()

    def _read_main_tf(self) -> Optional[str]:
        """
        Every generated block, whatever file it is in: a run with --layout per-workspace spreads
        them over ws_*.tf files, and a re-run must recognize them all.
        """
        paths = sorted(glob.glob(os.path.join(self.output_dir, "*.tf")))
        if not paths:
            return None
        content = []
        for path in paths:
            with open(path, "r") as f:
                content.append(f.read())
        return "\n".join(content)

    @staticmethod
    def workspace_file_name(owner: str) -> str:
        return f"ws_{re.sub(r'[^a-z0-9_]+', '_', owner.lower())}.tf"

    @staticmethod
    def _iter_blocks(content: str, kind: str):
        """
        Yield (type, name, body) for every `kind "type" "name" { ... }` block, matching braces
        so that a nested block such as `vcs_repo { ... }` does not end the resource early.
        """
        header = re.compile(rf'{kind}\s+"([^"]+)"\s+"([^"]+)"\s*{{')
        for match in header.finditer(content):
            depth = 1
            index = match.end()
            while index < len(content) and depth:
                if content[index] == '{':
                    depth += 1
                elif content[index] == '}':
                    depth -= 1
                index += 1
            yield match.group(1), match.group(2), content[match.end():index - 1]

    def _load_existing_resources(self):
        """Load existing resources from main.tf if it exists."""
        content = self._read_main_tf()
        if content is None:
            return

        for resource_type, name, attrs_block in self._iter_blocks(content, "resource"):
            # The name comes from the file, where it is already in its transformed form.
            self.resources.append(
                TerraformResource(resource_type, name, extract_resources(attrs_block), transform=False)
            )

    def _load_existing_data_sources(self):
        """Load existing resources from main.tf if it exists."""
        content = self._read_main_tf()
        if content is None:
            return

        for resource_type, name, attrs_block in self._iter_blocks(content, "data"):
            self.data_sources.append(
                TerraformDataSource(resource_type, name, extract_resources(attrs_block), transform=False)
            )

    def add_resource(self, resource: TerraformResource):
        """Add a resource if it doesn't already exist."""
        # Check if resource already exists
        if self.has_resource(resource.resource_type, resource.name):
            return

        self.resources.append(resource)

    def add_data_source(self, data_source: TerraformDataSource):
        """Add a resource if it doesn't already exist."""
        # Check if resource already exists
        if self.has_data_source(data_source.resource_type, data_source.name):
            return
        self.data_sources.append(data_source)

    def has_resource(self, resource_type: str, name: str) -> bool:
        converted = transform_name(name)

        for existing in self.resources:
            if existing.resource_type == resource_type and existing.name == converted:
                return True

        return False

    def get_resource(self, resource_type: str, name: str) -> Optional[AbstractTerraformResource]:
        converted = transform_name(name)
        for existing in self.data_sources + self.resources:
            if existing.resource_type == resource_type and existing.name == converted:
                return existing
        return None

    def has_data_source(self, resource_type: str, name: str) -> bool:
        converted = transform_name(name)

        for existing in self.data_sources:
            if existing.resource_type == resource_type and existing.name == converted:
                return True

        return False

    def write_resources(self, output_dir: str):
        os.makedirs(output_dir, exist_ok=True)

        # Only write new resources: a re-run appends what earlier runs did not generate.
        content = self._read_main_tf() or ""
        existing_resources = set()
        for resource in (self.resources + self.data_sources):
            pattern = f'{resource.hcl_resource_type} "{resource.resource_type}" "{resource.name}"'
            if pattern in content:
                existing_resources.add((resource.resource_type, resource.name))

        # File name -> blocks to append to it.
        files: Dict[str, List[str]] = {}
        for resource in (self.data_sources + self.resources):
            if (resource.resource_type, resource.name) in existing_resources:
                continue
            file_name = "main.tf"
            if self.layout == LAYOUT_PER_WORKSPACE and resource.owner and isinstance(resource, TerraformResource):
                file_name = self.workspace_file_name(resource.owner)
            files.setdefault(file_name, []).append(resource.to_hcl().replace("'", '"'))

        files.setdefault("main.tf", [])
        for file_name, blocks in files.items():
            path = os.path.join(output_dir, file_name)
            file_exists = os.path.exists(path)
            if file_exists and not blocks:
                continue
            with open(path, "a" if file_exists else "w") as f:
                if not file_exists:
                    f.write("# Generated by Scalr Migrator\n")
                    f.write(f"# Generated at: {datetime.now().isoformat()}\n\n")
                    if file_name == "main.tf":
                        # Add required provider block only for new file
                        f.write(PROVIDERS_BLOCK)
                for block in blocks:
                    f.write(block + "\n\n")

        # Write imports.tf. Blocks of earlier runs are kept: a wave migrated without applying the
        # code (--skip-post-migration, or a failed apply) still has to be imported by a later apply.
        # Import blocks are idempotent, so the ones already applied are no-ops.
        imports_path = os.path.join(output_dir, "imports.tf")
        imports = self._read_imports(imports_path)
        content = self._read_main_tf() or ""
        imports = {address: resource_id for address, resource_id in imports.items()
                   if self._has_block(content, address)}
        for resource in self.resources:
            if resource.id and (resource.resource_type, resource.name) not in existing_resources:
                imports[f"{resource.resource_type}.{resource.name}"] = resource.id

        with open(imports_path, "w") as f:
            f.write("# Generated by Scalr Migrator\n")
            f.write(f"# Generated at: {datetime.now().isoformat()}\n\n")
            f.write("# This file contains import blocks for resources.\n")
            f.write("# You can safely remove this file after successful import.\n\n")

            for address, resource_id in imports.items():
                f.write(f'import {{\n')
                f.write(f'  to = {address}\n')
                f.write(f'  id = "{resource_id}"\n')
                f.write(f'}}\n\n')

    @staticmethod
    def _read_imports(path: str) -> Dict[str, str]:
        """Import blocks of an imports.tf written by an earlier run: address -> id."""
        if not os.path.exists(path):
            return {}
        with open(path, "r") as f:
            content = f.read()
        return {
            address: resource_id for address, resource_id in re.findall(
                r'import\s*\{\s*to\s*=\s*([\w.]+)\s*id\s*=\s*"([^"]*)"\s*\}', content)
        }

    @staticmethod
    def _has_block(content: str, address: str) -> bool:
        resource_type, name = address.split(".", 1)
        return f'resource "{resource_type}" "{name}"' in content

