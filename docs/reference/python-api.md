# Python API

## Client

::: secsdk.client.Client

## Defining providers

::: secsdk.provider.Provider
    options:
      members: [name, title, description, docs_url, settings_class, rate_limit, setup, limits, auth_status, invoke]

::: secsdk.operation.operation

::: secsdk.operation.Operation
    options:
      members: [fqname, tool_name, input_schema, output_schema, parameters, describe]

::: secsdk.provider.AuthStatus

## Results

::: secsdk.result.Result

## Errors

::: secsdk.errors
    options:
      show_root_heading: false

## Tool schemas

::: secsdk.tools.tool_definitions

## Scoring rules

::: secsdk.providers.intel.score_cve
