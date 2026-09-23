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
    def __init__(self, resource_type: str, name: str, attributes: Dict, hcl_resource_type: str,
                 transform: bool = True) -> None:
        self.resource_type = resource_type
        # Names read back from a generated main.tf are already transformed; transforming them
        # again would turn "r_workspace_a" into "r_r_workspace_a", and the resource would no
        # longer be recognized as the one already in the file.
        self.name = transform_name(name) if transform else name
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
    def __init__(self, resource_type: str, name: str, attributes: Dict, transform: bool = True) -> None:
        super().__init__(resource_type, name, attributes, "resource", transform)


class TerraformDataSource(AbstractTerraformResource):
    def __init__(self, resource_type: str, name: str, attributes: Dict, transform: bool = True) -> None:
        super().__init__(resource_type, name, attributes, "data", transform)


def _parse_value(raw: str) -> Any:
    value = raw.strip()
    if value.startswith('"') and value.endswith('"') and len(value) > 1:
        return value[1:-1]
    if value.lower() in ('true', 'false'):
        return value.lower() == 'true'
    if value.startswith('[') and value.endswith(']'):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def extract_resources(attrs_block: str) -> Dict:
    """
    Read the attributes of a generated block back. Nested blocks stay nested and lists stay
    lists: flattening a `vcs_repo` block into its parent, or turning `["a"]` into the string
    `["a"]`, produces a block that is no longer valid HCL when it is rendered again.
    """
    attrs: Dict = {}
    stack: List[Dict] = [attrs]
    heredoc_key: Optional[str] = None
    heredoc_lines: List[str] = []
    list_key: Optional[str] = None
    list_items: List[Any] = []

    for raw_line in attrs_block.split('\n'):
        line = raw_line.strip()

        # A list is rendered over several lines, so it is collected until its closing bracket.
        if list_key is not None:
            if line.startswith(']'):
                stack[-1][list_key] = list_items
                list_key, list_items = None, []
            elif line:
                list_items.append(_parse_value(line.rstrip(',')))
            continue

        if heredoc_key is not None:
            if line == 'EOT':
                stack[-1][heredoc_key] = '\n'.join(heredoc_lines)
                heredoc_key, heredoc_lines = None, []
            else:
                heredoc_lines.append(raw_line)
            continue

        if not line or line.startswith('#'):
            continue

        if line == '}' or line == '},':
            if len(stack) > 1:
                stack.pop()
            continue

        if '=' not in line:
            # `vcs_repo {` and other block openings
            if line.endswith('{'):
                key = line[:-1].strip()
                block: Dict = {}
                stack[-1][key] = block
                stack.append(block)
            continue

        key, value = line.split('=', 1)
        key, value = key.strip(), value.strip()

        if value == '{':
            block = {}
            stack[-1][key] = block
            stack.append(block)
            continue

        if value.startswith('<<'):
            heredoc_key = key
            continue

        if value == '[':
            list_key = key
            continue

        stack[-1][key] = _parse_value(value)

    return attrs
