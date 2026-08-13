"""Programmatic OpenAPI 3.1 spec builder for turnstone HTTP servers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from turnstone import __version__

if TYPE_CHECKING:
    from pydantic import BaseModel


def _schema_ref(model: type[BaseModel]) -> dict[str, Any]:
    """Return a $ref pointing to the model in #/components/schemas."""
    return {"$ref": f"#/components/schemas/{model.__name__}"}


def _json_content(model: type[BaseModel]) -> dict[str, Any]:
    return {"application/json": {"schema": _schema_ref(model)}}


def _collect_schemas(models: list[type[BaseModel]]) -> dict[str, Any]:
    """Generate component schemas from a list of Pydantic models."""
    schemas: dict[str, Any] = {}
    for model in models:
        json_schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
        defs = json_schema.pop("$defs", {})
        schemas[model.__name__] = json_schema
        schemas.update(defs)
    return schemas


def _component_refs(value: Any) -> set[str]:
    """Collect local schema names referenced anywhere in an OpenAPI value."""
    if isinstance(value, dict):
        refs = {
            ref.removeprefix("#/components/schemas/")
            for ref in [value.get("$ref")]
            if isinstance(ref, str) and ref.startswith("#/components/schemas/")
        }
        for nested in value.values():
            refs.update(_component_refs(nested))
        return refs
    if isinstance(value, list):
        list_refs: set[str] = set()
        for nested in value:
            list_refs.update(_component_refs(nested))
        return list_refs
    return set()


@dataclass
class QueryParam:
    """Describes a query parameter for an endpoint."""

    name: str
    description: str = ""
    required: bool = False
    schema_type: str = "string"
    default: Any = None
    enum: list[str] | None = None


@dataclass
class PathParam:
    """Describes validation metadata for one detected path parameter."""

    name: str
    description: str = ""
    schema_type: str = "string"
    pattern: str | None = None
    max_length: int | None = None


@dataclass
class EndpointSpec:
    """Declarative description of one endpoint for spec generation."""

    path: str
    method: str
    summary: str
    description: str = ""
    request_model: type[BaseModel] | None = None
    request_required: bool = True
    response_model: type[BaseModel] | None = None
    response_code: int = 200
    error_codes: list[int] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    query_params: list[QueryParam] = field(default_factory=list)
    path_params: list[PathParam] = field(default_factory=list)


def build_openapi(
    title: str,
    description: str,
    endpoints: list[EndpointSpec],
    models: list[type[BaseModel]],
) -> dict[str, Any]:
    """Build an OpenAPI 3.1.0 spec with a closed component graph."""
    from turnstone.api.schemas import ErrorResponse

    paths: dict[str, Any] = {}
    for ep in endpoints:
        method = ep.method.lower()
        op_id = ep.path.replace("/", "_").strip("_") + "_" + method
        op: dict[str, Any] = {"summary": ep.summary, "operationId": op_id}
        if ep.tags:
            op["tags"] = ep.tags
        if ep.description:
            op["description"] = ep.description
        # Auto-detect path parameters from {param} segments
        params: list[dict[str, Any]] = []
        path_metadata = {param.name: param for param in ep.path_params}
        for match in re.finditer(r"\{(\w+)\}", ep.path):
            name = match.group(1)
            metadata = path_metadata.get(name)
            schema: dict[str, Any] = {
                "type": metadata.schema_type if metadata is not None else "string"
            }
            parameter: dict[str, Any] = {
                "name": name,
                "in": "path",
                "required": True,
                "schema": schema,
            }
            if metadata is not None:
                if metadata.description:
                    parameter["description"] = metadata.description
                if metadata.pattern is not None:
                    schema["pattern"] = metadata.pattern
                if metadata.max_length is not None:
                    schema["maxLength"] = metadata.max_length
            params.append(parameter)
        if ep.query_params:
            for qp in ep.query_params:
                p: dict[str, Any] = {
                    "name": qp.name,
                    "in": "query",
                    "required": qp.required,
                    "schema": {"type": qp.schema_type},
                }
                if qp.description:
                    p["description"] = qp.description
                if qp.default is not None:
                    p["schema"]["default"] = qp.default
                if qp.enum:
                    p["schema"]["enum"] = qp.enum
                params.append(p)
        if params:
            op["parameters"] = params
        if ep.request_model:
            op["requestBody"] = {
                "required": ep.request_required,
                "content": _json_content(ep.request_model),
            }
        responses: dict[str, Any] = {}
        if ep.response_model:
            responses[str(ep.response_code)] = {
                "description": "Success",
                "content": _json_content(ep.response_model),
            }
        else:
            responses[str(ep.response_code)] = {"description": "Success"}
        for code in ep.error_codes:
            responses[str(code)] = {
                "description": f"Error {code}",
                "content": _json_content(ErrorResponse),
            }
        op["responses"] = responses
        paths.setdefault(ep.path, {})[method] = op

    # Endpoint models are part of the graph by construction.  Requiring every
    # caller to repeat them in ``models`` produced valid-looking operations
    # with dangling component references whenever that second registry drifted.
    # Key by schema name as well as class identity: two distinct Pydantic
    # classes with the same public component name would otherwise overwrite
    # each other silently in ``_collect_schemas``.
    unique_models: list[type[BaseModel]] = []
    models_by_name: dict[str, type[BaseModel]] = {}
    endpoint_models: list[type[BaseModel]] = []
    for endpoint in endpoints:
        if endpoint.request_model is not None:
            endpoint_models.append(endpoint.request_model)
        if endpoint.response_model is not None:
            endpoint_models.append(endpoint.response_model)
    candidate_models: list[type[BaseModel]] = [*models, *endpoint_models]
    candidate_models.append(ErrorResponse)
    for model in candidate_models:
        prior = models_by_name.get(model.__name__)
        if prior is not None and prior is not model:
            raise ValueError(
                "OpenAPI component name collision: "
                f"{model.__name__!r} is provided by distinct model classes"
            )
        if prior is None:
            models_by_name[model.__name__] = model
            unique_models.append(model)

    component_schemas = _collect_schemas(unique_models)
    spec: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {"title": title, "version": __version__, "description": description},
        "paths": paths,
        "components": {"schemas": component_schemas},
    }
    missing = _component_refs(spec) - set(component_schemas)
    if missing:
        raise ValueError(f"OpenAPI schema graph has unresolved refs: {sorted(missing)}")
    return spec
