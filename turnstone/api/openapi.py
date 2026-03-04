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
class EndpointSpec:
    """Declarative description of one endpoint for spec generation."""

    path: str
    method: str
    summary: str
    description: str = ""
    request_model: type[BaseModel] | None = None
    response_model: type[BaseModel] | None = None
    response_code: int = 200
    error_codes: list[int] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    query_params: list[QueryParam] = field(default_factory=list)


def build_openapi(
    title: str,
    description: str,
    endpoints: list[EndpointSpec],
    models: list[type[BaseModel]],
) -> dict[str, Any]:
    """Build an OpenAPI 3.1.0 spec dict."""
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
        for match in re.finditer(r"\{(\w+)\}", ep.path):
            params.append(
                {
                    "name": match.group(1),
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string"},
                }
            )
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
                "required": True,
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

    return {
        "openapi": "3.1.0",
        "info": {"title": title, "version": __version__, "description": description},
        "paths": paths,
        "components": {"schemas": _collect_schemas(models)},
    }
