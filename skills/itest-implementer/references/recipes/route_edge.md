# Recipe: `route_edge`

An API route and what it invokes. The point asserts "this method on this path
still reaches that target", as declared by an API Gateway route and the
integration behind it.

Fixtures come from the shared template in
[`../conftest.md`](../conftest.md) — write that file once, not per recipe.

## 1. What the fields mean

A `route_edge` point in `.itest/manifest.yaml` looks like this:

```yaml
- id: 4f1c9a02be77
  type: route_edge
  source: aws_apigatewayv2_api.main
  target: aws_lambda_function.api
  attributes:
    method: ANY
    path: /api/{proxy+}
    integration_type: AWS_PROXY
    auth: NONE
    api_key_required: false
    stages:
      - $default
    external: false
  hcl_address: aws_apigatewayv2_route.api_any
```

| Field | Meaning |
|---|---|
| `source` | The API's HCL address: `aws_api_gateway_rest_api.<name>` (REST, v1) or `aws_apigatewayv2_api.<name>` (HTTP API, v2). **Which one decides the whole recipe** — the two APIs have different clients and different calls. |
| `target` | What the route invokes. An HCL address when the integration URI named a Lambda in this state; otherwise the raw URI, or the integration type for a `MOCK` that has none. |
| `attributes.method` | `GET`, `POST`, `ANY`… `ANY` is also what a v2 `$default` route becomes. |
| `attributes.path` | `/ingest`, `/api/{proxy+}`, or `/*` for `$default`. Path and method together identify the route. |
| `attributes.integration_type` | `AWS_PROXY`, `HTTP_PROXY`, `AWS`, `MOCK`. Assert it: a proxy integration silently becoming a MOCK is a live route that answers without ever reaching the function. |
| `attributes.auth` | The route's authorization. `NONE` means unauthenticated — see §5. |
| `attributes.api_key_required` | Whether the route demands an API key. A usage plan is what enforces the key; this only records that the route asks for one. |
| `attributes.stages` | Sorted names of the stages serving this API. A route on no deployed stage is unreachable however correct its wiring. |
| `hcl_address` | `aws_api_gateway_method.<name>` (v1) or `aws_apigatewayv2_route.<name>` (v2). |

Dispatch on `source`'s resource type first: `aws_api_gateway_rest_api` is §3,
`aws_apigatewayv2_api` is §4.

## 2. What to assert, and what not to

The honest check is **"is the route still wired to that target, on a stage
that exists?"** — configuration, read back from the API.

**Do not call the endpoint.** Issuing a request to the deployed URL is an
active probe: it runs whatever is behind the route, which for a POST may
write. It belongs to the active tier, which the interview enables explicitly
and never in prod, and it is not what this point claims. `test_invoke_method`
is the same thing wearing an API name — it executes the integration — so it is
out too, even though it reads as a "test" call.

Every call below is `get_*`, and none of them reach the function.

## 3. REST API (v1)

`hcl_address` is the method, and it carries the two ids the API needs: the
REST API id and the resource id. Resolve it, then read the integration back.

```python
def test_route_api_POST_ingest_to_ingest(apigateway, resolve):
    """Integration point 4f1c9a02be77.

    aws_api_gateway_rest_api.api -> aws_lambda_function.ingest
    type=route_edge method=POST path=/ingest integration_type=AWS_PROXY
      auth=NONE api_key_required=True stages=['prod']
    HCL: aws_api_gateway_method.ingest_post
    """
    method = resolve("aws_api_gateway_method.ingest_post")
    function = resolve("aws_lambda_function.ingest")

    integration = apigateway.get_integration(
        restApiId=method["rest_api_id"],
        resourceId=method["resource_id"],
        httpMethod=method["http_method"],
    )

    assert integration["type"] == "AWS_PROXY", (
        f"Integration is {integration['type']}, not AWS_PROXY. The route "
        "answers without reaching the function."
    )
    assert integration["uri"].endswith(f"{function['arn']}/invocations"), (
        f"POST /ingest now integrates with {integration['uri']}, not {function['arn']}."
    )
```

The URI is checked by suffix on purpose: the full value is
`arn:aws:apigateway:<region>:lambda:path/2015-03-31/functions/<function
arn>/invocations`, and only the tail is the claim this point makes. Building
the whole string in the test would pin the region and the service path for no
gain.

Then confirm each recorded stage exists and has a deployment behind it — a
stage without one serves nothing:

