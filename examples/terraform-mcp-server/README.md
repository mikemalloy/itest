# terraform-mcp-server: a third-party streamable-HTTP server

The first server ITest has been pointed at that ITest did not write:
HashiCorp's `terraform-mcp-server`, **unmodified**, run from HashiCorp's own
image, over streamable HTTP on a loopback port. `examples/reference-mcp/` is
a server built to be broken on purpose; this one is a real server, and the
example proves only that ITest can *reach* it through a declaration and read
its tool list honestly.

## Running the server

```sh
docker run --rm -d --name tf-mcp -p 127.0.0.1:8080:8080 \
  hashicorp/terraform-mcp-server:1.3.0 \
  streamable-http --transport-host 0.0.0.0 --toolsets=registry
```

The image is pinned to `1.3.0`. It serves MCP at the path `/mcp` on the
published port; the declaration reaches it through the environment variable
**`TF_MCP_URL`** — a name, never a value — which you export as the loopback
URL of that endpoint. The URL is written nowhere in this example, on purpose.

Use the `streamable-http` subcommand, as above. The image's other form —
`-e TRANSPORT_MODE=streamable-http` with no subcommand — starts the same
server but ignores `--toolsets`: it logs `Enabled toolsets: [all]` whatever
you pass. The flag means something only in the subcommand form.

**Toolsets.** `--toolsets` decides the tool surface. `registry` is the public
Terraform Registry: nine provider, module and policy lookups, every one
annotated `readOnlyHint: true`. `terraform` is the HCP Terraform / Terraform
Enterprise set — workspaces, variables, runs, including destructive ones —
**and it registers nothing without `TFE_TOKEN`**: with no token,
`registry,terraform` lists the same nine tools as `registry`, and
`terraform` alone lists zero. Restart with a different value and the list
changes; a second `itest plan` sees it (nine tools, or none).

**No token.** The `terraform` toolset is gated by `TFE_TOKEN` at
registration, not just at call time, so its tools cannot even be listed
here. No token is set, none is declared, and none will be: this example
lists tools and calls none, and the surface a token would add is for a
later, separately gated session.

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
