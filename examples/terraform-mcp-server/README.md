# terraform-mcp-server: a third-party streamable-HTTP server

The first server ITest has been pointed at that ITest did not write:
HashiCorp's `terraform-mcp-server`, **unmodified**, run from HashiCorp's own
image, over streamable HTTP on a loopback port. `examples/reference-mcp/` is
a server built to be broken on purpose; this one is a real server, and the
example proves only that ITest can *reach* it through a declaration and read
its tool list honestly.

## Running the server

```sh
docker run --rm -d --name tf-mcp -p 8080:8080 \
  -e TRANSPORT_MODE=streamable-http -e TRANSPORT_HOST=0.0.0.0 \
  hashicorp/terraform-mcp-server:1.3.0 --toolsets=registry
```

The image is pinned to `1.3.0`. It serves MCP at the path `/mcp` on the
published port; the declaration reaches it through the environment variable
**`TF_MCP_URL`** — a name, never a value — which you export as the loopback
URL of that endpoint. The URL is written nowhere in this example, on purpose.

**Toolsets.** `--toolsets` decides the tool surface. `registry` is the public
Terraform Registry: provider, module and policy lookups, every one annotated
`readOnlyHint: true`. `registry,terraform` adds the HCP Terraform / Terraform
Enterprise tools — workspaces, variables, runs, including destructive ones.
Restart the container with the other value and the tool list changes; a
second `itest plan` sees the change.

**No token.** The `terraform` toolset needs `TFE_TOKEN` to *call* anything,
but not to be *listed*, and listing is all this example does. No token is
set, none is declared, and none will be: nothing here calls a tool.

## Running ITest against it

From this directory, with the container up and `TF_MCP_URL` exported:

```sh
itest plan          # read-only: one tools/list, no tool is called
```

`plan` names this server under "Private hosts allowed by declaration" every
time: the URL is on loopback, which the SSRF guard refuses by default, and
the declaration opts in with `transport.allow_private_hosts: true`. A real
deployment never needs that line — it exists for a local server like this.

`plan` is the only command this example is for. The environment policy is
absent on purpose, so the safe floor holds: nothing active can run here.