```python
def test_route_api_POST_ingest_stage_prod(apigateway, resolve):
    """Integration point 4f1c9a02be77 (stage coverage).

    aws_api_gateway_rest_api.api -> aws_lambda_function.ingest
    type=route_edge method=POST path=/ingest stages=['prod']
    HCL: aws_api_gateway_method.ingest_post
    """
    method = resolve("aws_api_gateway_method.ingest_post")

    stage = apigateway.get_stage(restApiId=method["rest_api_id"], stageName="prod")
    assert stage.get("deploymentId"), (
        "Stage prod has no deployment, so the route is not served there."
    )
```

Fold the stage check into the route test when the point has one stage; keep it
separate only if you are covering several. Either way the docstring stays the
generated one — never write a second test against the same point id under a
new name.

`get_method` is available too, and worth adding when `auth` or
`api_key_required` is what you care about:
`apigateway.get_method(...)["authorizationType"]` and `["apiKeyRequired"]`.

## 4. HTTP API (v2)

`hcl_address` is the route. Its live values carry the API id, the route id,
and the `target` string, and `target` is what links route to integration.

```python
def test_route_main_ANY_api_proxy_to_api(apigatewayv2, resolve):
    """Integration point 4f1c9a02be77.

    aws_apigatewayv2_api.main -> aws_lambda_function.api
    type=route_edge method=ANY path=/api/{proxy+} integration_type=AWS_PROXY
      auth=NONE api_key_required=False stages=['$default']
    HCL: aws_apigatewayv2_route.api_any
    """
    route = resolve("aws_apigatewayv2_route.api_any")
    function = resolve("aws_lambda_function.api")

    live = apigatewayv2.get_route(ApiId=route["api_id"], RouteId=route["id"])
    assert live["RouteKey"] == route["route_key"], (
        f"Route key is now {live['RouteKey']!r}, not {route['route_key']!r}; "
        "the deployed route no longer matches what this point covers."
    )
    assert live["Target"] == route["target"], (
        f"Route target moved from {route['target']} to {live['Target']}."
    )

    integration = apigatewayv2.get_integration(
        ApiId=route["api_id"],
        IntegrationId=live["Target"].rsplit("/", 1)[-1],
    )
    assert integration["IntegrationType"] == "AWS_PROXY", (
        f"Integration is {integration['IntegrationType']}, not AWS_PROXY."
    )
    assert integration["IntegrationUri"].endswith(f"{function['arn']}/invocations"), (
        f"The route now integrates with {integration['IntegrationUri']}, not "
        f"{function['arn']}."
    )
```

The route key comes from state rather than from a literal in the test, for
the same reason ids do: pasted, it stops describing this deployment the moment
the route moves, and the assertion quietly becomes about the recipe's author.

`IntegrationUri` for a v2 Lambda proxy is sometimes the bare function ARN
rather than the `/functions/<arn>/invocations` form. Accept either:

```python
def assert_targets_function(uri, function_arn):
    """True for both spellings AWS returns for a Lambda integration URI."""
    assert uri == function_arn or uri.endswith(f"{function_arn}/invocations"), (
        f"Integration URI {uri} does not name {function_arn}."
    )
```

Stages are `apigatewayv2.get_stage(ApiId=..., StageName=...)`. `$default` is a
real stage name, not a placeholder — pass it through unchanged.

## 5. Notes across both

- **`auth: NONE` is finding-class.** It is not automatically wrong: a public
  API is a design choice, and `api_key_required` or a WAF may be what guards
  it. But say so in the docstring, and if the point is meant to be
  authenticated, assert `authorizationType` explicitly so a silently removed
  authorizer fails the test rather than passing it.
- **`external: true`** means the target is not in this state — another stack's
  Lambda, or an HTTP upstream. Assert the recorded URI is still what the
  integration names, and say "cross-stack" in the docstring: a failure often
  means the other side moved.
- **A `MOCK` target** is the integration type itself, because there is no far
  end. Assert the type and stop; there is nothing further to check.
- **Existence is not reachability.** These assertions prove the route is
  declared and deployed. Whether a request actually succeeds depends on the
  function, its permissions, a custom domain, and WAF rules — none of which
  this point claims.

## 6. What a failure means

An integration type that changed, a URI that no longer names the recorded
function, or a route key that moved all mean the same thing: traffic that used
to reach the target no longer does, or reaches something else. That is drift,
and catching it here is cheaper than catching it as a 404 in production.

A missing stage means the route exists but is not served. It is a real finding
and a quiet one — the console shows the route just fine.

`ResourceNotFoundException` (v1: `NotFoundException`) from `get_integration`
means the integration is gone entirely: the method is declared with nothing
behind it.

`LookupError` from `resolve` means the address is not in the current state —
renamed, removed, or the wrong `terraform_dir`.

Report a failure as a **finding**, naming the method, the path, and both ends.
Ask the user whether it is drift or a bad assertion. Never relax the check to
"the API exists": that proves nothing about the route.
