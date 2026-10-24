"""`secctl dev scaffold <name>`: write a starter provider + test into the SDK tree."""

from __future__ import annotations

import re
from pathlib import Path

import secsdk.providers

PROVIDER_TEMPLATE = '''"""{title} provider.

TODO: one paragraph on what this source is and what it is useful for.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import {pyd_imports}
{settings_imports}
from ..operation import operation
from ..provider import {provider_imports}

API = "https://api.example.com"  # TODO: real base URL

{settings_block}
class Item(BaseModel):
    """TODO: the shape your operation returns. Keep it small and typed."""

    id: str
    name: str


class {cls}(Provider):
    name = "{name}"
    title = "{title}"
    description = "TODO: one line shown in `secctl providers` and the docs."
    docs_url = None  # TODO: link to upstream API docs
    rate_limit = (5, 1.0)  # (calls, seconds) client-side limit
{settings_attrs}
    @operation({op_args}examples=["secctl {name} search example"])
    def search(
        self,
        query: Annotated[str, Field(min_length=1, description="What to look for")],
        limit: Annotated[int, Field(ge=1, le=500)] = 20,
    ) -> list[Item]:
        """Search {title}. (First docstring line = CLI help + tool description.)"""
        data = self.http.get_json(
            f"{{API}}/search", params={{"q": query, "limit": limit}}{headers_arg}, limiter=self.limiter
        )
        return [parse_item(row) for row in data.get("results", [])]


def parse_item(row: dict[str, Any]) -> Item:
    """Pure mapping from the upstream JSON to our model: unit-test this directly."""
    return Item(id=str(row["id"]), name=row.get("name", ""))
'''

SETTINGS_BLOCK = """
class {cls}Settings(ProviderSettings):
    model_config = SettingsConfigDict(env_prefix="SECSDK_{upper}_")
    api_key: str | None = Field(None, validation_alias=AliasChoices("SECSDK_{upper}_API_KEY", "{upper}_API_KEY"))

"""

SETTINGS_ATTRS = """    settings_class = {cls}Settings
    settings: {cls}Settings

    def auth_status(self) -> AuthStatus:
        ok = bool(self.settings.api_key)
        return AuthStatus(required=True, configured=ok, missing=[] if ok else ["{upper}_API_KEY"])

    def _headers(self) -> dict[str, str]:
        return {{"Authorization": f"Bearer {{self.settings.api_key}}"}}
"""

TEST_TEMPLATE = """import httpx
import respx

from secsdk.providers.{name} import API, parse_item


def test_parse_item():
    item = parse_item({{"id": 1, "name": "x"}})
    assert item.id == "1" and item.name == "x"


@respx.mock
def test_search(client{env_fixture}):
    respx.get(f"{{API}}/search").mock(
        return_value=httpx.Response(200, json={{"results": [{{"id": "a", "name": "Alpha"}}]}})
    )
    result = client.execute("{name}.search", {{"query": "alpha"}})
    assert result.ok, result.error
    assert result.data[0].name == "Alpha"
"""


def scaffold(name: str, *, auth: bool = False, force: bool = False) -> list[Path]:
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,30}", name):
        raise ValueError("Provider name must be lowercase letters, digits or _ (e.g. 'greynoise').")
    providers_dir = Path(secsdk.providers.__file__).parent
    tests_dir = providers_dir.parents[2] / "tests" / "providers"
    cls = "".join(p.capitalize() for p in name.split("_"))
    title = name.replace("_", " ").title()
    upper = name.upper()

    provider_src = PROVIDER_TEMPLATE.format(
        name=name,
        cls=cls,
        title=title,
        pyd_imports="AliasChoices, BaseModel, Field" if auth else "BaseModel, Field",
        settings_imports=(
            "from pydantic_settings import SettingsConfigDict\n\nfrom ..config import ProviderSettings"
            if auth
            else ""
        ),
        provider_imports="AuthStatus, Provider" if auth else "Provider",
        settings_block=SETTINGS_BLOCK.format(cls=cls, upper=upper) if auth else "",
        settings_attrs=SETTINGS_ATTRS.format(cls=cls, upper=upper) if auth else "",
        op_args="auth=True, " if auth else "",
        headers_arg=", headers=self._headers()" if auth else "",
    )
    test_src = TEST_TEMPLATE.format(name=name, env_fixture=", monkeypatch" if auth else "")
    if auth:
        test_src = test_src.replace(
            "    result = client.execute(",
            f'    monkeypatch.setenv("{upper}_API_KEY", "test")\n    result = client.execute(',
        )

    targets = [(providers_dir / f"{name}.py", provider_src)]
    if tests_dir.parent.exists():
        targets.append((tests_dir / f"test_{name}.py", test_src))
    for path, _ in targets:
        if path.exists() and not force:
            raise FileExistsError(f"{path} already exists (use --force to overwrite)")
    for path, src in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(src)
    return [p for p, _ in targets]
