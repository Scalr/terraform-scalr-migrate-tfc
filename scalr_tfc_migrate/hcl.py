"""Terraform HCL helpers and resource representations."""
import json
import re
from typing import Any, Dict, List, Optional


def transform_name(name: str) -> str:
    return f"r_{name.lower().translate(str.maketrans({' ': '_', '-': '_'}))}"


class HClAttribute:
    def __init__(self, value, encode_required: bool = False) -> None:
        self.hcl_value = value
        self.encode_required = encode_required

    def get_hcl_value(self) -> Any:
        if not self.encode_required:
            return self.hcl_value

        try:
            json.loads(self.hcl_value)
            return json.dumps(self.hcl_value)
        except (ValueError, TypeError):
            return self.hcl_value


class HCLObject:
    def __init__(self, attributes: dict) -> None:
        self.attributes = attributes


class AbstractTerraformResource:
    def __init__(self, resource_type: str, name: str, attributes: Dict, hcl_resource_type: str) -> None:
        self.resource_type = resource_type
        self.name = transform_name(name)
        self.attributes = attributes
        self.id = None
        self.hcl_resource_type: str = hcl_resource_type

    def _render_attribute(self, attrs: list, key, value, ident: Optional[int] = None):
        if not ident:
            ident = 2

        if key == "vcs_repo" and self.resource_type == "scalr_workspace":
            # Special handling for vcs_repo block in scalr_workspace
            attrs.append((" " * ident) + "vcs_repo {")
            for repo_key, repo_value in value.items():
                if repo_value is not None:  # Skip None values
                    if isinstance(repo_value, str):
                        # Special handling for trigger_patterns
                        if repo_key == "trigger_patterns" and '\n' in repo_value:
                            attrs.append((" " * (ident + 2)) + f'{repo_key} = <<EOT')
                            attrs.extend(f'{line}' for line in repo_value.split('\n'))
                            attrs.append('    EOT')
                        else:
                            attrs.append((" " * (ident + 2)) + f'{repo_key} = "{repo_value}"')
                    elif isinstance(repo_value, bool):
                        attrs.append((" " * (ident + 2)) + f'{repo_key} = {str(repo_value).lower()}')
                    elif isinstance(repo_value, list):
                        attrs.append((" " * (ident + 2)) + f'{repo_key} = {json.dumps(repo_value)}')
            attrs.append("  }")
        elif isinstance(value, str):
            # Check if the value contains newlines and use EOT format if it does
            if '\n' in value:
                # Split the value into lines and indent each line
                lines = value.split('\n')
                attrs.append((" " * ident) + f'{key} = <<EOT')
                attrs.extend((" " * ident) + f'{line}' for line in lines)
                attrs.append((" " * ident) + f'EOT')
            else:
                attrs.append((" " * ident) + f'{key} = "{value}"')
        elif isinstance(value, bool):
            attrs.append((" " * ident) + f'{key} = {str(value).lower()}')
        elif isinstance(value, dict):
            attrs.append((" " * ident) + f'{key} = {json.dumps(value)}')
        elif isinstance(value, list):
            attrs.append((" " * ident) + f'{key} = [')
            for v in value:
                if isinstance(v, str):
                    attrs.append(f'"{v}",')
                elif isinstance(v, AbstractTerraformResource):
                    attrs.append((" " * (ident + 2)) + f'{v.get_address()},')
            attrs.append((" " * ident) + ']')

        elif isinstance(value, HClAttribute):
            attrs.append((" " * ident) + f'{key} = {value.get_hcl_value()}')
        elif isinstance(value, AbstractTerraformResource):
            attrs.append((" " * ident) + f'{key} = {value.get_address()}')
        elif isinstance(value, HCLObject):
            attrs.append((" " * ident) + f'{key} ' + '{')
            for hcl_key, hcl_value in value.attributes.items():
                self._render_attribute(attrs, hcl_key, hcl_value, ident + 2)
            attrs.append((" " * ident) + '}')
        elif value is None:
            pass
        else:
            attrs.append((" " * ident) + f'{key} = {value}')

    def to_hcl(self) -> str:
        attrs = []
        for key, value in self.attributes.items():
            self._render_attribute(attrs, key, value)

        return f'{self.hcl_resource_type} "{self.resource_type}" "{self.name}" {{\n{chr(10).join(attrs)}\n}}'

    def get_address(self):
        hcl_resource_type = f"{self.hcl_resource_type}." if self.hcl_resource_type == "data" else ''
        return f"{hcl_resource_type}{self.resource_type}.{self.name}.id"

    def add_attribute(self, name: str, value):
        self.attributes[name] = value


class TerraformResource(AbstractTerraformResource):
    def __init__(self, resource_type: str, name: str, attributes: Dict) -> None:
        super().__init__(resource_type, name, attributes, "resource")


class TerraformDataSource(AbstractTerraformResource):
    def __init__(self, resource_type: str, name: str, attributes: Dict) -> None:
        super().__init__(resource_type, name, attributes, "data")


def extract_resources(attrs_block: str) -> Dict:
    attrs = {}

    # Parse attributes from the block
    for line in attrs_block.split('\n'):
        line = line.strip()
        if '=' in line:
            key, value = line.split('=', 1)
            key = key.strip()
            value = value.strip()
            # Handle string values
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            # Handle boolean values
            elif value.lower() in ('true', 'false'):
                value = value.lower() == 'true'
            # Handle vcs_repo block
            elif key.strip() == 'vcs_repo':
                vcs_attrs = {}
                vcs_block = re.search(r'vcs_repo\s*{([^}]+)}', attrs_block, re.DOTALL)
                if vcs_block:
                    for vcs_line in vcs_block.group(1).split('\n'):
                        vcs_line = vcs_line.strip()
                        if '=' in vcs_line:
                            vcs_key, vcs_value = vcs_line.split('=', 1)
                            vcs_key = vcs_key.strip()
                            vcs_value = vcs_value.strip()
                            if vcs_value.startswith('"') and vcs_value.endswith('"'):
                                vcs_value = vcs_value[1:-1]
                            elif vcs_value.lower() in ('true', 'false'):
                                vcs_value = vcs_value.lower() == 'true'
                            vcs_attrs[vcs_key] = vcs_value
                attrs[key] = vcs_attrs
                continue
            attrs[key] = value
    return attrs

