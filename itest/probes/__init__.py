"""Active probes: the code an active-tier test runs to touch a live endpoint.

Probes live apart from the detectors and the CLI on purpose. A detector reads
Terraform and never touches the network; a probe touches the network and never
reads Terraform. Keeping them in separate packages means the read-only analysis
path has no accidental route to sending a request.
"""
