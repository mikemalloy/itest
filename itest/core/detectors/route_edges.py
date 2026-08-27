"""API Gateway route edge detector.

Emits one ``route_edge`` per API route and what that route invokes — the
place traffic from outside the stack enters it. Both API Gateway generations
are read, because both are in production use:

- **REST (v1).** ``aws_api_gateway_method`` is the route. Its path comes from
  the ``aws_api_gateway_resource`` it names (the API's root resource is not a
  resource of its own, so a method on it is path ``/``), and what it invokes
  from the ``aws_api_gateway_integration`` sharing its API, resource, and
  HTTP method.
- **HTTP API (v2).** ``aws_apigatewayv2_route`` is the route. Its
  ``route_key`` carries method and path in one string, and its ``target``
  names the ``aws_apigatewayv2_integration`` behind it.

A Lambda integration URI embeds the function ARN
(``.../functions/<arn>/invocations``); when that function is in the same
document the edge points at its address, otherwise the ARN is kept and the
point is flagged ``external: true``. Everything else an integration can reach
— an HTTP upstream, an AWS service, a MOCK response — is external by
definition: there is nothing in this state to resolve it to.

``auth`` records the route's authorization. ``NONE`` is not an error, but it
is finding-class the way ``wildcard_resource`` is on an IAM edge, so
``points.summary`` surfaces it as ``[open]``.

A route whose API, path, or integration is absent from the document emits
nothing: without both ends there is no edge to describe, only a guess.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime

from itest.core.manifest import IntegrationPoint

#: The function ARN embedded in a Lambda integration URI. An ARN carries no
#: slash, so the segment between the markers is exactly the ARN.
_LAMBDA_URI = re.compile(r"/functions/(?P<arn>arn:[^/]+)/invocations")

#: What a v2 ``$default`` route matches: any method, any path.
_DEFAULT_ROUTE = ("ANY", "/*")


def _point_id(api: str, method: str, path: str, target: str) -> str:
    """Content-derived id: the API, the route, and what it invokes.

    Deliberately not the attributes — an API key requirement toggling on a
    route does not make it a different integration point.
    """
    parts = ["route_edge", api, method, path, target]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12]


def _make_point(api: str, target: str, attributes: dict, hcl: str) -> IntegrationPoint:
    now = datetime.now(UTC)
    return IntegrationPoint(
        id=_point_id(api, attributes["method"], attributes["path"], target),
        type="route_edge",
        source=api,
        target=target,
        attributes=attributes,
        hcl_address=hcl,
        origin="detected",
        first_seen=now,
        last_seen=now,
    )


class RouteEdgeDetector:
    """Detect API route -> integration target edges."""

    handled_types = {
        # REST (v1)
        "aws_api_gateway_rest_api",
        "aws_api_gateway_resource",
        "aws_api_gateway_method",
        "aws_api_gateway_integration",
        "aws_api_gateway_deployment",
        "aws_api_gateway_stage",
        # HTTP API (v2)
        "aws_apigatewayv2_api",
        "aws_apigatewayv2_route",
        "aws_apigatewayv2_integration",
        "aws_apigatewayv2_stage",
    }

    def detect(self, plan_json: dict) -> list[IntegrationPoint]:
        from itest.core.detectors.base import iter_resources

        resources = [r for r in iter_resources(plan_json) if r.get("mode") == "managed"]
        functions = self._function_map(resources)

        points: list[IntegrationPoint] = []
        seen: set[str] = set()

        def emit(point: IntegrationPoint) -> None:
            if point.id in seen:
                return
            seen.add(point.id)
            points.append(point)

        for point in self._rest_edges(resources, functions):
            emit(point)
        for point in self._http_edges(resources, functions):
            emit(point)
        return points

    # -- REST (v1) ---------------------------------------------------------

    def _rest_edges(self, resources: list[dict], functions: dict[str, str]) -> list:
        apis = self._by_id(resources, "aws_api_gateway_rest_api")
        paths: dict[tuple[str, str], str] = {}
        for resource in self._of_type(resources, "aws_api_gateway_resource"):
            values = resource["values"]
            paths[(values.get("rest_api_id"), values.get("id"))] = values.get("path")

        integrations: dict[tuple[str, str, str], dict] = {}
        for resource in self._of_type(resources, "aws_api_gateway_integration"):
            values = resource["values"]
            key = (
                values.get("rest_api_id"),
                values.get("resource_id"),
                values.get("http_method"),
            )
            integrations.setdefault(key, values)

        stages = self._stage_map(
            resources, "aws_api_gateway_stage", "rest_api_id", "stage_name"
        )

        found = []
        for resource in self._of_type(resources, "aws_api_gateway_method"):
            values = resource["values"]
            api_id = values.get("rest_api_id")
            api = apis.get(api_id)
            if api is None:
                continue
            resource_id = values.get("resource_id")
            method = values.get("http_method") or ""
            if resource_id == api["values"].get("root_resource_id"):
                path = "/"
            else:
                path = paths.get((api_id, resource_id))
            if not path:
                continue
            integration = integrations.get((api_id, resource_id, method))
            if integration is None:
                continue

            target, external = self._resolve_target(
                integration.get("uri"), integration.get("type"), functions
            )
            found.append(
                _make_point(
                    api=api["address"],
                    target=target,
                    attributes={
                        "method": method,
                        "path": path,
                        "integration_type": integration.get("type"),
                        "auth": values.get("authorization"),
                        "api_key_required": bool(values.get("api_key_required")),
                        "stages": stages.get(api_id, []),
                        "external": external,
                    },
                    hcl=resource["address"],
                )
            )
        return found

    # -- HTTP API (v2) -----------------------------------------------------

    def _http_edges(self, resources: list[dict], functions: dict[str, str]) -> list:
        apis = self._by_id(resources, "aws_apigatewayv2_api")
        integrations: dict[tuple[str, str], dict] = {}
        for resource in self._of_type(resources, "aws_apigatewayv2_integration"):
            values = resource["values"]
            integrations.setdefault((values.get("api_id"), values.get("id")), values)

        stages = self._stage_map(resources, "aws_apigatewayv2_stage", "api_id", "name")

        found = []
        for resource in self._of_type(resources, "aws_apigatewayv2_route"):
            values = resource["values"]
            api_id = values.get("api_id")
            api = apis.get(api_id)
            if api is None:
                continue
            method, path = self._split_route_key(values.get("route_key"))
            target_ref = values.get("target") or ""
            if "/" not in target_ref:
                continue
            integration = integrations.get((api_id, target_ref.rsplit("/", 1)[-1]))
            if integration is None:
                continue

            target, external = self._resolve_target(
                integration.get("integration_uri"),
                integration.get("integration_type"),
                functions,
            )
            found.append(
                _make_point(
                    api=api["address"],
                    target=target,
                    attributes={
                        "method": method,
                        "path": path,
                        "integration_type": integration.get("integration_type"),
                        "auth": values.get("authorization_type"),
                        "api_key_required": bool(values.get("api_key_required")),
                        "stages": stages.get(api_id, []),
                        "external": external,
                    },
                    hcl=resource["address"],
                )
            )
        return found

    # -- shared ------------------------------------------------------------

    @staticmethod
    def _split_route_key(route_key) -> tuple[str, str]:
        """``"POST /ingest"`` -> ``("POST", "/ingest")``.

        ``$default`` is the catch-all route: any method, any path.
        """
        if not isinstance(route_key, str) or not route_key or route_key == "$default":
            return _DEFAULT_ROUTE
        parts = route_key.split(None, 1)
        if len(parts) == 2:
            return parts[0], parts[1]
        return _DEFAULT_ROUTE[0], parts[0]

    @staticmethod
    def _resolve_target(uri, integration_type, functions) -> tuple[str, bool]:
        """Return ``(target, external)`` for one integration."""
        match = _LAMBDA_URI.search(uri) if isinstance(uri, str) else None
        if match:
            arn = match.group("arn")
            address = functions.get(arn)
            return (address, False) if address else (arn, True)
        if isinstance(uri, str) and uri:
            # An HTTP upstream or an AWS service call: real, but nothing in
            # this document owns it.
            return uri, True
        # MOCK and friends answer from the gateway itself and carry no URI;
        # the integration type is the only honest label for the far end.
        return (str(integration_type) if integration_type else "unknown"), True

    @staticmethod
    def _of_type(resources: list[dict], rtype: str) -> list[dict]:
        return [
            r
            for r in resources
            if r.get("type") == rtype and isinstance(r.get("values"), dict)
        ]

    @classmethod
    def _by_id(cls, resources: list[dict], rtype: str) -> dict[str, dict]:
        mapping: dict[str, dict] = {}
        for resource in cls._of_type(resources, rtype):
            api_id = resource["values"].get("id")
            if isinstance(api_id, str) and api_id:
                mapping.setdefault(api_id, resource)
        return mapping

    @classmethod
    def _stage_map(
        cls, resources: list[dict], rtype: str, api_key: str, name_key: str
    ) -> dict[str, list[str]]:
        """Map each API id to the sorted names of the stages serving it."""
        mapping: dict[str, list[str]] = {}
        for resource in cls._of_type(resources, rtype):
            values = resource["values"]
            api_id = values.get(api_key)
            name = values.get(name_key)
            if not isinstance(api_id, str) or not isinstance(name, str) or not name:
                continue
            mapping.setdefault(api_id, []).append(name)
        return {api_id: sorted(names) for api_id, names in mapping.items()}

    @staticmethod
    def _function_map(resources: list[dict]) -> dict[str, str]:
        """Map every Lambda ARN spelling to its address."""
        mapping: dict[str, str] = {}
        for resource in resources:
            if resource.get("type") != "aws_lambda_function":
                continue
            values = resource.get("values") or {}
            for key in ("arn", "invoke_arn", "qualified_arn"):
                value = values.get(key)
                if isinstance(value, str) and value and value not in mapping:
                    mapping[value] = resource["address"]
        return mapping
