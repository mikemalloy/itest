"""The readiness page: `itest report --html`.

Two modules, one direction of flow. ``model`` turns a verify JSON document and
the manifest into the page's data and decides nothing about markup; ``render``
injects that data into the committed template and decides nothing about
meaning. Neither reads Terraform and neither touches the network.
"""
